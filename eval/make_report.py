"""Write docs/evaluation.md from the eval scripts' JSON outputs (docs/eval/*.json).

    python -m eval.make_report --run         # run all four evaluations, then write the report
    python -m eval.make_report               # only (re)write the report from existing JSON

Every number in docs/evaluation.md comes from these JSON files. Missed targets are stated plainly.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from typing import Any

from common.config import ROOT

EVAL = ROOT / "docs" / "eval"
REPORT = ROOT / "docs" / "evaluation.md"
TARGETS = {"precision": 0.80, "recall": 0.70}


def _git() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha + (" (with uncommitted changes)" if dirty else "")
    except Exception:
        return "unknown"


def _load(name: str) -> dict[str, Any] | None:
    p = EVAL / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _met(ok: bool) -> str:
    return "**MET**" if ok else "**MISSED**"


def _m(m: dict[str, Any]) -> str:
    c = m["confusion"]
    return (f"{m['precision']:.3f} | {m['recall']:.3f} | {m['f1']:.3f} | {m['pr_auc']} | {m['threshold']:.3f} | "
            f"{c['tp']} / {c['fp']} / {c['fn']} / {c['tn']}")


def loop1_section(r: dict[str, Any] | None) -> tuple[list[str], dict[str, bool]]:
    if r is None:
        return ["## Loop 1: not run (`python -m eval.evaluate_loop1`)", ""], {}
    full = r["variants"]["full"]
    b = full["blend"]
    t = {"precision": b["precision"] >= TARGETS["precision"], "recall": b["recall"] >= TARGETS["recall"]}
    L = ["## Loop 1: real-time scorer (test set 2025-07-01 .. 2025-12-31)", "",
         f"Candidate (A → B) pairs in the test period: {r['test_rows']:,}, of which {r['test_pos']} are labelled "
         f"cascades (K1–K9). Thresholds and blend weights were fixed on the validation set. "
         f"M1 backend: `{full['backend']}`.", "",
         "| Model | Precision | Recall | F1 | PR-AUC | Threshold | TP / FP / FN / TN |", "|---|---|---|---|---|---|---|",
         f"| Blend (with known-list prior) | {_m(full['blend'])} |", f"| M1 alone | {_m(full['m1'])} |",
         f"| M2 (logistic regression) | {_m(full['m2'])} |"]
    for v, label in (("no_known", "Ablation: without `pair_in_known_list`"),
                     ("no_asr", "Ablation: without `pair_aSR_*`")):
        x = r["variants"].get(v)
        if x and not x.get("missing"):
            L.append(f"| {label} (blend) | {_m(x['blend'])} |")
    L += ["", f"Targets: precision ≥ {TARGETS['precision']:.2f}: {_met(t['precision'])} ({b['precision']:.3f}); "
              f"recall ≥ {TARGETS['recall']:.2f}: {_met(t['recall'])} ({b['recall']:.3f}).", "",
          "Per-pair recall (blend):", "", "| Pair | Test positives | Recall |", "|---|---|---|"]
    for pid, v in full["per_pair_recall"].items():
        L.append(f"| {pid} | {v['positives']} | {v['recall']} |")
    ev = full["event_level"]
    L += ["", f"Event level (one decision per incident event, as the live scorer does): {ev['alerts']} alerts, "
              f"{ev['true_alerts']} true, {ev['false_alerts']} false, {ev['missed_cascade_events']} missed cascade "
              f"events; precision {ev['precision']:.3f}, recall {ev['recall']:.3f}.", "",
          "False alerts on coincidental A → B events (label 0, class pair on the known list):", "",
          "| Variant | Coincidental events | Alerted | False-alert rate | All label-0 pairs false-alert rate |",
          "|---|---|---|---|---|"]
    for v in ("full", "no_known", "no_asr"):
        x = r["variants"].get(v)
        if x and not x.get("missing"):
            c = x["coincidental_known_pair"]
            L.append(f"| {v} | {c['rows']} | {c['alerted']} | {c['false_alert_rate']} | {x['label0_false_alert_rate']} |")
    L += ["", "Hidden Novacor → PPI cascade (H1 labels used for evaluation only, never for training):", "",
          "| Variant | H1 test rows | Alerted by Loop 1 | Precision / recall counting H1 as positive |",
          "|---|---|---|---|"]
    for v in ("full", "no_known", "no_asr"):
        x = r["variants"].get(v)
        if x and not x.get("missing"):
            h = x["hidden_H1_rows_alerted"]
            w = x["blend_with_hidden_labels"]
            L.append(f"| {v} | {h['rows']} | {h['alerted']} | {w['precision']:.3f} / {w['recall']:.3f} |")
    L += ["", "Reading: with the known-list prior, Loop 1 flags every known cascade but also most coincidental "
              "events on known class pairs, and it never alerts on the hidden pair. Without the prior it relies on "
              "Loop 2's live aSR and catches the hidden cascade, at lower precision on known pairs. "
              "Both models are trained on synthetic data whose injection rates we chose.", ""]
    return L, t


def loop2_section(r: dict[str, Any] | None) -> tuple[list[str], dict[str, bool]]:
    if r is None:
        return ["## Loop 2: not run (`python -m eval.evaluate_loop2`)", ""], {}
    h1 = next((d for d in r["detection"] if d["pair_id"] == "H1"), None)
    t = {"h1": bool(h1 and h1.get("flagged"))}
    L = ["## Loop 2: streaming PSSA", "",
         f"End of data: {r['signals']} signals ({r['signals_bh']} after Benjamini–Hochberg, q = 0.05) among "
         f"{r['tested_pairs']} class pairs with n ≥ 10 at W = 90.", "",
         "| Pair | A → B (ATC L4) | Flagged | Flagged (BH) | aSR [95% CI] at W=90 | n (n_AB / n_BA) | First signal date |",
         "|---|---|---|---|---|---|---|"]
    for d in r["detection"]:
        if "asr" not in d:
            L.append(f"| {d['pair_id']} | {d['a']} → {d['b']} | no data | | | | |")
            continue
        hi = "∞" if d["hi"] is not None and d["hi"] >= 999 else d["hi"]
        L.append(f"| {d['pair_id']} | {d['a']} → {d['b']} | {'yes' if d['flagged'] else 'no'} | "
                 f"{'yes' if d['flagged_bh'] else 'no'} | {d['asr']} [{d['lo']}–{hi}] | "
                 f"{d['n']} ({d['n_ab']} / {d['n_ba']}) | {d['first_signal_date'] or '—'} |")
    hd = r["h1_days_from_launch"]
    L += ["", "Days from the Novacor launch (2025-07-01) to the H1 signal: " + ", ".join(
        f"{k.replace('_', ' ')}: {v if v is not None else 'not reached'}" for k, v in hd.items()) +
        " (per window: n ≥ 10 and lower CI > 1 at that window; full rule: section 6.1).", "",
        f"H1 detected: {_met(t['h1'])}.", "",
        f"False signals (flagged pairs that were not injected): {r['false_signals']['count']} without BH, "
        f"{r['false_signals_bh']['count']} with BH."]
    if r["false_signals"]["pairs"]:
        L.append("Without BH: " + ", ".join(f"`{p}`" for p in r["false_signals"]["pairs"]) + ".")
    if r["false_signals_bh"]["pairs"]:
        L.append("With BH: " + ", ".join(f"`{p}`" for p in r["false_signals_bh"]["pairs"]) + ".")
    nr = r.get("null_run")
    if nr:
        L += ["", f"Null run (same population and seed regenerated with `--no-inject`): {nr['signals']} signals "
                  f"without BH, {nr['signals_bh']} with BH, among {nr['tested_pairs']} tested pairs."]
    else:
        L += ["", "Null run: not run."]
    L += ["", "Reading: the CI rule alone is not corrected for testing hundreds of pairs, so chance signals appear "
              "even with no injected cascade (null run). BH removes most of them.", ""]
    return L, t


def genai_section(r: dict[str, Any] | None) -> list[str]:
    if r is None:
        return ["## GenAI: not run (`python -m eval.evaluate_genai`)", ""]
    L = ["## GenAI: prescriber note + PvPI draft", "",
         f"Backend requested: `{r['backend_requested']}`; drafts: {r['n_drafts']} (test-set alerts).", "",
         "| Validator pass rate | Template fallback rate | Mean generation time | p95 generation time | Generator mix |",
         "|---|---|---|---|---|",
         f"| {r['validator_pass_rate']:.0%} | {r['template_fallback_rate']:.0%} | {r['gen_ms_mean']} ms | "
         f"{r['gen_ms_p95']} ms | {', '.join(f'{k}: {v}' for k, v in r['generator_mix'].items())} |", ""]
    if not r["llm_evaluated"]:
        L += ["**No LLM backend was evaluated in this run** (GENAI_BACKEND=template). LLM pass and fallback "
              "rates are TBD (run `GENAI_BACKEND=lab|transformers|llamacpp python -m eval.evaluate_genai`).", ""]
    L += ["Three verbatim examples:", ""]
    for ex in r["examples"]:
        L += [f"- **{ex['alert_id']}** ({ex['index_drug']} → {ex['marker_drug']}, {ex['generator']})",
              f"  - Note: {ex['note']}", f"  - PvPI reaction description: {ex['reaction_description']}"]
    L += ["", f"Human review sheet (10 drafts; columns usable / needs edit / wrong are left empty for reviewers): "
              f"`{r['human_review_csv']}`. Human review: TBD.", ""]
    return L


def system_section(r: dict[str, Any] | None) -> tuple[list[str], dict[str, bool]]:
    if r is None:
        return ["## System: not run (`python -m eval.latency_report`)", ""], {"latency": False}
    lat = r["latency_ms"] or {}
    L = ["## System: throughput and scoring latency", "",
         f"Mode: {r['mode']}; architecture `{r['arch']}`; {r['cpu_count']} CPUs; replay at `--speed max` "
         f"for {r['requested_duration_s']:.0f} s requested ({r['publish_seconds']} s publishing).", "",
         "| Events scored | Sustained events/s | p50 | p95 | p99 | max | Alerts |", "|---|---|---|---|---|---|---|",
         f"| {r['events_scored']:,} | {r['sustained_events_per_s']} | {lat.get('p50')} ms | {lat.get('p95')} ms | "
         f"{lat.get('p99')} ms | {lat.get('max')} ms | {r['alerts']} |", "",
         f"Latency = time from message read to alert write or no-alert decision; {r['note']}.", ""]
    if r["publish_seconds"] + 1 < r["requested_duration_s"]:
        L += [f"The dataset ran out after {r['publish_seconds']} s, so the run is shorter than the requested "
              f"{r['requested_duration_s']:.0f} s.", ""]
    return L, {"latency": bool(lat)}


def write_report() -> str:
    l1, t1 = loop1_section(_load("loop1"))
    l2, t2 = loop2_section(_load("loop2"))
    g = genai_section(_load("genai"))
    sy, t3 = system_section(_load("latency"))
    arch = platform.machine()
    lines = ["# CASCADE evaluation", "",
             f"Generated by `eval/make_report.py` at {time.strftime('%Y-%m-%d %H:%M:%S %Z')} · git commit `{_git()}` · "
             f"`uname -m` = **{arch}**", ""]
    if arch != "s390x":
        lines += [f"> **This report was produced on a {arch} development machine, not on the IBM LinuxONE VM.** "
                  "Re-run `python -m eval.make_report --run` on the VM for the submission numbers.", ""]
    lines += ["All data is synthetic; cascade rates were chosen by the team (see `docs/DECISIONS.md`). "
              "These numbers describe the system's behaviour on that data, not clinical performance.", "",
              "## Targets", "", "| Target | Result |", "|---|---|",
              f"| Loop 1 precision ≥ 0.80 | {_met(t1.get('precision', False)) if t1 else 'not run'} |",
              f"| Loop 1 recall ≥ 0.70 | {_met(t1.get('recall', False)) if t1 else 'not run'} |",
              f"| H1 detected by Loop 2 | {_met(t2.get('h1', False)) if t2 else 'not run'} |",
              f"| Latency measured | {_met(t3.get('latency', False))} |", ""]
    lines += l1 + l2 + g + sy
    text = "\n".join(lines).rstrip() + "\n"
    REPORT.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true", help="run all evaluations first")
    ap.add_argument("--latency-seconds", type=float, default=300.0)
    args = ap.parse_args()
    if args.run:
        py = sys.executable
        for mod, extra in (("eval.evaluate_loop1", []), ("eval.evaluate_loop2", []), ("eval.evaluate_genai", []),
                           ("eval.latency_report", ["--duration", str(args.latency_seconds)])):
            print(f"== {mod}", flush=True)
            subprocess.run([py, "-m", mod, *extra], cwd=ROOT, check=True)
    write_report()
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
