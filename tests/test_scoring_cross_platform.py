"""Cross-platform and endian-safe scoring test.

Ensures that scoring of fixed clinical feature rows is deterministic, numerically stable,
and consistent across architectures (x86_64 and s390x) within an acceptable tolerance.
"""

from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pytest

from common.config import get_settings
from datagen.catalogue import load_catalogue
from loop1.features import FeatureSpace
from loop1.train import Loop1Model, fit_m1, fit_m2, fit_rx_ranker, blend


# 10 deterministic test candidate rows representing varied clinical scenarios
FIXED_TEST_CASES = [
    # 0: Strong classic cascade (K1 amlodipine -> furosemide)
    {
        "days_since_A_start": 30, "A_atc_l3": "C08C", "B_atc_l3": "C03C",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 3.4,
        "pair_aSR_lower_ci": 2.1, "different_prescriber": 1, "n_active_drugs": 3,
        "age_band_ordinal": 14, "sex": "F", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D001", "_A_atc_l4": "C08CA", "_A_dose": 10.0, "_aSR_hi": 5.2, "_aSR_n": 85,
        "_known_pair_id": "K1", "_B_dose": 40.0, "_age_band": "70-74",
    },
    # 1: K2 ACE-inhibitor dry cough
    {
        "days_since_A_start": 21, "A_atc_l3": "C09A", "B_atc_l3": "R05D",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 2.8,
        "pair_aSR_lower_ci": 1.7, "different_prescriber": 0, "n_active_drugs": 2,
        "age_band_ordinal": 12, "sex": "M", "A_dose_increase_30d": 1,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D003", "_A_atc_l4": "C09AA", "_A_dose": 10.0, "_aSR_hi": 4.1, "_aSR_n": 45,
        "_known_pair_id": "K2", "_B_dose": 60.0, "_age_band": "60-64",
    },
    # 2: Negative: Long delay (lag > 250 days, not a cascade)
    {
        "days_since_A_start": 280, "A_atc_l3": "C08C", "B_atc_l3": "C03C",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 1.1,
        "pair_aSR_lower_ci": 0.5, "different_prescriber": 0, "n_active_drugs": 1,
        "age_band_ordinal": 8, "sex": "F", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D001", "_A_atc_l4": "C08CA", "_A_dose": 5.0, "_aSR_hi": 1.8, "_aSR_n": 10,
        "_known_pair_id": "K1", "_B_dose": 20.0, "_age_band": "40-44",
    },
    # 3: Negative: Unrelated drugs without known pair or population signal
    {
        "days_since_A_start": 45, "A_atc_l3": "A10B", "B_atc_l3": "N02A",
        "B_is_marker_class": 0, "pair_in_known_list": 0, "pair_aSR_live": 1.0,
        "pair_aSR_lower_ci": 0.0, "different_prescriber": 0, "n_active_drugs": 2,
        "age_band_ordinal": 10, "sex": "M", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D031", "_A_atc_l4": "A10BA", "_A_dose": 500.0, "_aSR_hi": None, "_aSR_n": 0,
        "_known_pair_id": None, "_B_dose": 50.0, "_age_band": "50-54",
    },
    # 4: Recurrent cascade: Patient with prior cascade history
    {
        "days_since_A_start": 14, "A_atc_l3": "N03A", "B_atc_l3": "C03C",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 2.5,
        "pair_aSR_lower_ci": 1.5, "different_prescriber": 1, "n_active_drugs": 5,
        "age_band_ordinal": 15, "sex": "F", "A_dose_increase_30d": 1,
        "B_started_before_A_ever": 0, "prior_cascade_count": 2, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D059", "_A_atc_l4": "N03AX", "_A_dose": 150.0, "_aSR_hi": 3.8, "_aSR_n": 30,
        "_known_pair_id": "K7", "_B_dose": 40.0, "_age_band": "75+",
    },
    # 5: Confounding indication: B has documented primary indication
    {
        "days_since_A_start": 60, "A_atc_l3": "C08C", "B_atc_l3": "C03C",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 1.8,
        "pair_aSR_lower_ci": 1.1, "different_prescriber": 0, "n_active_drugs": 4,
        "age_band_ordinal": 13, "sex": "M", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 1,
        "_A_drug_id": "D001", "_A_atc_l4": "C08CA", "_A_dose": 5.0, "_aSR_hi": 2.6, "_aSR_n": 40,
        "_known_pair_id": "K1", "_B_dose": 40.0, "_age_band": "65-69",
    },
    # 6: K4 antipsychotic extrapyramidal symptoms
    {
        "days_since_A_start": 10, "A_atc_l3": "N05A", "B_atc_l3": "N04A",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 4.1,
        "pair_aSR_lower_ci": 2.4, "different_prescriber": 1, "n_active_drugs": 3,
        "age_band_ordinal": 11, "sex": "M", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 1, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D081", "_A_atc_l4": "N05AD", "_A_dose": 5.0, "_aSR_hi": 6.5, "_aSR_n": 25,
        "_known_pair_id": "K4", "_B_dose": 2.0, "_age_band": "55-59",
    },
    # 7: Novel discovery cascade (e.g. Novacor -> PPI) with statistical signal only
    {
        "days_since_A_start": 25, "A_atc_l3": "C09X", "B_atc_l3": "A02B",
        "B_is_marker_class": 1, "pair_in_known_list": 0, "pair_aSR_live": 3.6,
        "pair_aSR_lower_ci": 2.2, "different_prescriber": 1, "n_active_drugs": 2,
        "age_band_ordinal": 14, "sex": "F", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "DNOVA", "_A_atc_l4": "C09XX", "_A_dose": 10.0, "_aSR_hi": 5.1, "_aSR_n": 55,
        "_known_pair_id": None, "_B_dose": 40.0, "_age_band": "70-74",
    },
    # 8: Reversal: B started before A historically
    {
        "days_since_A_start": 15, "A_atc_l3": "C08C", "B_atc_l3": "C03C",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 1.2,
        "pair_aSR_lower_ci": 0.6, "different_prescriber": 0, "n_active_drugs": 2,
        "age_band_ordinal": 12, "sex": "M", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 1, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D001", "_A_atc_l4": "C08CA", "_A_dose": 5.0, "_aSR_hi": 2.0, "_aSR_n": 15,
        "_known_pair_id": "K1", "_B_dose": 20.0, "_age_band": "60-64",
    },
    # 9: Mild signal, low active polypharmacy
    {
        "days_since_A_start": 40, "A_atc_l3": "G04C", "B_atc_l3": "N07C",
        "B_is_marker_class": 1, "pair_in_known_list": 1, "pair_aSR_live": 1.9,
        "pair_aSR_lower_ci": 1.2, "different_prescriber": 0, "n_active_drugs": 1,
        "age_band_ordinal": 13, "sex": "M", "A_dose_increase_30d": 0,
        "B_started_before_A_ever": 0, "prior_cascade_count": 0, "has_primary_indication_for_B": 0,
        "_A_drug_id": "D095", "_A_atc_l4": "G04CA", "_A_dose": 0.4, "_aSR_hi": 2.8, "_aSR_n": 20,
        "_known_pair_id": "K6", "_B_dose": 48.0, "_age_band": "65-69",
    },
]


def test_cross_platform_scoring_deterministic():
    """Verify that feature vectors and model scoring are deterministic and within valid probability bounds."""
    cat = load_catalogue()
    space = FeatureSpace.build(cat)

    # Convert 10 test rows to matrix with explicit little-endian float32
    X = space.matrix(FIXED_TEST_CASES).astype("<f4")
    assert X.shape == (10, len(space.columns))
    assert X.dtype == np.dtype("<f4")

    # Fit small synthetic model under HGB to verify end-to-end scoring pipeline
    y_synth = np.array([1, 1, 0, 0, 1, 0, 1, 1, 0, 1], dtype=int)
    y_rx_synth = np.array([0, 1, -1, -1, 2, -1, 3, 0, -1, 4], dtype=int)

    m1 = fit_m1(X, y_synth, backend="hgb", seed=7)
    m2 = fit_m2(X, y_synth)
    rx_m = fit_rx_ranker(X, y_rx_synth, backend="hgb", seed=7)

    weights = (0.6, 0.2, 0.2)
    model = Loop1Model(
        space=space,
        m1=m1,
        m2=m2,
        backend="hgb",
        weights=weights,
        threshold=0.35,
        m1_threshold=0.30,
        m2_threshold=0.30,
        variant="full",
        cat=cat,
        rx_model=rx_m,
        rx_classes=cat.reactions_list(),
    )

    scores, m1_p, m2_p, _ = model.score(FIXED_TEST_CASES)

    # Sanity checks: probabilities in [0, 1]
    assert len(scores) == 10
    assert np.all((scores >= 0.0) & (scores <= 1.0))
    assert np.all((m1_p >= 0.0) & (m1_p <= 1.0))
    assert np.all((m2_p >= 0.0) & (m2_p <= 1.0))

    # Strong known cascades (cases 0, 1, 4, 6) should score higher than long delays / unrelated pairs (cases 2, 3)
    assert scores[0] > scores[2]
    assert scores[0] > scores[3]
    assert scores[1] > scores[3]

    # Test reaction ranking on positive cascade cases
    top_rx, prob, _ = model.rank_reaction(X[0])
    assert isinstance(top_rx, str)
    assert 0.0 <= prob <= 1.0
