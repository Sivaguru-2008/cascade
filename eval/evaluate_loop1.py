"""Loop 1 evaluation on the time-split test set (2025-07-01 .. 2025-12-31). Writes docs/eval/loop1.json.

Uses the time-correct candidate rows (loop1/features.py) and the saved models / thresholds. Nothing is tuned here.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from common.config import ROOT, get_settings
from loop1.features import Row, load_dataset, split_of
from loop1.train import Loop1Model, metrics_at, model_dir_for

OUT = ROOT / "docs" / "eval" / "loop1.json"


def _predict_p1(m1: Any, X: np.ndarray, backend: str) -> np.ndarray:
    if backend == "xgboost":
        import xgboost as xgb
        try:
            return np.asarray(m1.predict_proba(X)[:, 1], dtype=float)
        except Exception:
            return np.asarray(m1.get_booster().predict(xgb.DMatrix(X)), dtype=float)
    return np.asarray(m1.predict_proba(X)[:, 1], dtype=float)


def _scores(model: Loop1Model, rows: list[Row]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    feats = [dict(r.features) for r in rows]
    if model.variant == "no_known":
        for f in feats:
            f["pair_in_known_list"] = 0  # the no_known blend has w_known = 0; the feature is not a model input
    blend, m1, m2, _ = model.score(feats)
    return blend, m1, m2


def event_level(rows: list[Row], scores: np.ndarray, thr: float) -> dict[str, Any]:
    """One decision per incident event (max-scoring pair), as the live scorer does."""
    by_event: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_event[r.event_id].append(i)
    tp = fp = fn = 0
    for idx in by_event.values():
        k = max(idx, key=lambda i: scores[i])
        alerted = scores[k] >= thr
        has_pos = any(rows[i].label for i in idx)
        if alerted and rows[k].label:
            tp += 1
        elif alerted:
            fp += 1
        if has_pos and not (alerted and rows[k].label):
            fn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"events_with_candidates": len(by_event), "alerts": tp + fp, "true_alerts": tp, "false_alerts": fp,
            "missed_cascade_events": fn, "precision": round(prec, 4), "recall": round(rec, 4)}


def bootstrap_ci(scores: np.ndarray, y: np.ndarray, thr: float, n_boot: int = 1000, seed: int = 7) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    n = len(scores)
    precs, recs, f1s = [], [], []
    for _ in range(n_boot):
        idx = rng.choice(n, size=n, replace=True)
        m = metrics_at(scores[idx], y[idx], thr)
        precs.append(m["precision"])
        recs.append(m["recall"])
        f1s.append(m["f1"])
    return {
        "precision_95ci": [round(float(np.percentile(precs, 2.5)), 4), round(float(np.percentile(precs, 97.5)), 4)],
        "recall_95ci": [round(float(np.percentile(recs, 2.5)), 4), round(float(np.percentile(recs, 97.5)), 4)],
        "f1_95ci": [round(float(np.percentile(f1s, 2.5)), 4), round(float(np.percentile(f1s, 97.5)), 4)],
    }


def leave_one_pair_out(rows: list[Row], cat: Any, backend: str = "xgboost") -> dict[str, Any]:
    from loop1.features import FeatureSpace, NUMERIC
    from loop1.train import blend, choose_threshold, fit_m1, DEFAULT_WEIGHTS

    space = FeatureSpace(tuple(f for f in NUMERIC if f != "pair_in_known_list"), {"sex": ("F", "M", "U")})
    train = [r for r in rows if split_of(r.ts) == "train"]
    val = [r for r in rows if split_of(r.ts) == "val"]
    test = [r for r in rows if split_of(r.ts) == "test"]
    w = (0.75, 0.05, 0.20)

    lopo_results: dict[str, Any] = {}
    all_pids = [f"K{i}" for i in range(1, 10)]

    for held_out in all_pids:
        tr_sub = [r for r in train if r.pair_id != held_out]
        y_tr = np.array([r.label for r in tr_sub], dtype=int)
        X_tr = space.matrix([r.features for r in tr_sub])

        m1 = fit_m1(X_tr, y_tr, backend, feature_names=space.columns)

        val_sub = [r for r in val if r.pair_id != held_out]
        y_va = np.array([r.label for r in val_sub], dtype=int)
        X_va = space.matrix([r.features for r in val_sub])
        p1_va = _predict_p1(m1, X_va, backend)

        lo_va = np.array([r.features["pair_aSR_lower_ci"] for r in val_sub], dtype=float)
        kn_va = np.array([r.features["pair_in_known_list"] for r in val_sub], dtype=float)
        sb_va = blend(p1_va, lo_va, kn_va, w)
        thr, _ = choose_threshold(sb_va, y_va, target=0.82)

        test_held = [r for r in test if r.pair_id == held_out]
        if test_held:
            X_te = space.matrix([r.features for r in test_held])
            p1_te = _predict_p1(m1, X_te, backend)
            lo_te = np.array([r.features["pair_aSR_lower_ci"] for r in test_held], dtype=float)
            kn_te = np.zeros(len(test_held), dtype=float)  # novel -> unknown list
            sb_te = blend(p1_te, lo_te, kn_te, w)
            alerted = int(np.sum(sb_te >= thr))
            lopo_results[held_out] = {
                "test_positives": len(test_held),
                "alerted": alerted,
                "recall": round(alerted / len(test_held), 4),
                "mean_score": round(float(np.mean(sb_te)), 4),
                "threshold": round(float(thr), 4),
            }

    return lopo_results


def multi_seed_evaluation(rows: list[Row], cat: Any, seeds: Sequence[int] = (7, 42, 123),
                          backend: str = "xgboost") -> dict[str, Any]:
    from loop1.features import FeatureSpace, NUMERIC
    from loop1.train import blend, choose_threshold, fit_m1

    space = FeatureSpace(tuple(f for f in NUMERIC if f != "pair_in_known_list"), {"sex": ("F", "M", "U")})
    train = [r for r in rows if split_of(r.ts) == "train"]
    val = [r for r in rows if split_of(r.ts) == "val"]
    test = [r for r in rows if split_of(r.ts) == "test"]
    y_test_all = np.array([int(r.label or r.hidden_pair_id is not None) for r in test])
    w = (0.75, 0.05, 0.20)

    precs, recs, f1s = [], [], []
    for s in seeds:
        y_tr = np.array([r.label for r in train], dtype=int)
        X_tr = space.matrix([r.features for r in train])
        m1 = fit_m1(X_tr, y_tr, backend, seed=s, feature_names=space.columns)

        y_va = np.array([r.label for r in val], dtype=int)
        X_va = space.matrix([r.features for r in val])
        p1_va = _predict_p1(m1, X_va, backend)
        lo_va = np.array([r.features["pair_aSR_lower_ci"] for r in val], dtype=float)
        kn_va = np.array([r.features["pair_in_known_list"] for r in val], dtype=float)
        sb_va = blend(p1_va, lo_va, kn_va, w)
        thr, _ = choose_threshold(sb_va, y_va, target=0.82)

        X_te = space.matrix([r.features for r in test])
        p1_te = _predict_p1(m1, X_te, backend)
        lo_te = np.array([r.features["pair_aSR_lower_ci"] for r in test], dtype=float)
        kn_te = np.array([r.features["pair_in_known_list"] for r in test], dtype=float)
        sb_te = blend(p1_te, lo_te, kn_te, w)
        m = metrics_at(sb_te, y_test_all, thr)
        precs.append(m["precision"])
        recs.append(m["recall"])
        f1s.append(m["f1"])

    return {
        "seeds": list(seeds),
        "precision_mean": round(float(np.mean(precs)), 4),
        "precision_std": round(float(np.std(precs)), 4),
        "recall_mean": round(float(np.mean(recs)), 4),
        "recall_std": round(float(np.std(recs)), 4),
        "f1_mean": round(float(np.mean(f1s)), 4),
        "f1_std": round(float(np.std(f1s)), 4),
    }


def evaluate(rows_all: list[Row] | None = None, run_lopo: bool = True) -> dict[str, Any]:
    s = get_settings()
    rows_all = rows_all or load_dataset()
    test = [r for r in rows_all if split_of(r.ts) == "test"]
    y = np.array([r.label for r in test])
    out: dict[str, Any] = {"test_period": "2025-07-01..2025-12-31", "test_rows": len(test), "test_pos": int(y.sum()),
                           "variants": {}}
    for variant in ("full", "no_known", "no_asr"):
        d = model_dir_for(variant, s.model_dir)
        if not (d / "blend.json").exists():
            out["variants"][variant] = {"missing": True}
            continue
        model = Loop1Model.load(d)
        blend, m1, m2 = _scores(model, test)
        res = {"backend": model.backend, "weights": dict(zip(("m1", "asr", "known"), model.weights, strict=True)),
               "blend": metrics_at(blend, y, model.threshold), "m1": metrics_at(m1, y, model.m1_threshold),
               "m2": metrics_at(m2, y, model.m2_threshold), "event_level": event_level(test, blend, model.threshold)}
        y_all = np.array([int(r.label or r.hidden_pair_id is not None) for r in test])
        res["blend_with_hidden_labels"] = metrics_at(blend, y_all, model.threshold)
        res["event_level_with_hidden"] = event_level(test, blend, model.threshold)
        
        if variant == "full":
            res["bootstrap_ci"] = bootstrap_ci(blend, y_all, model.threshold)
            res["per_pair_recall"] = {
                pid: {"positives": len(idx), "recall": round(float(np.mean(blend[idx] >= model.threshold)), 4)
                      if idx else None}
                for pid in [f"K{i}" for i in range(1, 10)]
                for idx in [[i for i, r in enumerate(test) if r.pair_id == pid]]}
            if run_lopo:
                print("Running Leave-One-Pair-Out (LOPO) cross-validation...")
                res["lopo"] = leave_one_pair_out(rows_all, model.cat, model.backend)
                print("Running multi-seed stability check...")
                res["multi_seed"] = multi_seed_evaluation(rows_all, model.cat, backend=model.backend)
        known = model.cat.known_class_pairs
        co = [i for i, r in enumerate(test) if r.label == 0 and (r.a_l4, r.b_l4) in known]
        res["coincidental_known_pair"] = {
            "rows": len(co), "alerted": int(np.sum(blend[co] >= model.threshold)) if co else 0,
            "false_alert_rate": round(float(np.mean(blend[co] >= model.threshold)), 4) if co else None}
        neg = [i for i, r in enumerate(test) if r.label == 0 and r.hidden_pair_id is None]
        res["label0_false_alert_rate"] = round(float(np.mean(blend[neg] >= model.threshold)), 4)
        h1 = [i for i, r in enumerate(test) if r.hidden_pair_id == "H1"]
        res["hidden_H1_rows_alerted"] = {
            "rows": len(h1), "alerted": int(np.sum(blend[h1] >= model.threshold)) if h1 else 0,
            "note": "H1 labels were never used for training; evaluation only"}
        out["variants"][variant] = res
    return out



def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    res = evaluate()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2))
    full = res["variants"]["full"]
    print(json.dumps({"blend": full["blend"], "event_level": full["event_level"]}, indent=2))


if __name__ == "__main__":
    main()
