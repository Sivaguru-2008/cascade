"""Loop 2 evaluation. Writes docs/eval/loop2.json.

* detection table for K1-K9 + H1 at the end of the data: flagged?, aSR [CI] at W=90, n, and the first
  simulated date the pair met the signal rule (streaming replay, one snapshot per simulated day);
* days from the Novacor launch to the H1 signal, per window (n >= 10 and lower CI > 1 at that window)
  and for the full rule;
* false signals (flagged pairs that were not injected) with and without Benjamini-Hochberg;
* a null run: the same population regenerated with --no-inject, then batch PSSA.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from common.config import ROOT, get_settings
from datagen import generate
from datagen.catalogue import load_catalogue
from loop1.candidates import Ev
from loop1.features import iter_raw_rows
from loop2 import pssa
from loop2.stream_counts import PState, StreamingPSSA

OUT = ROOT / "docs" / "eval" / "loop2.json"
LAUNCH = date(2025, 7, 1)


def injected_pairs() -> dict[tuple[str, str], str]:
    cat = load_catalogue()
    pairs = {ab: k.pair_id for ab, k in cat.known_class_pairs.items()}
    pairs[("C09XX", "A02BC")] = "H1"
    return pairs


def replay(events: Path, epoch: date) -> dict[str, Any]:
    cat = load_catalogue()
    targets = injected_pairs()
    eng = StreamingPSSA(cat.classes)
    states: dict[str, PState] = {}
    first_full: dict[tuple[str, str], int] = {}
    first_win: dict[tuple[tuple[str, str], int], int] = {}
    day = None

    def check(d: int) -> None:
        snap = eng.snapshot()
        for ab in targets:
            ij = snap.index(*ab)
            if ij is None:
                continue
            i, j = ij
            if snap.signal[i, j] and ab not in first_full:
                first_full[ab] = d
            for W in snap.windows:
                s = snap.stats[W]
                if (ab, W) not in first_win and s.n[i, j] >= pssa.MIN_N and np.nan_to_num(s.lo[i, j]) > 1:
                    first_win[(ab, W)] = d

    for raw in iter_raw_rows(events):
        ev = Ev.from_raw(raw, epoch)
        if day is not None and ev.day != day and day >= pssa.WASHOUT_DAYS:
            check(day)
        day = ev.day
        eng.process(states.setdefault(ev.patient, PState()), ev.atc_l4, ev.day)
    check(day or 0)
    return {"engine": eng, "snapshot": eng.snapshot(), "first_full": first_full, "first_win": first_win}


def iso(epoch: date, d: int | None) -> str | None:
    return None if d is None else (epoch + timedelta(days=d)).isoformat()


def evaluate(events: Path, null_events: Path | None) -> dict[str, Any]:
    s = get_settings()
    epoch = s.epoch
    r = replay(events, epoch)
    snap = r["snapshot"]
    targets = injected_pairs()
    s90 = snap.stats[90]
    table = []
    for ab, pid in sorted(targets.items(), key=lambda kv: (kv[1], kv[0])):
        ij = snap.index(*ab)
        if ij is None:
            table.append({"pair_id": pid, "a": ab[0], "b": ab[1], "flagged": False, "note": "no data"})
            continue
        i, j = ij
        table.append({"pair_id": pid, "a": ab[0], "b": ab[1], "flagged": bool(snap.signal[i, j]),
                      "flagged_bh": bool(snap.signal_bh[i, j]), "asr": _r(s90.asr[i, j]), "lo": _r(s90.lo[i, j]),
                      "hi": _r(s90.hi[i, j]), "n": int(s90.n[i, j]), "n_ab": int(s90.n_ab[i, j]),
                      "n_ba": int(s90.n_ba[i, j]), "first_signal_date": iso(epoch, r["first_full"].get(ab))})
    launch_day = (LAUNCH - epoch).days
    h1 = ("C09XX", "A02BC")
    h1_days = {f"W{W}": (r["first_win"][(h1, W)] - launch_day) if (h1, W) in r["first_win"] else None
               for W in snap.windows}
    h1_days["full_rule"] = (r["first_full"][h1] - launch_day) if h1 in r["first_full"] else None
    flagged = {(snap.classes[i], snap.classes[j]) for i, j in zip(*np.nonzero(snap.signal), strict=True)}
    flagged_bh = {(snap.classes[i], snap.classes[j]) for i, j in zip(*np.nonzero(snap.signal_bh), strict=True)}
    false = sorted(flagged - set(targets))
    false_bh = sorted(flagged_bh - set(targets))
    out: dict[str, Any] = {
        "events": str(events), "tested_pairs": int(snap.tested.sum()), "signals": len(flagged),
        "signals_bh": len(flagged_bh), "detection": table, "h1_days_from_launch": h1_days,
        "false_signals": {"count": len(false), "pairs": [f"{a}>{b}" for a, b in false]},
        "false_signals_bh": {"count": len(false_bh), "pairs": [f"{a}>{b}" for a, b in false_bh]},
    }
    if null_events is not None and null_events.exists():
        firsts, horizon = pssa.load_first_incidents(null_events, epoch)
        res = pssa.analyse(firsts, horizon, load_catalogue().classes)
        out["null_run"] = {"events": str(null_events), "tested_pairs": int(res.tested.sum()),
                           "signals": int(res.signal.sum()), "signals_bh": int(res.signal_bh.sum()),
                           "pairs": [f"{res.classes[i]}>{res.classes[j]}" for i, j in
                                     zip(*np.nonzero(res.signal), strict=True)]}
    return out


def _r(x: float) -> float | None:
    return None if not np.isfinite(x) else round(float(x), 3)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    s = get_settings()
    ap.add_argument("--events", type=Path, default=s.generated_dir / "events.csv.gz")
    ap.add_argument("--null-dir", type=Path, default=s.generated_dir / "null")
    ap.add_argument("--patients", type=int, default=None, help="null run population (default: from the report)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--skip-null", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    null_events = None
    if not args.skip_null:
        rep = json.loads((args.events.parent / "generation_report.json").read_text())
        patients = args.patients or rep["patients"]
        seed = args.seed if args.seed is not None else rep["seed"]
        null_events = args.null_dir / "events.csv.gz"
        print(f"null run: regenerating {patients} patients, seed {seed}, --no-inject -> {args.null_dir}")
        generate.main(["--patients", str(patients), "--seed", str(seed), "--no-inject", "--out", str(args.null_dir)])
    res = evaluate(args.events, null_events)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2))
    print(json.dumps({k: res[k] for k in ("signals", "signals_bh", "h1_days_from_launch", "false_signals")},
                     indent=2))
    if "null_run" in res:
        print("null run signals:", res["null_run"]["signals"], "BH:", res["null_run"]["signals_bh"])


if __name__ == "__main__":
    main()
