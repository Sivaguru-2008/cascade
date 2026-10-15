"""Validator for generated text (section 7.4). Returns (ok, reasons[]).

Rejects when:
  * a drug name, number or date in the output is not present in the input JSON;
  * the output contains a banned phrase (stop / discontinue / diagnose / definitely / must and their forms);
  * the PvPI JSON fails its Pydantic schema;
  * the note is longer than 90 words;
  * (stricter than the spec) the note misses a required element of 7.2: both drugs, the gap in days,
    the suspected reaction, the evidence source, or the closing request to review.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from common.schemas import DraftInput, PvpiDraft

MAX_NOTE_WORDS = 90
BANNED = re.compile(r"\b(stop\w*|discontinu\w*|diagnos\w*|definite\w*|must)\b", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
DATE = re.compile(rf"\b\d{{4}}-\d{{1,2}}-\d{{1,2}}\b|\b\d{{1,2}}[/.]\d{{1,2}}[/.]\d{{2,4}}\b|"
                  rf"\b\d{{1,2}}\s+(?:{MONTHS})\.?(?:\s+\d{{2,4}})?\b|\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?"
                  rf"(?:,?\s+\d{{2,4}})?\b|\b(?:{MONTHS})\.?\s+\d{{4}}\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def _drug_names() -> tuple[str, ...]:
    from datagen.catalogue import load_catalogue

    names = {d.generic_name.lower() for d in load_catalogue().drugs}
    # also match the first word of multi-word names (e.g. "levodopa", "potassium" is too generic: skip <6 chars)
    names |= {n.split()[0] for n in list(names) if " " in n and len(n.split()[0]) >= 6}
    return tuple(sorted(names, key=len, reverse=True))


def _flatten(obj: Any) -> Iterable[Any]:
    if isinstance(obj, Mapping):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj


def input_numbers(inp: Mapping[str, Any]) -> set[float]:
    nums: set[float] = set()
    for v in _flatten(inp):
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int | float):
            nums.add(round(float(v), 6))
        elif isinstance(v, str):
            nums |= {round(float(m), 6) for m in NUMBER.findall(v)}
    return nums


def input_text(inp: Mapping[str, Any]) -> str:
    return json.dumps(inp, ensure_ascii=False).lower()


def check_text(text: str, inp: Mapping[str, Any], field: str) -> list[str]:
    reasons: list[str] = []
    for m in BANNED.finditer(text):
        reasons.append(f"{field}: banned phrase '{m.group(0)}'")
    allowed = input_numbers(inp)
    for d in DATE.findall(text):
        if d.lower() not in input_text(inp):
            reasons.append(f"{field}: date '{d}' not in input")
    for n in NUMBER.findall(text):
        if round(float(n), 6) not in allowed:
            reasons.append(f"{field}: number '{n}' not in input")
    src = input_text(inp)
    low = text.lower()
    for name in _drug_names():
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", low) and name not in src:
            reasons.append(f"{field}: drug '{name}' not in input")
    return reasons


def _name(drug_with_dose: str) -> str:
    """'Amlodipine 5 mg' -> 'amlodipine'."""
    return re.sub(r"\s+\d.*$", "", drug_with_dose).strip().lower()


def validate_note(note: str, inp: DraftInput) -> list[str]:
    d = inp.model_dump()
    reasons = check_text(note, d, "note")
    words = len(note.split())
    if words > MAX_NOTE_WORDS:
        reasons.append(f"note: {words} words > {MAX_NOTE_WORDS}")
    low = note.lower()
    for label, needle in (("index drug", _name(inp.index_drug)), ("marker drug", _name(inp.marker_drug))):
        if needle not in low:
            reasons.append(f"note: missing {label}")
    if not re.search(rf"(?<!\d){inp.days_between}(?!\d)\s*days?", low):
        reasons.append("note: missing gap in days")
    reaction = inp.suspected_reaction.lower()
    if not (reaction in low or ("unknown" in reaction and "unknown" in low)):
        reasons.append("note: missing suspected reaction")
    source = (inp.evidence.known_source or "").lower()
    if source and source.split(" (")[0] not in low:
        reasons.append("note: missing evidence source")
    if not source and "population signal" not in low:
        reasons.append("note: missing evidence source")
    last = re.split(r"(?<=[.?!])\s+", note.strip())[-1].lower()
    if "review" not in last:
        reasons.append("note: does not end with a request to review")
    if "possible" not in low and "possibly" not in low:
        reasons.append("note: does not say 'possible'")
    return reasons


def validate_report(report: Any, inp: DraftInput) -> list[str]:
    try:
        model = report if isinstance(report, PvpiDraft) else PvpiDraft.model_validate(report)
    except ValidationError as exc:
        return [f"report: schema error: {e['loc']} {e['msg']}" for e in exc.errors()]
    d = inp.model_dump()
    reasons: list[str] = []
    for path, value in _strings(model.model_dump(exclude={"form", "disclaimer"})):
        reasons += check_text(value, d, f"report.{path}")
    return reasons


def _strings(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def validate(note: str, report: Any, inp: DraftInput) -> tuple[bool, list[str]]:
    reasons = validate_note(note, inp) + validate_report(report, inp)
    return (not reasons), reasons
