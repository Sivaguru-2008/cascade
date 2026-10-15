"""Human-readable reasons for a Loop 1 score (section 5.5).

Primary: SHAP contributions from M1 (XGBoost's built-in TreeSHAP, `pred_contribs=True`, so no
separate `shap` package is needed). Fallback: M2 logistic-regression coefficient x standardised value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from datagen.catalogue import Catalogue

POPULATION = "population_signal"
GROUP = {"pair_aSR_live": POPULATION, "pair_aSR_lower_ci": POPULATION}


def _fmt_ratio(x: float | None) -> str:
    if x is None:
        return "?"
    return "∞" if x >= 999 else f"{x:.1f}"


def reason_text(feature: str, f: Mapping[str, Any], cat: Catalogue, b_drug_id: str) -> str:
    a = cat.by_id.get(f["_A_drug_id"])
    b = cat.by_id.get(b_drug_id)
    an = a.display_name if a else f["_A_drug_id"]
    bn = b.display_name if b else b_drug_id
    if feature == "days_since_A_start":
        return f"Started {f['days_since_A_start']} days after {an.lower() if a and not a.is_fictional else an}"
    if feature == "B_is_marker_class":
        return (f"{bn} is a common side-effect treatment" if f["B_is_marker_class"]
                else f"{bn} is not a typical side-effect treatment")
    if feature == "pair_in_known_list":
        known = cat.known_by_id.get(f.get("_known_pair_id") or "")
        return (f"Pair is on a published cascade list ({known.source_short})" if known
                else "Pair is not on a published cascade list")
    if feature == POPULATION:
        if not f.get("_aSR_n"):
            return "No population signal for this pair yet"
        return (f"Population signal aSR {_fmt_ratio(f['pair_aSR_live'])} "
                f"({_fmt_ratio(f['pair_aSR_lower_ci'])}–{_fmt_ratio(f.get('_aSR_hi'))})")
    if feature == "different_prescriber":
        return (f"Prescribed by a different prescriber than {an}" if f["different_prescriber"]
                else f"Same prescriber as {an}")
    if feature == "n_active_drugs":
        return f"{f['n_active_drugs']} active medicines"
    if feature == "age_band_ordinal":
        return f"Age band {f.get('_age_band', f['age_band_ordinal'])}"
    if feature == "sex":
        return f"Patient sex {f['sex']}"
    if feature == "A_atc_l3":
        return f"{an} drug class ({f['A_atc_l3']})"
    if feature == "B_atc_l3":
        return f"{bn} drug class ({f['B_atc_l3']})"
    if feature == "A_dose_increase_30d":
        return (f"{an} dose increased in the last 30 days" if f["A_dose_increase_30d"]
                else f"No recent {an} dose increase")
    if feature == "B_started_before_A_ever":
        return f"{bn} was used before {an}" if f["B_started_before_A_ever"] else f"{bn} not used before {an}"
    return feature


def group_contributions(columns: Sequence[str], contrib: np.ndarray) -> dict[str, float]:
    out: dict[str, float] = {}
    for col, c in zip(columns, contrib, strict=True):
        base = col.split("=", 1)[0]
        base = GROUP.get(base, base)
        out[base] = out.get(base, 0.0) + float(c)
    return out


def top_reasons(contribs: Mapping[str, float], f: Mapping[str, Any], cat: Catalogue, b_drug_id: str,
                k: int = 3) -> list[str]:
    ranked = sorted(contribs.items(), key=lambda kv: -kv[1])
    return [reason_text(name, f, cat, b_drug_id) for name, _ in ranked[:k]]


def xgb_contributions(booster: Any, columns: Sequence[str], x: np.ndarray) -> dict[str, float]:
    import xgboost as xgb

    contrib = booster.predict(xgb.DMatrix(x.reshape(1, -1), feature_names=list(columns)), pred_contribs=True)[0]
    return group_contributions(columns, contrib[:-1])  # last column is the bias


def linear_contributions(m2: Any, columns: Sequence[str], x: np.ndarray) -> dict[str, float]:
    scaler, lr = m2.named_steps["scale"], m2.named_steps["lr"]
    z = (x - scaler.mean_) / np.where(scaler.scale_ > 0, scaler.scale_, 1.0)
    return group_contributions(columns, lr.coef_[0] * z)
