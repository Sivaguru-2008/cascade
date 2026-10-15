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


def evaluate(rows_all: list[Row] | None = None) -> dict[str, Any]:
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
        if variant == "full":
            res["per_pair_recall"] = {
                pid: {"positives": len(idx), "recall": round(float(np.mean(blend[idx] >= model.threshold)), 4)
                      if idx else None}
                for pid in [f"K{i}" for i in range(1, 10)]
                for idx in [[i for i, r in enumerate(test) if r.pair_id == pid]]}
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
