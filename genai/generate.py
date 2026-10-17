"""Generative layer (section 7): prescriber note + PvPI draft, validated, with a template fallback.

Backends (GENAI_BACKEND): template | lab | transformers | llamacpp.
  * template      Jinja2, always available, deterministic.
  * lab           an OpenAI-compatible chat endpoint (GENAI_LAB_URL), e.g. the model server used in the
                  IBM "Predictive + Generative AI" lab; its exact API must be confirmed at kickoff.
  * transformers  a small instruct model (GENAI_MODEL, default IBM Granite 2B) via Hugging Face transformers.
  * llamacpp      a GGUF model via llama-cpp-python (LLAMACPP_MODEL_PATH).
Every LLM output passes the validator; on rejection, error or a 10 s timeout we fall back to the
template. Only the DraftInput contract (no tokens, pharmacy or prescriber IDs) reaches the model.

    uvicorn genai.generate:app --port 8003      # POST /draft, GET /health
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc  # type: ignore
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel

from common.config import Settings, get_settings
from common.crypto import Cipher, cipher_from_settings
from common.db import Database
from common.logging import get_logger
from common.schemas import (
    Alert,
    DraftEvidence,
    DraftInput,
    DraftPatient,
    DraftResult,
    PvpiConcomitant,
    PvpiDraft,
    PvpiPatient,
    PvpiReaction,
    PvpiReporter,
    PvpiSuspectDrug,
)
from genai.validate import validate

log = get_logger("genai")
HERE = Path(__file__).resolve().parent
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="genai")


def fmt_num(x: float) -> str:
    return f"{x:g}" if abs(x) >= 0.01 else f"{x:.3g}"


def drug_with_dose(name: str, dose: float | None) -> str:
    return f"{name} {fmt_num(dose)} mg" if dose else name


def build_input(alert: Alert) -> DraftInput:
    """Section 7.1 contract. Ratios are rounded to one decimal (as displayed); an upper CI at the 999 cap is null."""
    lo, hi = (alert.pair_aSR_ci + [None, None])[:2]
    return DraftInput(
        alert_id=alert.alert_id,
        index_drug=drug_with_dose(alert.index_drug, alert.index_dose_mg),
        marker_drug=drug_with_dose(alert.marker_drug, alert.marker_dose_mg),
        days_between=alert.days_between,
        suspected_reaction=alert.suspected_reaction,
        evidence=DraftEvidence(
            score=round(alert.score, 2),
            aSR=None if alert.pair_aSR is None else round(min(alert.pair_aSR, 999.0), 1),
            aSR_ci=[None if lo is None else round(lo, 1), None if hi is None or hi >= 999 else round(hi, 1)],
            known_source=alert.known_source),
        patient=DraftPatient(age_band=alert.age_band, sex=alert.sex if alert.sex in ("M", "F", "U") else "U",
                             n_active_drugs=alert.n_active_drugs),
        different_prescriber=alert.different_prescriber,
    )


# ----------------------------------------------------------------------------- template backend
@lru_cache(maxsize=1)
def _jinja() -> Environment:
    return Environment(loader=FileSystemLoader(str(HERE / "templates")), undefined=StrictUndefined,
                       autoescape=False, keep_trailing_newline=False)


def _evidence_phrase(inp: DraftInput) -> str:
    e = inp.evidence
    parts = []
    if e.known_source:
        parts.append(f"listed in {e.known_source}")
    if e.aSR is not None:
        lo, hi = e.aSR_ci
        ci = f"CI {lo:g}–{hi:g}" if lo is not None and hi is not None else (
            f"lower bound {lo:g}" if lo is not None else "")
        parts.append(f"population signal aSR {e.aSR:g}" + (f" ({ci})" if ci else ""))
    else:
        parts.append("population signal not yet estimated")
    if not e.known_source:
        parts.append("not on a published cascade list")
    return "; ".join(parts)


def _reaction_phrase(inp: DraftInput) -> str:
    r = inp.suspected_reaction
    return r if r.lower().startswith("unknown") else f"possible {r[0].lower()}{r[1:]}"


def render_template(inp: DraftInput) -> tuple[str, str]:
    env = _jinja()
    ctx = {"i": inp, "p": inp.patient, "sex_word": {"F": "female", "M": "male"}.get(inp.patient.sex, "sex unknown"),
           "reaction": _reaction_phrase(inp), "evidence": _evidence_phrase(inp)}
    note = " ".join(env.get_template("note.j2").render(**ctx).split())
    if inp.suspected_reaction.lower().startswith("unknown"):
        reaction_cap = "Possible adverse reaction of unknown type (pattern detected statistically)"
    else:
        reaction_cap = f"Possible {inp.suspected_reaction[0].lower()}{inp.suspected_reaction[1:]}"
    narrative = " ".join(env.get_template("pvpi.j2").render(**ctx, reaction_cap=reaction_cap).split())
    return note, narrative


def build_report(inp: DraftInput, narrative: str) -> PvpiDraft:
    def dose_of(s: str) -> str | None:
        m = re.search(r"(\d+(?:\.\d+)?) mg$", s)
        return f"{m.group(1)} mg per day" if m else None

    sex = {"F": "Female", "M": "Male"}.get(inp.patient.sex)
    return PvpiDraft(
        A_patient=PvpiPatient(age_at_onset=f"{inp.patient.age_band} years", sex=sex),
        B_reaction=PvpiReaction(reaction_description=narrative),
        C_suspected_medications=[PvpiSuspectDrug(drug_name=inp.index_drug, dose=dose_of(inp.index_drug))],
        concomitant_medications=[PvpiConcomitant(drug_name=inp.marker_drug, dose=dose_of(inp.marker_drug))],
        D_reporter=PvpiReporter(),
    )


# ----------------------------------------------------------------------------- LLM backends
def _prompt(name: str, inp: DraftInput) -> str:
    return (HERE / "prompts" / name).read_text(encoding="utf-8").format(
        input_json=json.dumps(inp.model_dump(), ensure_ascii=False))


def _parse_json(text: str, key: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    value = json.loads(m.group(0))[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty {key}")
    return " ".join(value.split())


class LLMBackend:
    name = "llm"

    def complete(self, prompt: str, s: Settings) -> str:
        raise NotImplementedError


class LabBackend(LLMBackend):
    """OpenAI-compatible /v1/chat/completions (vLLM, llama.cpp server, Ollama and others speak it)."""

    name = "lab"

    def complete(self, prompt: str, s: Settings) -> str:
        import os

        import httpx

        if not s.genai_lab_url:
            raise RuntimeError("GENAI_LAB_URL is not set")
        headers = {}
        if os.environ.get("GENAI_LAB_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['GENAI_LAB_API_KEY']}"
        r = httpx.post(s.genai_lab_url.rstrip("/") + "/v1/chat/completions", headers=headers, timeout=s.genai_timeout_s,
                       json={"model": s.genai_lab_model, "messages": [{"role": "user", "content": prompt}],
                             "temperature": s.genai_temperature, "max_tokens": s.genai_max_tokens})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class TransformersBackend(LLMBackend):
    name = "transformers"
    _pipe: Any = None

    def complete(self, prompt: str, s: Settings) -> str:
        if TransformersBackend._pipe is None:
            from transformers import pipeline

            TransformersBackend._pipe = pipeline("text-generation", model=s.genai_model)
        out = TransformersBackend._pipe([{"role": "user", "content": prompt}], max_new_tokens=s.genai_max_tokens,
                                        do_sample=False, return_full_text=False)  # greedy = temperature 0
        return out[0]["generated_text"]


class LlamaCppBackend(LLMBackend):
    name = "llamacpp"
    _llm: Any = None

    def complete(self, prompt: str, s: Settings) -> str:
        if LlamaCppBackend._llm is None:
            from llama_cpp import Llama

            LlamaCppBackend._llm = Llama(model_path=s.llamacpp_model_path, n_ctx=2048, verbose=False)
        out = LlamaCppBackend._llm.create_chat_completion(messages=[{"role": "user", "content": prompt}],
                                                          temperature=s.genai_temperature,
                                                          max_tokens=s.genai_max_tokens)
        return out["choices"][0]["message"]["content"]


BACKENDS: dict[str, type[LLMBackend]] = {"lab": LabBackend, "transformers": TransformersBackend,
                                         "llamacpp": LlamaCppBackend}


async def _call(backend: LLMBackend, prompt: str, s: Settings, timeout: float) -> str:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_executor, backend.complete, prompt, s), timeout)


async def generate_draft(inp: DraftInput, backend: str | None = None, settings: Settings | None = None) -> DraftResult:
    s = settings or get_settings()
    name = backend or s.genai_backend
    t0 = time.perf_counter()
    reject: list[str] = []
    if name in BACKENDS:
        try:
            impl = BACKENDS[name]()
            deadline = t0 + s.genai_timeout_s
            note = _parse_json(await _call(impl, _prompt("note.txt", inp), s, s.genai_timeout_s), "note")
            narrative = _parse_json(await _call(impl, _prompt("pvpi.txt", inp), s,
                                                max(0.1, deadline - time.perf_counter())), "reaction_description")
            report = build_report(inp, narrative)
            ok, reasons = validate(note, report, inp)
            if ok:
                return DraftResult(alert_id=inp.alert_id, note_text=note, report=report, generator=f"llm:{name}",
                                   validator_pass=True, gen_ms=round((time.perf_counter() - t0) * 1000, 1))
            reject = [f"llm:{name} rejected: {r}" for r in reasons]
        except TimeoutError:
            reject = [f"llm:{name} timed out after {s.genai_timeout_s:g} s"]
        except Exception as exc:  # backend missing, network, parse errors -> template
            reject = [f"llm:{name} error: {type(exc).__name__}: {exc}"[:300]]
        log.info("falling back to template", extra={"alert_id": inp.alert_id, "reasons": reject[:3]})
    elif name != "template":
        reject = [f"unknown GENAI_BACKEND {name!r}"]
    note, narrative = render_template(inp)
    report = build_report(inp, narrative)
    ok, reasons = validate(note, report, inp)
    return DraftResult(alert_id=inp.alert_id, note_text=note, report=report,
                       generator="template" if name == "template" else "template (fallback)",
                       validator_pass=ok, reject_reasons=reject + reasons,
                       gen_ms=round((time.perf_counter() - t0) * 1000, 1))


# ----------------------------------------------------------------------------- storage
def store_draft(db: Database, cipher: Cipher, d: DraftResult) -> None:
    db.execute(
        "INSERT INTO drafts (alert_id, note_text, report_json, generator, validator_pass, reject_reasons, gen_ms, "
        "created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (alert_id) DO UPDATE SET note_text = excluded.note_text, "
        "report_json = excluded.report_json, generator = excluded.generator, validator_pass = excluded.validator_pass, "
        "reject_reasons = excluded.reject_reasons, gen_ms = excluded.gen_ms, created_ts = excluded.created_ts",
        (d.alert_id, cipher.encrypt_text(d.note_text, aad=d.alert_id),
         cipher.encrypt_json(d.report.model_dump(mode="json"), aad=d.alert_id), d.generator, int(d.validator_pass),
         json.dumps(d.reject_reasons), d.gen_ms, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))


def load_draft(db: Database, cipher: Cipher, alert_id: str) -> DraftResult | None:
    row = db.fetchone("SELECT note_text, report_json, generator, validator_pass, reject_reasons, gen_ms FROM drafts "
                      "WHERE alert_id = ?", (alert_id,))
    if row is None:
        return None
    return DraftResult(alert_id=alert_id, note_text=cipher.decrypt_text(row[0], aad=alert_id),
                       report=PvpiDraft.model_validate(cipher.decrypt_json(row[1], aad=alert_id)), generator=row[2],
                       validator_pass=bool(row[3]), reject_reasons=json.loads(row[4]), gen_ms=float(row[5]))


async def draft_and_store(db: Database, cipher: Cipher, inp: DraftInput) -> DraftResult:
    d = await generate_draft(inp)
    store_draft(db, cipher, d)
    return d


# ----------------------------------------------------------------------------- HTTP service
class DraftRequest(BaseModel):
    input: DraftInput


_state: dict[str, Any] = {}


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db = Database.connect()
    db.init_schema()
    _state.update(db=db, cipher=cipher_from_settings())
    yield


app = FastAPI(title="CASCADE genai", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "genai", "backend": get_settings().genai_backend}


@app.post("/draft", response_model=DraftResult)
async def draft(req: DraftRequest) -> DraftResult:
    return await draft_and_store(_state["db"], _state["cipher"], req.input)
