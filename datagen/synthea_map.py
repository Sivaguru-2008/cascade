"""Optional: map Synthea medications.csv + patients.csv to CASCADE raw events (section 4.3).

    python -m datagen.synthea_map --synthea-dir path/to/synthea/csv --out data/generated/synthea

Only used when the team provides Synthea CSVs (https://github.com/synthetichealth/synthea, Apache-2.0).
Not exercised in this repository's tests or results: no Synthea data is bundled. Medications are matched to
the catalogue by generic name appearing in the Synthea DESCRIPTION; unmatched medications are skipped and
counted. Each medication interval becomes a dispense every 30 days from START to STOP (or the end date).
The output has no labels: cascades must be injected with datagen.inject logic if needed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import random
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from datagen.catalogue import load_catalogue
from datagen.replay import RAW_COLUMNS


def _stable(text: str) -> int:
    """Process-independent hash (built-in hash() is salted per process)."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def _band(birth: date, at: date) -> str:
    age = at.year - birth.year - ((at.month, at.day) < (birth.month, birth.day))
    return "90+" if age >= 90 else f"{age // 5 * 5}-{age // 5 * 5 + 4}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--synthea-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    cat = load_catalogue()
    names = sorted(((d.generic_name.split(" and ")[0].lower(), d) for d in cat.drugs if not d.is_fictional),
                   key=lambda t: -len(t[0]))
    patients: dict[str, tuple[date, str]] = {}
    with (args.synthea_dir / "patients.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            patients[r["Id"]] = (date.fromisoformat(r["BIRTHDATE"][:10]), r.get("GENDER", "U")[:1].upper() or "U")
    end = date.fromisoformat(args.end)
    rows: list[list[str]] = []
    skipped = 0
    with (args.synthea_dir / "medications.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            desc = r["DESCRIPTION"].lower()
            drug = next((d for n, d in names if re.search(rf"\b{re.escape(n)}\b", desc)), None)
            if drug is None or r["PATIENT"] not in patients:
                skipped += 1
                continue
            birth, sex = patients[r["PATIENT"]]
            start = datetime.fromisoformat(r["START"].replace("Z", "")).date()
            stop = datetime.fromisoformat(r["STOP"].replace("Z", "")).date() if r.get("STOP") else end
            prescriber = f"SYN-{_stable(r.get('ENCOUNTER', '')) % 300:04d}"
            pharmacy = f"PH{_stable(r['PATIENT']) % 50 + 1:03d}"
            d = start
            while d <= min(stop, end):
                ts = datetime.combine(d, datetime.min.time()) + timedelta(seconds=rng.randint(8 * 3600, 21 * 3600))
                rows.append([str(uuid.UUID(int=rng.getrandbits(128), version=4)), ts.isoformat(), pharmacy,
                             r["PATIENT"], prescriber, _band(birth, d), sex if sex in "MF" else "U", drug.drug_id,
                             drug.atc_code, drug.atc_l4, drug.atc_l3, "30", "30", str(drug.dose_mg)])
                d += timedelta(days=30)
    rows.sort(key=lambda x: x[1])
    args.out.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out / "events.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RAW_COLUMNS)
        w.writerows(rows)
    print(f"wrote {len(rows):,} events ({skipped:,} medication rows unmatched) -> {args.out}")


if __name__ == "__main__":
    main()
