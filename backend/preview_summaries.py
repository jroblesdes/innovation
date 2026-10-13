"""Sample summaries for the static preview only (build_preview.py).

The live site always uses the AI summary. This keyword version lets the
preview show realistic content without calling the API. Same output schema.
"""

from __future__ import annotations

import re
from typing import Any

from .summarize_patient_encounter import BedPlacementSummary, parse_datetime


# ---------------------------------------------------------------------------

def _has(texts: list[str], pattern: str) -> bool:
    rx = re.compile(pattern, re.I)
    return any(rx.search(t) for t in texts)


def _matches(texts: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [t for t in texts if rx.search(t)]


def _latest_omr(encounters: dict[str, Any], name: str, cutoff) -> float | None:
    best = None
    for enc in encounters.values():
        for row in enc.get("omr", []):
            if row.get("result_name") != name:
                continue
            when = parse_datetime(row.get("chartdate"))
            if when is None or (cutoff and when > cutoff):
                continue
            try:
                value = float(str(row.get("result_value")).split("/")[0])
            except ValueError:
                continue
            if best is None or when > best[0]:
                best = (when, value)
    return best[1] if best else None


def _micro_isolation_reasons(micro: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for m in micro:
        org = (m.get("org_name") or "").upper()
        test = (m.get("test_name") or "").upper()
        ab = (m.get("ab_name") or "").upper()
        interp = (m.get("interpretation") or "").upper()
        if "METHICILLIN RESISTANT" in org or (test == "MRSA SCREEN" and org and org != "CANCELLED"):
            reasons.append("MRSA")
        elif "STAPH AUREUS" in org and ab == "OXACILLIN" and interp == "R":
            reasons.append("MRSA")
        elif "DIFFICILE" in test and org in {"CLOSTRIDIUM DIFFICILE", "POSITIVE"}:
            reasons.append("C. difficile")
        elif "ENTEROCOCCUS" in org and ab == "VANCOMYCIN" and interp == "R":
            reasons.append("VRE")
    return reasons


def sample_summary(filtered_chart: dict[str, Any]) -> BedPlacementSummary:
    encounters = filtered_chart["encounters"]
    current_id = filtered_chart["selected_hadm_id"]
    current = encounters[current_id]
    cutoff = parse_datetime(current.get("discharge_time"))

    cur_dx = [d.get("long_title") or "" for d in current.get("diagnoses", [])]
    cur_px = [p.get("long_title") or "" for p in current.get("procedures", [])]
    all_dx = [d.get("long_title") or "" for e in encounters.values() for d in e.get("diagnoses", [])]
    all_px = [p.get("long_title") or "" for e in encounters.values() for p in e.get("procedures", [])]
    cur_text = cur_dx + cur_px

    weight = _latest_omr(encounters, "Weight (Lbs)", cutoff)
    bmi = _latest_omr(encounters, "BMI (kg/m2)", cutoff)

    yn = lambda flag: "yes" if flag else "no"  # noqa: E731

    vent = _has(cur_px, r"invasive mechanical ventilation|respiratory ventilation|endotracheal|tracheostomy")
    trach = _has(cur_text, r"tracheostomy")
    balloon = _has(cur_px, r"balloon pump|pulsation balloon|intra-?aortic balloon")
    over300 = weight is not None and weight > 300
    bariatric = over300 or _has(cur_dx, r"morbid \(severe\) obesity|morbid obesity|bariatric")
    falls = _has(cur_dx + all_dx, r"\bfall(s|ing)?\b|history of fall|unsteadiness on feet")

    iso_reasons = _micro_isolation_reasons(current.get("microbiology", []))
    for title in cur_dx + _matches(all_dx, r"history of methicillin resistant"):
        if re.search(r"methicillin resistant", title, re.I):
            iso_reasons.append("MRSA")
        elif re.search(r"clostridi(um|oides) difficile", title, re.I):
            iso_reasons.append("C. difficile")
        elif re.search(r"vancomycin resistant", title, re.I):
            iso_reasons.append("VRE")
    iso_reasons = sorted(set(iso_reasons))
    contact = bool(iso_reasons)

    immuno = _has(
        all_dx,
        r"transplant status|immunodeficiency|HIV|immunosuppress|antineoplastic chemotherapy|neutropenia",
    )
    bmt = _has(all_dx + all_px, r"bone marrow transplant|stem cells? transplant|hematopoietic")
    lvad = _has(all_dx + all_px, r"left ventricular assist|heart assist device|implantable heart assist")
    ecmo = _has(cur_px, r"extracorporeal membrane oxygenation|\bECMO\b|extracorporeal oxygenation")
    impella = _has(cur_px, r"impella|impeller pump")
    pregnant = _has(cur_dx, r"pregnan|antepartum") and not _has(cur_dx, r"not pregnant")
    decubiti = _has(cur_dx, r"pressure ulcer|decubitus|pressure injury")
    bloodless = _has(all_dx, r"refus\w* .*blood|blood transfusion refused|jehovah")
    ventric = _has(cur_px, r"ventriculostomy|external ventricular drain|drainage of cerebral ventricle")
    video = _has(cur_px, r"video.*(ekg|ecg|electrocardio)")

    lines = []
    if _has(cur_px, r"central venous catheter|infusion device into superior vena cava|infusion device into right atrium|internal jugular|femoral vein"):
        lines.append("Central venous catheter")
    if _has(cur_px, r"arterial catheterization|monitoring of arterial pressure"):
        lines.append("Arterial line")
    if _has(cur_px, r"venous catheterization, not elsewhere"):
        lines.append("Venous catheter")
    if _has(cur_dx + cur_px, r"dialysis catheter|hemodialysis"):
        lines.append("Dialysis access")

    tubes = []
    if _has(cur_px, r"endotracheal"):
        tubes.append("Endotracheal tube")
    if trach:
        tubes.append("Tracheostomy")
    if _has(cur_px, r"pleural cavity with drainage device|intercostal catheter"):
        tubes.append("Chest tube")
    if _has(cur_px, r"feeding device|enteral infusion"):
        tubes.append("Feeding tube")
    if _has(cur_px, r"percutaneous abdominal drainage|peritoneal cavity|pericardial cavity with drainage|stomach with drainage device"):
        tubes.append("Drain")

    flags = {
        "balloon pump": balloon,
        "ventilator/ET/trach": vent,
        "bariatric needs": bariatric,
        "fall precautions": falls,
        "contact isolation" + (f" ({', '.join(iso_reasons)})" if iso_reasons else ""): contact,
        "immunosuppression": immuno,
    }
    needs = [k for k, v in flags.items() if v]
    sentences = []
    if needs:
        joined = needs[0] if len(needs) == 1 else ", ".join(needs[:-1]) + " and " + needs[-1]
        sentences.append(f"Flagged for {joined}.")
    else:
        sentences.append("No special placement needs found in the chart.")
    measures = []
    if weight is not None:
        measures.append(f"latest weight {weight:.0f} lb")
    if bmi is not None:
        measures.append(f"BMI {bmi:.1f}")
    if measures:
        text = ", ".join(measures)
        sentences.append(text[0].upper() + text[1:] + ".")
    if lines or tubes:
        sentences.append("Documented lines/tubes: " + ", ".join(lines + tubes).lower() + ".")
    summary = " ".join(sentences)

    return BedPlacementSummary(
        summary=summary,
        balloon_pump=yn(balloon),
        ventilator_et_trach=yn(vent),
        bariatric=yn(bariatric),
        weight_over_300_lbs=yn(over300),
        fall_precautions=yn(falls),
        contact_isolation=yn(contact),
        contact_isolation_reason=", ".join(iso_reasons) if contact else "No supporting evidence in chart",
        bloodless=yn(bloodless),
        decubiti=yn(decubiti),
        immunosuppressed=yn(immuno),
        lvad=yn(lvad),
        tracheostomy=yn(trach),
        video_ekg=yn(video),
        bmt=yn(bmt),
        ecmo=yn(ecmo),
        impella=yn(impella),
        pregnant=yn(pregnant),
        ventriculostomy=yn(ventric),
        drips=[],
        lines=lines,
        tubes=tubes,
    )
