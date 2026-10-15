"""Drug catalogue + known-cascade list: loading, validation and lookups.

Run `python -m datagen.catalogue` to validate both CSV files.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from common.config import get_settings

ATC7 = re.compile(r"^[A-Z]\d\d[A-Z][A-Z]\d\d$")
UNKNOWN_REACTION = "unknown — pattern detected statistically"


@dataclass(frozen=True)
class Drug:
    drug_id: str
    generic_name: str
    atc_code: str
    atc_l4: str
    atc_l3: str
    is_marker: bool
    dose_mg: float
    is_fictional: bool

    @property
    def display_name(self) -> str:
        return self.generic_name if self.is_fictional else self.generic_name.capitalize()


@dataclass(frozen=True)
class KnownPair:
    pair_id: str
    index_drugs: tuple[str, ...]
    index_l4: tuple[str, ...]
    marker_drugs: tuple[str, ...]
    marker_l4: tuple[str, ...]
    reaction: str
    lag_min: int
    lag_max: int
    p_cascade: float
    source: str

    @property
    def verified(self) -> bool:
        return not self.source.startswith("VERIFY:")

    @property
    def source_short(self) -> str:
        """Display form, e.g. 'ThinkCascades' (the 'VERIFY:' marker is removed only for display)."""
        text = self.source.removeprefix("VERIFY:").strip()
        return text.split(" (")[0].strip()


class Catalogue:
    def __init__(self, drugs: list[Drug], known: list[KnownPair]) -> None:
        self.drugs = drugs
        self.by_id = {d.drug_id: d for d in drugs}
        self.by_name = {d.generic_name.lower(): d for d in drugs}
        self.by_l4: dict[str, list[Drug]] = {}
        for d in drugs:
            self.by_l4.setdefault(d.atc_l4, []).append(d)
        self.known = known
        self.known_by_id = {k.pair_id: k for k in known}
        self.known_class_pairs: dict[tuple[str, str], KnownPair] = {}
        for k in known:
            for a in k.index_l4:
                for b in k.marker_l4:
                    self.known_class_pairs[(a, b)] = k
        self.classes = sorted(self.by_l4)

    def drug(self, name: str) -> Drug:
        return self.by_name[name.lower()]

    def known_pair(self, a_l4: str, b_l4: str) -> KnownPair | None:
        return self.known_class_pairs.get((a_l4, b_l4))

    def class_examples(self, l4: str, n: int = 3) -> list[str]:
        return [d.display_name for d in self.by_l4.get(l4, [])[:n]]

    def all_drug_names(self) -> list[str]:
        return [d.generic_name for d in self.drugs]


def _split(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split("|") if v.strip())


def read_drugs(path: Path) -> list[Drug]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            Drug(
                drug_id=r["drug_id"],
                generic_name=r["generic_name"],
                atc_code=r["atc_code"],
                atc_l4=r["atc_l4"],
                atc_l3=r["atc_l3"],
                is_marker=r["is_marker_class"] == "1",
                dose_mg=float(r["typical_daily_dose_mg"]),
                is_fictional=r.get("is_fictional", "0") == "1",
            )
            for r in csv.DictReader(f)
        ]


def read_known(path: Path) -> list[KnownPair]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            KnownPair(
                pair_id=r["pair_id"],
                index_drugs=_split(r["index_drug"]),
                index_l4=_split(r["index_atc_l4"]),
                marker_drugs=_split(r["marker_drug"]),
                marker_l4=_split(r["marker_atc_l4"]),
                reaction=r["reaction"],
                lag_min=int(r["lag_min_days"]),
                lag_max=int(r["lag_max_days"]),
                p_cascade=float(r["p_cascade"]),
                source=r["source"],
            )
            for r in csv.DictReader(f)
        ]


def validate(cat: Catalogue) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    if len(cat.by_id) != len(cat.drugs):
        problems.append("duplicate drug_id")
    if len(cat.by_name) != len(cat.drugs):
        problems.append("duplicate generic_name")
    for d in cat.drugs:
        if not d.is_fictional and not ATC7.match(d.atc_code):
            problems.append(f"{d.drug_id}: malformed ATC code {d.atc_code}")
        if d.atc_l4 != d.atc_code[:5] or d.atc_l3 != d.atc_code[:4]:
            problems.append(f"{d.drug_id}: atc_l4/atc_l3 inconsistent with {d.atc_code}")
        if d.dose_mg <= 0:
            problems.append(f"{d.drug_id}: dose must be positive")
    for k in cat.known:
        for name in k.index_drugs + k.marker_drugs:
            if name.lower() not in cat.by_name:
                problems.append(f"{k.pair_id}: drug {name!r} missing from catalogue")
        if {cat.drug(n).atc_l4 for n in k.index_drugs if n.lower() in cat.by_name} != set(k.index_l4):
            problems.append(f"{k.pair_id}: index_atc_l4 does not match catalogue")
        if {cat.drug(n).atc_l4 for n in k.marker_drugs if n.lower() in cat.by_name} != set(k.marker_l4):
            problems.append(f"{k.pair_id}: marker_atc_l4 does not match catalogue")
        if not 0 < k.p_cascade < 1 or k.lag_min > k.lag_max:
            problems.append(f"{k.pair_id}: invalid p_cascade or lag range")
    if any(d.is_fictional and d.drug_id != "DNOVA" for d in cat.drugs):
        problems.append("only Novacor may be fictional")
    if any("novacor" in [n.lower() for n in k.index_drugs + k.marker_drugs] for k in cat.known):
        problems.append("Novacor must not be in cascades_known.csv")
    return problems


@lru_cache(maxsize=4)
def load_catalogue(data_dir: Path | None = None) -> Catalogue:
    data_dir = data_dir or get_settings().data_dir
    cat = Catalogue(read_drugs(data_dir / "drug_catalogue.csv"), read_known(data_dir / "cascades_known.csv"))
    problems = validate(cat)
    if problems:
        raise ValueError("catalogue invalid: " + "; ".join(problems))
    return cat


def load_faers(data_dir: Path | None = None) -> dict:
    path = (data_dir or get_settings().data_dir) / "faers_cache.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    cat = load_catalogue()
    markers = sum(d.is_marker for d in cat.drugs)
    print(f"catalogue OK: {len(cat.drugs)} drugs, {len(cat.classes)} ATC level-4 classes, "
          f"{markers} marker drugs")
    print(f"known cascades OK: {', '.join(k.pair_id for k in cat.known)}")


if __name__ == "__main__":
    main()
