"""GenAI evaluation. Writes docs/eval/genai.json and docs/eval/genai_human_review.csv.

Drafts for 50 test-set alerts (the Loop 1 alerts the saved model raises in 2025-07..12, built exactly like
the live scorer's), using the configured GENAI_BACKEND. Reports the validator pass rate, template fallback rate,
mean / p95 generation time, and 3 verbatim examples. The review CSV has empty usable / needs_edit / wrong
columns: humans fill them; this script never does.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import platform
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from common.config import ROOT, get_settings
from genai.generate import build_input, generate_draft
from loop1.features import load_dataset, split_of
from loop1.score_service import make_alert
from loop1.train import Loop1Model, model_dir_for
from loop2.pssa import faers_supported_pairs

OUT = ROOT / "docs" / "eval" / "genai.json"
REVIEW = ROOT / "docs" / "eval" / "genai_human_review.csv"


def test_alerts(n: int, seed: int = 7) -> list[Any]:
    from datagen.catalogue import load_faers

    model = Loop1Model.load(model_dir_for("full"))
    rows = [r for r in load_dataset() if split_of(r.ts) == "test"]
    scores, _, _, X = model.score([r.features for r in rows])
    by_event: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_event[r.event_id].append(i)
    tops = [max(idx, key=lambda i: scores[i]) for idx in by_event.values()]
    alerted = sorted((i for i in tops if scores[i] >= model.threshold), key=lambda i: rows[i].ts)
    rng = np.random.default_rng(seed)
    pick = sorted(rng.choice(len(alerted), size=min(n, len(alerted)), replace=False).tolist())
    faers = faers_supported_pairs(load_faers())
    out = []
    for k, j in enumerate(pick):
        i = alerted[j]
        r = rows[i]
        f = r.features
        out.append(make_alert(model.cat, faers, f"EV-{k + 1:03d}", r.event_id, datetime.fromisoformat(r.ts),
                              r.pharmacy_id, "0" * 64, r.b_drug, r.b_l4, f, float(scores[i]),
                              model.reasons(f, X[i], r.b_drug)))
    return out


async def run(n: int, backend: str | None) -> dict[str, Any]:
    s = get_settings()
    alerts = test_alerts(n)
    drafts = [await generate_draft(build_input(a), backend=backend) for a in alerts]
    ms = np.array([d.gen_ms for d in drafts])
    name = backend or s.genai_backend
    fallback = [d for d in drafts if d.generator == "template (fallback)"]
    res: dict[str, Any] = {
        "backend_requested": name, "arch": platform.machine(), "n_drafts": len(drafts),
        "validator_pass_rate": round(float(np.mean([d.validator_pass for d in drafts])), 4) if drafts else None,
        "template_fallback_rate": round(len(fallback) / len(drafts), 4) if drafts else None,
        "generator_mix": {g: sum(d.generator == g for d in drafts) for g in sorted({d.generator for d in drafts})},
        "gen_ms_mean": round(float(ms.mean()), 2) if len(ms) else None,
        "gen_ms_p95": round(float(np.percentile(ms, 95)), 2) if len(ms) else None,
        "fallback_reasons_sample": sorted({r for d in fallback for r in d.reject_reasons})[:10],
        "validator_failures": [{"alert": d.alert_id, "reasons": d.reject_reasons} for d in drafts
                               if not d.validator_pass][:10],
        "llm_evaluated": name != "template",
        "examples": [{"alert_id": d.alert_id, "index_drug": a.index_drug, "marker_drug": a.marker_drug,
                      "generator": d.generator, "note": d.note_text,
                      "reaction_description": d.report.B_reaction.reaction_description}
                     for a, d in list(zip(alerts, drafts, strict=True))[:3]],
    }
    REVIEW.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["alert_id", "index_drug", "marker_drug", "generator", "note_text", "reaction_description",
                    "usable", "needs_edit", "wrong", "reviewer_comment"])
        for a, d in list(zip(alerts, drafts, strict=True))[:10]:
            w.writerow([d.alert_id, a.index_drug, a.marker_drug, d.generator, d.note_text,
                        d.report.B_reaction.reaction_description, "", "", "", ""])
    res["human_review_csv"] = str(REVIEW.relative_to(ROOT))
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--backend", default=None, help="override GENAI_BACKEND")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    res = asyncio.run(run(args.n, args.backend))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print(json.dumps({k: res[k] for k in ("backend_requested", "n_drafts", "validator_pass_rate",
                                          "template_fallback_rate", "gen_ms_mean", "gen_ms_p95")}, indent=2))


if __name__ == "__main__":
    main()
