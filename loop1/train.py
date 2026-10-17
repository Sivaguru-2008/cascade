"""Train Loop 1 models (section 5.4) and the inference wrapper used by the scorer and eval.

    python -m loop1.train            # builds the dataset if needed, trains full / no_known / no_asr

Split by time: train < 2025-01-01, validation 2025-01-01..2025-06-30, test 2025-07-01..2025-12-31.
M1 = XGBoost (fallback: scikit-learn HistGradientBoostingClassifier), M2 = logistic regression.
Final score = w1*M1 + w2*sigmoid(log(max(aSR_lower_ci, 1e-3))) + w3*pair_in_known_list, weights and
threshold tuned on validation only.
"""

from __future__ import annotations

import argparse
import json
import pickle
import platform
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, confusion_matrix, precision_recall_curve
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from common.config import get_settings
from datagen.catalogue import Catalogue, load_catalogue
from loop1 import explain
from loop1.features import ASR_FEATURES, KNOWN_FEATURE, FeatureSpace, Row, dataset_path, load_dataset, split_of

TARGET_PRECISION = 0.82
DEFAULT_WEIGHTS = (0.6, 0.2, 0.2)
NOVELTY_DROPS = ("A_atc_l3", "B_atc_l3", KNOWN_FEATURE)
VARIANTS: dict[str, tuple[str, ...]] = {
    "full": NOVELTY_DROPS,
    "no_known": NOVELTY_DROPS,
    "no_asr": (*NOVELTY_DROPS, *ASR_FEATURES),
}


def resolve_backend(requested: str) -> str:
    if requested in ("auto", "xgboost"):
        try:
            import xgboost  # noqa: F401

            return "xgboost"
        except Exception:
            if requested == "xgboost":
                raise
    return "hgb"


def fit_m1(X: np.ndarray, y: np.ndarray, backend: str, seed: int = 7, feature_names: Sequence[str] = ()) -> Any:
    pos = max(int(y.sum()), 1)
    neg = len(y) - pos
    if backend == "xgboost":
        import xgboost as xgb

        params: dict[str, Any] = dict(
            n_estimators=300, max_depth=5, learning_rate=0.05, subsample=0.9,
            colsample_bytree=0.9, min_child_weight=2, scale_pos_weight=neg / pos,
            tree_method="hist", eval_metric="aucpr", n_jobs=4, random_state=seed,
        )
        if feature_names:
            constraints = str(tuple(1 if "pair_aSR_" in c else 0 for c in feature_names))
            params["monotone_constraints"] = constraints
        model = xgb.XGBClassifier(**params)
        model.fit(X, y)
        return model

    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.experimental import enable_hist_gradient_boosting  # noqa: F401

    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_depth=6,
                                           class_weight="balanced", random_state=seed)
    model.fit(X, y)
    return model


def fit_m2(X: np.ndarray, y: np.ndarray) -> Pipeline:
    model = Pipeline([("scale", StandardScaler()),
                      ("lr", LogisticRegression(class_weight="balanced", max_iter=5000, C=1.0))])
    model.fit(X, y)
    return model


def fit_rx_ranker(X: np.ndarray, y_rx: np.ndarray, backend: str, seed: int = 7) -> tuple[Any, np.ndarray] | tuple[None, None]:
    """Multi-class reaction ranker predicting the adverse reaction for an alerted cascade."""
    valid_mask = y_rx >= 0
    X_sub, y_sub = X[valid_mask], y_rx[valid_mask]
    classes, y_encoded = np.unique(y_sub, return_inverse=True)
    if len(classes) <= 1:
        return None, None
    num_classes = len(classes)
    if backend == "xgboost":
        import xgboost as xgb

        params: dict[str, Any] = dict(
            objective="multi:softprob",
            num_class=num_classes,
            n_estimators=100,
            max_depth=4,
            learning_rate=0.05,
            tree_method="hist",
            eval_metric="mlogloss",
            n_jobs=4,
            random_state=seed,
        )
        model = xgb.XGBClassifier(**params)
        model.fit(X_sub, y_encoded)
        return model, classes

    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier

    model = HistGradientBoostingClassifier(
        max_iter=100, learning_rate=0.05, max_depth=5, random_state=seed
    )
    model.fit(X_sub, y_encoded)
    return model, classes


def asr_term(lo: np.ndarray) -> np.ndarray:
    """sigmoid(log(max(lo, 1e-3))) == max(lo, 1e-3) / (1 + max(lo, 1e-3))."""
    x = np.maximum(np.asarray(lo, dtype=float), 1e-3)
    return x / (1.0 + x)


def blend(m1: np.ndarray, lo: np.ndarray, known: np.ndarray, w: Sequence[float]) -> np.ndarray:
    return w[0] * m1 + w[1] * asr_term(lo) + w[2] * np.asarray(known, dtype=float)


def tune_weights(m1: np.ndarray, lo: np.ndarray, known: np.ndarray, y: np.ndarray, use_asr: bool,
                 use_known: bool, step: float = 0.05, max_known_w: float = 0.20,
                 min_m1_w: float = 0.70) -> tuple[tuple[float, float, float], float]:
    """Grid over the simplex, maximising validation PR-AUC; ties -> closest to the default weights."""
    best: tuple[float, tuple[float, float, float], float] | None = None
    grid = np.round(np.arange(step, 1.0 + 1e-9, step), 4)
    for w1 in grid:
        if w1 < min_m1_w - 1e-9:
            continue
        for w2 in np.round(np.arange(0.0, 1.0 - w1 + 1e-9, step), 4) if use_asr else [0.0]:
            w3 = round(1.0 - w1 - w2, 4) if use_known else 0.0
            if not use_known and use_asr:
                w2 = round(1.0 - w1, 4)
            if w3 < -1e-9 or (use_known and w3 > max_known_w + 1e-9):
                continue

            w = (float(w1), float(w2), float(w3))
            if not use_known and not use_asr:
                w = (1.0, 0.0, 0.0)
            ap = float(average_precision_score(y, blend(m1, lo, known, w)))
            dist = sum((a - b) ** 2 for a, b in zip(w, DEFAULT_WEIGHTS, strict=True))
            if best is None or ap > best[0] + 1e-9 or (abs(ap - best[0]) <= 1e-9 and dist < best[2]):
                best = (ap, w, dist)
    assert best is not None
    return best[1], best[0]


def choose_threshold(scores: np.ndarray, y: np.ndarray, target: float = TARGET_PRECISION) -> tuple[float, str]:
    """Lowest threshold with validation precision >= target; otherwise the best-F1 threshold."""
    precision, recall, thresholds = precision_recall_curve(y, scores)
    p, r = precision[:-1], recall[:-1]
    ok = np.nonzero((p >= target) & (r > 0))[0]
    if ok.size:
        return float(thresholds[ok].min()), f"lowest threshold with validation precision >= {target:.2f}"
    f1 = np.where(p + r > 0, 2 * p * r / np.maximum(p + r, 1e-12), 0)
    return float(thresholds[int(np.argmax(f1))]), f"precision {target:.2f} unreachable on validation: best F1"


def metrics_at(scores: np.ndarray, y: np.ndarray, thr: float) -> dict[str, Any]:
    pred = (scores >= thr).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"threshold": round(float(thr), 6), "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
            "pr_auc": round(float(average_precision_score(y, scores)), 4) if y.any() else None,
            "confusion": {"tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn)}}


# ----------------------------------------------------------------------------- inference wrapper
@dataclass
class Loop1Model:
    space: FeatureSpace
    m1: Any
    m2: Pipeline
    backend: str
    weights: tuple[float, float, float]
    threshold: float
    m1_threshold: float
    m2_threshold: float
    variant: str
    cat: Catalogue
    rx_model: Any | None = None
    rx_classes: list[str] | None = None

    @classmethod
    def load(cls, model_dir: Path, cat: Catalogue | None = None) -> Loop1Model:
        meta = json.loads((model_dir / "feature_list.json").read_text())
        bl = json.loads((model_dir / "blend.json").read_text())
        backend = bl["ml_backend"]
        if backend == "xgboost":
            import xgboost as xgb

            m1: Any = xgb.Booster()
            m1.load_model(str(model_dir / "m1.json"))
        else:
            with (model_dir / "m1.pkl").open("rb") as f:
                m1 = pickle.load(f)
        with (model_dir / "m2.pkl").open("rb") as f:
            m2 = pickle.load(f)
        w = bl["weights"]

        rx_model = None
        rx_classes = None
        rx_classes_file = model_dir / "reaction_classes.json"
        if rx_classes_file.exists():
            try:
                rx_classes = json.loads(rx_classes_file.read_text(encoding="utf-8"))
                if backend == "xgboost" and (model_dir / "rx_m1.json").exists():
                    import xgboost as xgb

                    rx_m1 = xgb.XGBClassifier()
                    rx_m1.load_model(str(model_dir / "rx_m1.json"))
                    rx_model = rx_m1
                elif (model_dir / "rx_m1.pkl").exists():
                    with (model_dir / "rx_m1.pkl").open("rb") as f:
                        rx_model = pickle.load(f)
            except Exception:
                rx_model = None

        return cls(FeatureSpace.from_json(meta), m1, m2, backend, (w["m1"], w["asr"], w["known"]),
                   float(bl["threshold"]), float(bl["m1_threshold"]), float(bl["m2_threshold"]), bl["variant"],
                   cat or load_catalogue(), rx_model, rx_classes)

    def m1_proba(self, X: np.ndarray) -> np.ndarray:
        if self.backend == "xgboost":
            import xgboost as xgb
            try:
                return np.asarray(self.m1.inplace_predict(X), dtype=float)
            except Exception:
                return np.asarray(self.m1.predict(xgb.DMatrix(X)), dtype=float)
        return self.m1.predict_proba(X)[:, 1]

    def score(self, feats: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Returns (blend, m1, m2, X) for a batch of feature dicts."""
        X = self.space.matrix(feats)
        p1 = self.m1_proba(X)
        p2 = self.m2.predict_proba(X)[:, 1]
        lo = np.array([f["pair_aSR_lower_ci"] for f in feats], dtype=float)
        known = np.array([f["pair_in_known_list"] for f in feats], dtype=float)
        return blend(p1, lo, known, self.weights), p1, p2, X

    def rank_reaction(self, x: np.ndarray) -> tuple[str, float, list[str]]:
        """Rank reactions for an alerted row; returns (top_reaction_name, prob, top_reasons)."""
        if self.rx_model is None or not self.rx_classes:
            return "unknown — pattern detected statistically", 0.0, []
        try:
            x_2d = x.reshape(1, -1)
            probs = self.rx_model.predict_proba(x_2d)[0]
            top_idx = int(np.argmax(probs))
            prob = float(probs[top_idx])
            rx_name = self.rx_classes[top_idx] if top_idx < len(self.rx_classes) else "unknown — pattern detected statistically"
            reasons: list[str] = []
            if self.backend == "xgboost":
                try:
                    import xgboost as xgb
                    booster = self.rx_model.get_booster()
                    contribs = booster.predict(xgb.DMatrix(x_2d, feature_names=list(self.space.columns)), pred_contribs=True)
                    if contribs.ndim == 3:
                        class_contribs = contribs[0, top_idx, :-1]
                    elif contribs.ndim == 2:
                        class_contribs = contribs[0, :-1]
                    else:
                        class_contribs = contribs.ravel()[:len(self.space.columns)]
                    top_f_indices = np.argsort(-np.abs(class_contribs))[:3]
                    reasons = [self.space.columns[i] for i in top_f_indices]
                except Exception:
                    pass
            return rx_name, prob, reasons
        except Exception:
            return "unknown — pattern detected statistically", 0.0, []

    def reasons(self, f: Mapping[str, Any], x: np.ndarray, b_drug_id: str) -> list[str]:
        if self.backend == "xgboost":
            contribs = explain.xgb_contributions(self.m1, self.space.columns, x)
        else:
            contribs = explain.linear_contributions(self.m2, self.space.columns, x)
        return explain.top_reasons(contribs, f, self.cat, b_drug_id)


def model_dir_for(variant: str, base: Path | None = None) -> Path:
    base = base or get_settings().model_dir
    return base if variant == "full" else base / variant


# ----------------------------------------------------------------------------- training
def _arrays(rows: Sequence[Row], space: FeatureSpace) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    X = space.matrix([r.features for r in rows])
    y = np.array([r.label for r in rows], dtype=int)
    lo = np.array([r.features["pair_aSR_lower_ci"] for r in rows], dtype=float)
    known = np.array([r.features["pair_in_known_list"] for r in rows], dtype=float)
    return X, y, lo, known


def train_variant(rows: Sequence[Row], variant: str, out: Path, backend: str, cat: Catalogue) -> dict[str, Any]:
    drop = VARIANTS[variant]
    space = FeatureSpace.build(cat, drop)
    train = [r for r in rows if split_of(r.ts) == "train"]
    val = [r for r in rows if split_of(r.ts) == "val"]
    Xtr, ytr, _, _ = _arrays(train, space)
    Xva, yva, lova, knva = _arrays(val, space)
    if variant == "no_known":
        knva = np.zeros_like(knva)
    t0 = time.time()
    m1 = fit_m1(Xtr, ytr, backend, feature_names=space.columns)
    m2 = fit_m2(Xtr, ytr)

    rx_model = None
    rx_classes = cat.reactions_list()
    rx_map = {name: i for i, name in enumerate(rx_classes)}
    if variant == "full":
        y_rx_tr = []
        X_rx_tr = []
        for r, x in zip(train, Xtr, strict=True):
            if r.label:
                rx_name = cat.reaction_for_pair(r.a_l4, r.b_l4)
                if rx_name in rx_map:
                    y_rx_tr.append(rx_map[rx_name])
                    X_rx_tr.append(x)
                elif r.pair_id and r.pair_id in cat.known_by_id:
                    k_rx = cat.known_by_id[r.pair_id].reaction
                    if k_rx in rx_map:
                        y_rx_tr.append(rx_map[k_rx])
                        X_rx_tr.append(x)
        if y_rx_tr:
            rx_model, active_classes = fit_rx_ranker(np.array(X_rx_tr), np.array(y_rx_tr), backend)
            if active_classes is not None:
                rx_active_names = [rx_classes[c] for c in active_classes]
            else:
                rx_active_names = None
        else:
            rx_model, rx_active_names = None, None

    fit_s = time.time() - t0
    if backend == "xgboost":
        import xgboost as xgb
        try:
            p1 = m1.predict_proba(Xva)[:, 1]
        except Exception:
            p1 = m1.get_booster().predict(xgb.DMatrix(Xva))
    else:
        p1 = m1.predict_proba(Xva)[:, 1]
    p2 = m2.predict_proba(Xva)[:, 1]
    weights, val_ap = tune_weights(p1, lova, knva, yva, use_asr=variant != "no_asr", use_known=variant != "no_known")
    sb = blend(p1, lova, knva, weights)
    thr, rule = choose_threshold(sb, yva)
    t1, _ = choose_threshold(p1, yva)
    t2, _ = choose_threshold(p2, yva)

    out.mkdir(parents=True, exist_ok=True)
    if backend == "xgboost":
        m1.get_booster().save_model(str(out / "m1.json"))
        if rx_model is not None:
            rx_model.save_model(str(out / "rx_m1.json"))
    else:
        with (out / "m1.pkl").open("wb") as f:
            pickle.dump(m1, f)
        if rx_model is not None:
            with (out / "rx_m1.pkl").open("wb") as f:
                pickle.dump(rx_model, f)
    with (out / "m2.pkl").open("wb") as f:
        pickle.dump(m2, f)
    if rx_model is not None and rx_active_names:
        (out / "reaction_classes.json").write_text(json.dumps(rx_active_names, indent=2))

    (out / "feature_list.json").write_text(json.dumps(space.to_json(), indent=2))
    blend_json = {"variant": variant, "dropped_features": list(drop), "ml_backend": backend,
                  "weights": {"m1": weights[0], "asr": weights[1], "known": weights[2]},
                  "weights_default": dict(zip(("m1", "asr", "known"), DEFAULT_WEIGHTS, strict=True)),
                  "weights_tuned_on": "validation 2025-01-01..2025-06-30, max PR-AUC",
                  "threshold": thr, "threshold_rule": rule, "m1_threshold": t1, "m2_threshold": t2,
                  "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / "blend.json").write_text(json.dumps(blend_json, indent=2))
    report = {
        "variant": variant, "ml_backend": backend, "arch": platform.machine(), "fit_seconds": round(fit_s, 1),
        "sizes": {"train": len(train), "validation": len(val)},
        "class_balance": {"train_pos": int(ytr.sum()), "train_neg": int(len(ytr) - ytr.sum()),
                          "val_pos": int(yva.sum()), "val_neg": int(len(yva) - yva.sum())},
        "validation": {"blend": metrics_at(sb, yva, thr), "m1": metrics_at(p1, yva, t1),
                       "m2": metrics_at(p2, yva, t2)},
        "weights": blend_json["weights"], "threshold_rule": rule,
        "validation_pr_auc_at_tuned_weights": round(val_ap, 4),
    }
    (out / "train_report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Train Loop 1 models")
    ap.add_argument("--variants", default="full,no_known,no_asr")
    ap.add_argument("--rebuild-dataset", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    cat = load_catalogue()
    if args.rebuild_dataset or not dataset_path().exists():
        from loop1 import features

        rows = features.build_dataset()
    else:
        rows = load_dataset()
    backend = resolve_backend(s.ml_backend)
    for variant in args.variants.split(","):
        rep = train_variant(rows, variant, model_dir_for(variant), backend, cat)
        v = rep["validation"]
        cb = rep["class_balance"]
        print(f"[{variant}] backend={backend} train={rep['sizes']['train']:,} (pos {cb['train_pos']}) "
              f"val={rep['sizes']['validation']:,} (pos {cb['val_pos']})")
        for name in ("blend", "m1", "m2"):
            m = v[name]
            print(f"   {name:5s} thr={m['threshold']:.3f} precision={m['precision']:.3f} recall={m['recall']:.3f} "
                  f"f1={m['f1']:.3f} pr_auc={m['pr_auc']}")
        print(f"   weights={rep['weights']} rule='{rep['threshold_rule']}'")


if __name__ == "__main__":
    main()
