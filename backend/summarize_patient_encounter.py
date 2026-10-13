#!/usr/bin/env python3
"""Summarize one fictional MIMIC-IV patient chart for bed placement.

The input produced by ``build_patient_chart`` in ``mimiciv.ipynb`` is JSON
Lines: one patient-chart JSON object per line. This script selects one patient
and one ``hadm_id``, removes later encounters, and asks the OpenAI Responses API
for a schema-validated placement summary.

This is a proof-of-concept for fictional data, not a clinical decision tool.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from dotenv import load_dotenv

load_dotenv()


YesNo = Literal["yes", "no"]


class BedPlacementSummary(BaseModel):
    """Exact structured output returned by the model."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="Brief description of the patient's bed-placement needs."
    )
    balloon_pump: YesNo
    ventilator_et_trach: YesNo
    bariatric: YesNo
    weight_over_300_lbs: YesNo
    fall_precautions: YesNo
    contact_isolation: YesNo
    contact_isolation_reason: str = Field(
        description=(
            "Evidence-based reason for contact isolation, or a short statement "
            "that no supporting evidence is present."
        )
    )
    bloodless: YesNo
    decubiti: YesNo
    immunosuppressed: YesNo
    lvad: YesNo
    tracheostomy: YesNo
    video_ekg: YesNo
    bmt: YesNo
    ecmo: YesNo
    impella: YesNo
    pregnant: YesNo
    ventriculostomy: YesNo
    drips: list[str] = Field(
        description="Active/relevant IV drips; empty when none are supported."
    )
    lines: list[str] = Field(
        description="Active/relevant vascular access lines; empty when none are supported."
    )
    tubes: list[str] = Field(
        description="Active/relevant tubes or drains; empty when none are supported."
    )


SYSTEM_PROMPT = """\
You extract bed-placement information from fictional longitudinal patient data.
The supplied chart has already been limited to the selected encounter and prior
encounters. Never assume that omitted later encounters exist.

Use diagnoses, procedures, microbiology, labs, measurements, and their timing
together. Return only the requested structured result.

Rules:
- The summary must be concise (normally 2-4 sentences), focus on current
  bed-placement needs, and distinguish current findings from remote history.
- Infer each yes/no field from the supplied evidence. Use "yes" only when the
  chart contains positive or strongly supporting evidence. Otherwise use "no";
  absence of a field is not positive evidence.
- Do not invent devices, precautions, diagnoses, treatments, or measurements.
- Treat ventilator_et_trach as yes for invasive mechanical ventilation, an
  endotracheal tube, or a tracheostomy. Treat tracheostomy as yes only when a
  tracheostomy is specifically supported.
- Bariatric may be yes when the record explicitly describes bariatric status or
  body habitus requiring bariatric equipment. Weight_over_300_lbs requires a
  documented/inferable weight over 300 lb (136.1 kg); BMI alone is insufficient.
- Contact isolation requires an organism, infection/colonization, or explicit
  precaution that supports contact precautions. State the reason briefly.
- Bloodless means an explicit refusal/avoidance of blood products, not merely
  anemia or lack of transfusion.
- Decubiti means a pressure injury/ulcer. Immunosuppressed may be supported by
  transplant/BMT status, immunosuppressive therapy, or a clearly immunocompromising
  condition/treatment.
- Video EKG means continuous video-assisted ECG monitoring specifically; ordinary
  telemetry or a standard ECG is insufficient.
- List only drips, lines, and tubes supported by the chart. Prefer specific names.
  Use empty arrays when no item is supported.
- This result is informational and must not claim to replace clinician review.
"""


def parse_datetime(value: Any) -> datetime | None:
    """Parse notebook-produced datetime strings without guessing invalid values."""
    if value is None or value == "" or str(value).lower() in {"nat", "nan", "none"}:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def load_patient_chart(jsonl_path: Path, subject_id: str) -> dict[str, Any]:
    """Stream the JSONL file and return the requested patient's chart."""
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                chart = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc

            chart_subject = chart.get("patient", {}).get("subject_id")
            if str(chart_subject) == str(subject_id):
                return chart

    raise KeyError(f"subject_id {subject_id!r} was not found in {jsonl_path}")


def select_current_and_prior_encounters(
    patient_chart: dict[str, Any], hadm_id: str
) -> dict[str, Any]:
    """Return a copy containing the selected admission and earlier admissions.

    Chronology is based on ``admit_time``. Encounters with an unparseable/missing
    admit time are excluded unless they are the selected encounter. The full
    selected encounter is retained through its discharge, per the requested
    behavior.
    """
    encounters = patient_chart.get("encounters")
    if not isinstance(encounters, dict):
        raise ValueError("Patient chart must contain an 'encounters' object")

    target_key = str(hadm_id)
    if target_key not in encounters:
        raise KeyError(f"hadm_id {hadm_id!r} is not present in this patient chart")

    target_admit = parse_datetime(encounters[target_key].get("admit_time"))
    if target_admit is None:
        raise ValueError(
            f"Selected encounter {hadm_id!r} has no parseable admit_time; "
            "a safe temporal cutoff cannot be established"
        )

    selected: dict[str, Any] = {}
    for encounter_id, encounter in encounters.items():
        if str(encounter_id) == target_key:
            selected[str(encounter_id)] = encounter
            continue
        encounter_admit = parse_datetime(encounter.get("admit_time"))
        if encounter_admit is not None and encounter_admit <= target_admit:
            selected[str(encounter_id)] = encounter

    selected = dict(
        sorted(
            selected.items(),
            key=lambda item: parse_datetime(item[1].get("admit_time")) or datetime.min,
        )
    )
    return {
        "patient": patient_chart.get("patient", {}),
        "selected_hadm_id": target_key,
        "encounters": selected,
    }


def summarize_encounter(
    filtered_chart: dict[str, Any],
    *,
    model: str = "gpt-6.1-sol",
    client: OpenAI | None = None,
) -> BedPlacementSummary:
    """Call the Responses API and return a validated Pydantic result."""
    api_client = client or OpenAI()
    chart_json = json.dumps(filtered_chart, ensure_ascii=False, default=str)

    response = api_client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Summarize the selected encounter for potential bed placement. "
                    "The JSON contains the selected encounter and prior encounters only.\n\n"
                    f"PATIENT_CHART_JSON:\n{chart_json}"
                ),
            },
        ],
        text_format=BedPlacementSummary,
        store=False,
    )

    if response.output_parsed is None:
        raise RuntimeError(
            "The model did not return a parsed summary. Inspect the API response "
            "for a refusal or incomplete response."
        )
    return response.output_parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize one fictional patient encounter for bed placement."
    )
    parser.add_argument("patient_charts", type=Path, help="Path to patient_charts.json (JSONL)")
    parser.add_argument("--subject-id", required=True, help="Patient subject_id")
    parser.add_argument("--hadm-id", required=True, help="Selected encounter hadm_id")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-6.1-sol"),
        help="OpenAI model (default: OPENAI_MODEL or gpt-6.1-sol)",
    )
    parser.add_argument("--output", type=Path, help="Optional path for result JSON")
    parser.add_argument(
        "--show-filtered-chart",
        action="store_true",
        help="Print the temporally filtered chart and exit without an API call",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    chart = load_patient_chart(args.patient_charts, args.subject_id)
    filtered_chart = select_current_and_prior_encounters(chart, args.hadm_id)

    if args.show_filtered_chart:
        print(json.dumps(filtered_chart, indent=2, ensure_ascii=False, default=str))
        return

    if not os.getenv("OPENAI_API_KEY"):
        raise EnvironmentError("Set OPENAI_API_KEY before making the API request")

    result = summarize_encounter(filtered_chart, model=args.model)
    output_json = json.dumps(result.model_dump(), indent=2, ensure_ascii=False)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_json + "\n", encoding="utf-8")
    print(output_json)


if __name__ == "__main__":
    main()
