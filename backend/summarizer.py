"""AI bed-placement summary for one encounter.

Uses summarize_encounter() from summarize_patient_encounter.py (OpenAI
Responses API, schema-validated BedPlacementSummary). Requires OPENAI_API_KEY.
Results are cached on disk in ../cache so each encounter is summarized once;
pass refresh=True to re-run.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .summarize_patient_encounter import (
    select_current_and_prior_encounters,
    summarize_encounter,
)

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

ATTRIBUTE_FIELDS = [
    "balloon_pump",
    "ventilator_et_trach",
    "bariatric",
    "weight_over_300_lbs",
    "fall_precautions",
    "contact_isolation",
    "bloodless",
    "decubiti",
    "immunosuppressed",
    "lvad",
    "tracheostomy",
    "video_ekg",
    "bmt",
    "ecmo",
    "impella",
    "pregnant",
    "ventriculostomy",
]


class SummaryError(RuntimeError):
    """The AI summary could not be produced (missing key, network, refusal...)."""


def model_name() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-6.1-sol")


def get_summary(chart: dict[str, Any], hadm_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Return {"model": str, "result": BedPlacementSummary as dict}."""
    subject_id = str(chart["patient"]["subject_id"])
    model = model_name()
    cache_file = CACHE_DIR / f"{subject_id}_{hadm_id}.json"

    if cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    if not os.getenv("OPENAI_API_KEY"):
        raise SummaryError("OPENAI_API_KEY is not set on the server.")

    filtered = select_current_and_prior_encounters(chart, hadm_id)
    try:
        result = summarize_encounter(filtered, model=model).model_dump()
    except Exception as exc:  # network, quota, refusal, invalid output
        log.exception("AI summary failed for %s/%s", subject_id, hadm_id)
        raise SummaryError(f"AI summary failed ({type(exc).__name__}). Try again.") from exc

    payload = {"model": model, "result": result}
    cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
