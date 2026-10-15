import asyncio
from datetime import datetime

import pytest

from common.schemas import Alert, DraftInput
from genai.generate import build_input, build_report, generate_draft, render_template
from genai.pdf import page_count, render_html, render_pdf
from genai.validate import MAX_NOTE_WORDS, validate, validate_note, validate_report


def _alert(**kw):
    base = dict(alert_id="AL-000123", event_id="e1", ts=datetime(2025, 3, 1, 10), pharmacy_id="PH001",
                patient_token="a" * 64, index_drug="Amlodipine", marker_drug="Furosemide", index_atc_l4="C08CA",
                marker_atc_l4="C03CA", days_between=18, score=0.912, reasons=["r1", "r2", "r3"], pair_aSR=3.44,
                pair_aSR_ci=[2.61, 4.48], known_source="ThinkCascades", index_drug_id="D001", marker_drug_id="D014",
                index_dose_mg=5.0, marker_dose_mg=40.0, suspected_reaction="Ankle oedema", age_band="70-74", sex="F",
                n_active_drugs=7, different_prescriber=True, pair_status="known")
    base.update(kw)
    return Alert(**base)


@pytest.fixture
def inp() -> DraftInput:
    return build_input(_alert())


def test_contract_has_no_identifiers(inp):
    d = inp.model_dump()
    blob = str(d)
    assert "a" * 64 not in blob and "PH001" not in blob and "e1" not in d.values()
    assert d["index_drug"] == "Amlodipine 5 mg" and d["marker_drug"] == "Furosemide 40 mg"
    assert d["evidence"] == {"score": 0.91, "aSR": 3.4, "aSR_ci": [2.6, 4.5], "known_source": "ThinkCascades"}


def test_template_output_passes(inp):
    note, narrative = render_template(inp)
    ok, reasons = validate(note, build_report(inp, narrative), inp)
    assert ok, reasons
    assert len(note.split()) <= MAX_NOTE_WORDS
    assert "possible" in note.lower() and "please review" in note.lower()
    assert note.rstrip().endswith("?") and "review" in note.split(".")[-1]


def test_template_passes_for_new_signal_without_source():
    inp = build_input(_alert(known_source=None, suspected_reaction="unknown — pattern detected statistically",
                             index_drug="Novacor", marker_drug="Pantoprazole", pair_aSR=8.05, pair_aSR_ci=[4.92, 999.0],
                             pair_status="new", index_dose_mg=10.0))
    note, narrative = render_template(inp)
    ok, reasons = validate(note, build_report(inp, narrative), inp)
    assert ok, reasons
    assert "not on a published cascade list" in note and "999" not in note


@pytest.mark.parametrize("word", ["stop", "Stopping", "discontinue", "diagnose", "definitely", "must"])
def test_banned_phrases(inp, word):
    note, _ = render_template(inp)
    reasons = validate_note(note.replace("This is a possible", f"You {word} consider; this is a possible"), inp)
    assert any("banned" in r for r in reasons)


def test_number_not_in_input_rejected(inp):
    note, _ = render_template(inp)
    reasons = validate_note(note.replace("18 days", "18 days (dose 80 mg)"), inp)
    assert any("number '80'" in r for r in reasons)


def test_drug_not_in_input_rejected(inp):
    note, _ = render_template(inp)
    reasons = validate_note(note + " Consider also ramipril.", inp)
    assert any("drug 'ramipril'" in r for r in reasons)


def test_date_not_in_input_rejected(inp):
    note, _ = render_template(inp)
    for d in ("2025-03-01", "12/03/2025", "3 March 2025", "March 3"):
        assert any("date" in r for r in validate_note(note.replace("Thank you", f"Seen on {d}. Thank you"), inp)), d


def test_too_long_rejected(inp):
    note, _ = render_template(inp)
    long = "Please note. " * 40 + note
    assert any("words" in r for r in validate_note(long, inp))


def test_missing_required_content(inp):
    reasons = validate_note("Dear Prescriber, possible issue. Could you please review?", inp)
    assert any("missing index drug" in r for r in reasons) and any("gap in days" in r for r in reasons)
    note, _ = render_template(inp)
    assert any("end with a request to review" in r for r in validate_note(note + " Thanks.", inp))


def test_report_schema_rejected(inp):
    assert any("schema" in r for r in validate_report({"A_patient": {}}, inp))
    _, narrative = render_template(inp)
    bad = build_report(inp, narrative).model_dump()
    bad["B_reaction"]["reaction_description"] = "Definitely caused by amlodipine; started 2024-01-01."
    reasons = validate_report(bad, inp)
    assert any("banned" in r for r in reasons) and any("date" in r for r in reasons)


def test_unknown_fields_are_null(inp):
    _, narrative = render_template(inp)
    r = build_report(inp, narrative)
    assert r.A_patient.patient_initials is None and r.A_patient.weight_kg is None
    assert r.C_suspected_medications[0].batch_lot_no is None and r.C_suspected_medications[0].manufacturer is None
    assert r.B_reaction.outcome is None and r.B_reaction.seriousness is None
    assert all(v is None for v in r.D_reporter.model_dump().values())


def test_llm_backend_failure_falls_back_to_template(inp, monkeypatch):
    monkeypatch.setenv("GENAI_LAB_URL", "http://127.0.0.1:9")  # nothing listens here
    from common.config import reload_settings

    s = reload_settings()
    d = asyncio.run(generate_draft(inp, backend="lab", settings=s))
    assert d.generator == "template (fallback)" and d.validator_pass
    assert any("llm:lab error" in r for r in d.reject_reasons)
    monkeypatch.delenv("GENAI_LAB_URL")
    reload_settings()


def test_llm_output_rejected_by_validator_falls_back(inp, monkeypatch):
    from genai import generate as g

    class Bad(g.LLMBackend):
        def complete(self, prompt, s):
            if "reaction_description" in prompt.split("Reply with JSON only")[-1]:
                return '{"reaction_description": "Possible ankle oedema."}'
            return '{"note": "Furosemide 40 mg must be stopped now."}'

    monkeypatch.setitem(g.BACKENDS, "fake", Bad)
    d = asyncio.run(generate_draft(inp, backend="fake"))
    assert d.generator == "template (fallback)" and d.validator_pass
    assert any("banned phrase 'must'" in r for r in d.reject_reasons)


def test_llm_good_output_accepted(inp, monkeypatch):
    from genai import generate as g

    good_note, narrative = render_template(inp)

    class Good(g.LLMBackend):
        def complete(self, prompt, s):
            if "reaction_description" in prompt.split("Reply with JSON only")[-1]:
                return f'{{"reaction_description": "{narrative}"}}'
            return f'{{"note": "{good_note}"}}'

    monkeypatch.setitem(g.BACKENDS, "fake", Good)
    d = asyncio.run(generate_draft(inp, backend="fake"))
    assert d.generator == "llm:fake" and d.validator_pass and d.note_text == good_note


def test_pdf_is_one_page_and_labelled(inp):
    d = asyncio.run(generate_draft(inp, backend="template"))
    pdf = render_pdf(d, {"approved_ts": "2026-10-17T10:00:00Z"})
    assert pdf.startswith(b"%PDF") and page_count(pdf) == 1
    html = render_html(d, {})
    assert "DRAFT — synthetic demo data — requires reporter completion" in html
    assert "To be completed by reporter" in html
