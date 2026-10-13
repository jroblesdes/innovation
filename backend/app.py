"""Bed Placement Review: FastAPI backend + static frontend.

Run from the project folder:
    uvicorn backend.app:app --reload
then open http://localhost:8000
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .data_store import TransferRequest, get_store
from .matcher import recommend
from .summarize_patient_encounter import parse_datetime
from .summarizer import ATTRIBUTE_FIELDS, SummaryError, get_summary, model_name

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
log = logging.getLogger(__name__)

app = FastAPI(title="Bed Placement Review", version="1.1")

if not os.getenv("OPENAI_API_KEY"):
    log.warning("OPENAI_API_KEY is not set: summaries will fail until it is added to .env")


def _request_or_404(transfer_id: str) -> TransferRequest:
    req = get_store().transfers.get(transfer_id)
    if req is None:
        raise HTTPException(404, f"Transfer request {transfer_id} not found")
    return req


# ---------------------------------------------------------------------------
# Vital signs
# Each vital is {"value", "unit", "time", "source"} or None when the chart has
# no reading at or before the transfer time. Heart rate and respiratory rate
# are part of the response so the UI is ready when that data is added; the
# current charts do not contain them, so they are None.
# ---------------------------------------------------------------------------

def _latest_lab(encounter: dict[str, Any], label: str, cutoff: datetime | None, specimen: str | None = None):
    """Latest numeric lab at or before cutoff. specimen="ART." keeps arterial blood gases only."""
    specimen_types = {
        r.get("specimen_id"): str(r.get("value") or "").upper()
        for r in encounter.get("labs", [])
        if r.get("label") == "Specimen Type"
    }
    best = None
    for row in encounter.get("labs", []):
        if row.get("label") != label or row.get("valuenum") is None:
            continue
        if specimen and specimen_types.get(row.get("specimen_id")) != specimen:
            continue
        when = parse_datetime(row.get("charttime"))
        if when is None or (cutoff and when > cutoff):
            continue
        if best is None or when > best[0]:
            best = (when, row)
    return best


def _latest_omr(encounter: dict[str, Any], names: tuple[str, ...], cutoff: datetime | None):
    best = None
    for row in encounter.get("omr", []):
        if row.get("result_name") not in names:
            continue
        when = parse_datetime(row.get("chartdate"))
        if when is None or (cutoff and when.date() > cutoff.date()):
            continue
        key = (when, row.get("seq_num") or 0)
        if best is None or key > best[0]:
            best = (key, row)
    return best


def _vital(value: Any, unit: str | None, when: datetime, source: str) -> dict[str, Any]:
    return {"value": value, "unit": unit, "time": when.isoformat(sep=" "), "source": source}


def vital_signs(encounter: dict[str, Any], cutoff: datetime | None) -> dict[str, Any]:
    vitals: dict[str, Any] = {"heart_rate": None, "respiratory_rate": None}

    bp = _latest_omr(encounter, ("Blood Pressure", "Blood Pressure Sitting",
                                 "Blood Pressure Lying", "Blood Pressure Standing (3 mins)"), cutoff)
    vitals["blood_pressure"] = (
        _vital(str(bp[1]["result_value"]), "mmHg", bp[0][0], bp[1]["result_name"]) if bp else None
    )

    temp = _latest_lab(encounter, "Temperature", cutoff)
    vitals["temperature"] = (
        _vital(round(float(temp[1]["valuenum"]), 1), "°C", temp[0], "Blood gas") if temp else None
    )

    # arterial only: venous / mixed samples are not a patient oxygen saturation
    sat = _latest_lab(encounter, "Oxygen Saturation", cutoff, specimen="ART.")
    vitals["oxygen_saturation"] = (
        _vital(round(float(sat[1]["valuenum"])), "%", sat[0], "Arterial blood gas") if sat else None
    )

    weight = _latest_omr(encounter, ("Weight (Lbs)",), cutoff)
    vitals["weight"] = (
        _vital(round(float(weight[1]["result_value"])), "lb", weight[0][0], "OMR") if weight else None
    )

    bmi = _latest_omr(encounter, ("BMI (kg/m2)",), cutoff)
    vitals["bmi"] = _vital(float(bmi[1]["result_value"]), None, bmi[0][0], "OMR") if bmi else None
    return vitals


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "ai_configured": bool(os.getenv("OPENAI_API_KEY")), "model": model_name()}


@app.get("/api/requests")
def list_requests() -> list[dict[str, Any]]:
    store = get_store()
    out = []
    for req in store.transfers.values():
        patient = store.chart(req.subject_id)["patient"]
        out.append(
            {
                "transfer_id": req.transfer_id,
                "subject_id": req.subject_id,
                "hadm_id": req.hadm_id,
                "gender": patient.get("gender"),
                "age": patient.get("age"),
                "department": req.department,
                "careunit": req.careunit,
                "level_of_care": req.level_of_care,
                "requested_level_of_care": req.requested_level_of_care,
                "intime": req.intime,
            }
        )
    return out


@app.get("/api/requests/{transfer_id}")
def request_detail(transfer_id: str) -> dict[str, Any]:
    """Patient banner, transfer and vital signs. Fast; no AI call."""
    store = get_store()
    req = _request_or_404(transfer_id)
    chart = store.chart(req.subject_id)
    encounter = chart["encounters"].get(req.hadm_id)
    if encounter is None:
        raise HTTPException(404, f"Encounter {req.hadm_id} not in chart")
    patient = chart["patient"]
    return {
        "patient": {
            "subject_id": req.subject_id,
            "gender": patient.get("gender"),
            "age": patient.get("age"),
        },
        "encounter": {
            "hadm_id": req.hadm_id,
            "admit_time": encounter.get("admit_time"),
            "discharge_time": encounter.get("discharge_time"),
        },
        "transfer": {
            "transfer_id": req.transfer_id,
            "department": req.department,
            "service": req.curr_service,
            "careunit": req.careunit,
            "level_of_care": req.level_of_care,
            "requested_level_of_care": req.requested_level_of_care,
            "provider_name": req.provider_name,
            "intime": req.intime,
        },
        # latest reading at or before the transfer time; None when not charted
        "vitals": vital_signs(encounter, parse_datetime(req.intime)),
    }


@app.get("/api/requests/{transfer_id}/placement")
def placement(transfer_id: str, refresh: bool = False) -> dict[str, Any]:
    """AI summary + bed recommendations."""
    store = get_store()
    req = _request_or_404(transfer_id)
    chart = store.chart(req.subject_id)
    try:
        summary = get_summary(chart, req.hadm_id, refresh=refresh)
    except SummaryError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    result = summary["result"]
    beds = recommend(
        store,
        specialty=req.department,
        requested_loc=req.requested_level_of_care,
        patient_sex=chart["patient"].get("gender", ""),
        summary=result,
    )
    return {
        "summary": {
            "model": summary["model"],
            "text": result["summary"],
            "flagged": [f for f in ATTRIBUTE_FIELDS if result.get(f) == "yes"],
            "not_flagged": [f for f in ATTRIBUTE_FIELDS if result.get(f) != "yes"],
            "contact_isolation_reason": result.get("contact_isolation_reason"),
            "bloodless": result.get("bloodless"),
            "drips": result.get("drips", []),
            "lines": result.get("lines", []),
            "tubes": result.get("tubes", []),
        },
        "recommendations": beds,
    }


@app.get("/api/bed-board")
def bed_board() -> dict[str, Any]:
    store = get_store()
    units: dict[str, dict[str, Any]] = {}
    for b in store.beds:
        u = units.setdefault(b.unit, {"unit": b.unit, "department": b.department, "counts": {}})
        u["counts"][b.status] = u["counts"].get(b.status, 0) + 1
    return {"units": list(units.values())}


# ---- frontend ----
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="frontend")
