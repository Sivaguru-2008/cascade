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


@dataclass(frozen=True)
class Reaction:
    reaction_id: str
    reaction_name: str
    index_l4: tuple[str, ...]
    index_drugs: tuple[str, ...]
    lag_min: int
    lag_max: int
    p_reaction: float
    source: str


@dataclass(frozen=True)
class Treatment:
    treatment_id: str
    reaction_id: str
    marker_l4: tuple[str, ...]
    marker_drugs: tuple[str, ...]
    p_treatment: float
    source: str


@dataclass(frozen=True)
class Indication:
    condition_id: str
    condition_name: str
    atc_l4: str
    drugs: tuple[str, ...]
    first_line: bool
    typical_age_min: int


class Catalogue:
    def __init__(
        self,
        drugs: list[Drug],
        known: list[KnownPair],
        reactions: list[Reaction] | None = None,
        treatments: list[Treatment] | None = None,
        indications: list[Indication] | None = None,
    ) -> None:
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

        # Extended schema split: reactions, treatments, indications
        self.reactions = reactions or []
        self.reactions_by_id = {r.reaction_id: r for r in self.reactions}
        self.treatments = treatments or []
        self.treatments_by_reaction: dict[str, list[Treatment]] = {}
        for t in self.treatments:
            self.treatments_by_reaction.setdefault(t.reaction_id, []).append(t)
        self.indications = indications or []
        self.indications_by_condition: dict[str, list[Indication]] = {}
        self.condition_classes: set[tuple[str, str]] = set()
        for ind in self.indications:
            self.indications_by_condition.setdefault(ind.condition_name.lower(), []).append(ind)
            self.condition_classes.add((ind.condition_name.lower(), ind.atc_l4))

    def drug(self, name: str) -> Drug:
        return self.by_name[name.lower()]

    def known_pair(self, a_l4: str, b_l4: str) -> KnownPair | None:
        return self.known_class_pairs.get((a_l4, b_l4))

    def reaction_for_pair(self, a_l4: str, b_l4: str) -> str:
        k = self.known_pair(a_l4, b_l4)
        if k is not None:
            return k.reaction
        for r in self.reactions:
            if a_l4 in r.index_l4:
                for t in self.treatments_by_reaction.get(r.reaction_id, []):
                    if b_l4 in t.marker_l4:
                        return r.reaction_name
        return UNKNOWN_REACTION

    def is_indicated(self, condition: str, drug_l4: str) -> bool:
        return (condition.lower(), drug_l4) in self.condition_classes

    def reactions_list(self) -> list[str]:
        names = {k.reaction for k in self.known}
        names.update(r.reaction_name for r in self.reactions)
        return sorted(names)

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


def read_reactions(path: Path) -> list[Reaction]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [
            Reaction(
                reaction_id=r["reaction_id"],
                reaction_name=r["reaction_name"],
                index_l4=_split(r["index_atc_l4"]),
                index_drugs=_split(r["index_drugs"]),
                lag_min=int(r["lag_min_days"]),
                lag_max=int(r["lag_max_days"]),
                p_reaction=float(r["p_reaction"]),
                source=r["source"],
            )
            for r in csv.DictReader(f)
        ]


def read_treatments(path: Path) -> list[Treatment]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [
            Treatment(
                treatment_id=r["treatment_id"],
                reaction_id=r["reaction_id"],
                marker_l4=_split(r["marker_atc_l4"]),
                marker_drugs=_split(r["marker_drugs"]),
                p_treatment=float(r["p_treatment"]),
                source=r["source"],
            )
            for r in csv.DictReader(f)
        ]


def read_indications(path: Path) -> list[Indication]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [
            Indication(
                condition_id=r["condition_id"],
                condition_name=r["condition_name"],
                atc_l4=r["atc_l4"],
                drugs=_split(r["drug_names"]),
                first_line=r.get("first_line", "1") == "1",
                typical_age_min=int(r.get("typical_age_min", "0")),
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
    for r in cat.reactions:
        for name in r.index_drugs:
            if name.lower() not in cat.by_name:
                problems.append(f"reaction {r.reaction_id}: drug {name!r} missing from catalogue")
        if not 0 < r.p_reaction < 1 or r.lag_min > r.lag_max:
            problems.append(f"reaction {r.reaction_id}: invalid p_reaction or lag range")
    for t in cat.treatments:
        if t.reaction_id not in cat.reactions_by_id:
            problems.append(f"treatment {t.treatment_id}: reaction {t.reaction_id} not found in reactions")
        for name in t.marker_drugs:
            if name.lower() not in cat.by_name:
                problems.append(f"treatment {t.treatment_id}: drug {name!r} missing from catalogue")
    if any(d.is_fictional and d.drug_id != "DNOVA" for d in cat.drugs):
        problems.append("only Novacor may be fictional")
    if any("novacor" in [n.lower() for n in k.index_drugs + k.marker_drugs] for k in cat.known):
        problems.append("Novacor must not be in cascades_known.csv")
    return problems


@lru_cache(maxsize=4)
def load_catalogue(data_dir: Path | None = None) -> Catalogue:
    data_dir = data_dir or get_settings().data_dir
    cat = Catalogue(
        read_drugs(data_dir / "drug_catalogue.csv"),
        read_known(data_dir / "cascades_known.csv"),
        read_reactions(data_dir / "reactions.csv"),
        read_treatments(data_dir / "treatments.csv"),
        read_indications(data_dir / "indications.csv"),
    )
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
    print(f"extended schema OK: {len(cat.reactions)} reactions, {len(cat.treatments)} treatments, "
          f"{len(cat.indications)} indications")


if __name__ == "__main__":
    main()

