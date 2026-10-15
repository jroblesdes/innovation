"""Bed recommendation engine.

1. RPRB rule set: the patient's specialty + requested level of care give an
   ordered list of units (primary first, then overflow).
2. Bed board: only beds with status "A" (available) in those units.
3. Room checks for shared rooms:
     - contact isolation patients need a private room, or a shared room whose
       other bed(s) are empty (the other bed must then be held);
     - patients not on isolation can't share with a roommate on isolation;
     - an occupied roommate must be the same sex.
4. Ranking, in order:
     a. primary units before overflow (RPRB);
     b. contact isolation patients: isolation-specific rooms from the room
        proximity CSV first (ISOLATION RM, NEG PRES ISO RM, ...);
     c. fall precaution patients: shortest distance to the nearest nurses
        station first;
     d. isolation patients get private rooms first, other patients get shared
        rooms first so private rooms stay free for patients who need them;
     e. fewer cautions, then unit / room / bed.
   Beds whose room is not in the proximity CSV (e.g. floor 9) skip b and c:
   they are not treated as isolation rooms and have no distance, so they sort
   after rooms that have one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .data_store import BED_STATUS, Bed, DataStore, RoomInfo

LOC_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "ICU": [("Primary ICU", "Primary"), ("Overflow ICU", "Overflow")],
    "Stepdown": [("Stepdown", "Primary")],
    "Telemetry": [("Primary Tele", "Primary"), ("Overflow Tele", "Overflow")],
    "MedSurg": [("MedSurg", "Primary")],
}

ICU_ONLY_DEVICES = {
    "balloon_pump": "balloon pump",
    "ecmo": "ECMO",
    "impella": "Impella",
    "ventilator_et_trach": "ventilator / ET / trach",
    "lvad": "LVAD",
}

SEX = {"F": "Female", "M": "Male"}


@dataclass
class Candidate:
    bed: Bed
    tier: str
    tier_rank: int
    private: bool
    room_info: RoomInfo | None = None
    checks: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    @property
    def in_isolation_room(self) -> bool:
        return bool(self.room_info and self.room_info.is_isolation_room)

    @property
    def distance_ft(self) -> float | None:
        return self.room_info.distance_ft if self.room_info else None

    def score(self, isolation: bool, fall_precautions: bool) -> tuple:
        key: list[Any] = [self.tier_rank]
        if isolation:
            key.append(0 if self.in_isolation_room else 1)
        if fall_precautions:
            key.append(self.distance_ft if self.distance_ft is not None else math.inf)
        privacy_pref = (0 if self.private else 1) if isolation else (1 if self.private else 0)
        key += [privacy_pref, len(self.cautions), self.bed.unit, self.bed.room, self.bed.bed]
        return tuple(key)

    def to_dict(self) -> dict[str, Any]:
        b = self.bed
        return {
            "bed_id": b.bed_id,
            "unit": b.unit,
            "department": b.department,
            "room": b.room,
            "bed": b.bed,
            "tier": self.tier,
            "room_type": "Private" if self.private else "Shared",
            "room_plan_type": self.room_info.room_type if self.room_info else None,
            "isolation_room": self.in_isolation_room,
            "distance_to_nurses_station_ft": self.distance_ft,
            "nearest_nurses_station": self.room_info.nearest_nurses_station if self.room_info else None,
            "checks": self.checks,
            "cautions": self.cautions,
        }


def recommend(
    store: DataStore,
    *,
    specialty: str,
    requested_loc: str,
    patient_sex: str,
    summary: dict[str, Any],
    limit: int = 3,
) -> dict[str, Any]:
    isolation = summary.get("contact_isolation") == "yes"
    fall_precautions = summary.get("fall_precautions") == "yes"
    sex = SEX.get(patient_sex, "Unknown")
    warnings: list[str] = []
    notes: list[str] = []

    # Level-of-care sanity check against ICU-only devices
    if requested_loc != "ICU":
        devices = [label for key, label in ICU_ONLY_DEVICES.items() if summary.get(key) == "yes"]
        if devices:
            warnings.append(
                f"Requested {requested_loc}, but {', '.join(devices)} flagged. Confirm level of care before placing."
            )

    rules = store.rules.get(specialty)
    if rules is None:
        return _empty(f"No RPRB rule for specialty '{specialty}'.", warnings)
    columns = LOC_COLUMNS.get(requested_loc)
    if columns is None:
        return _empty(f"Unknown requested level of care '{requested_loc}'.", warnings)

    board_units = store.bed_board_units()
    tiers: list[tuple[str, int, list[str]]] = []
    rank = 0
    for col, label in columns:
        units = rules.get(col, [])
        if not units:
            continue
        off_board = [u for u in units if u not in board_units]
        if off_board:
            notes.append(f"{', '.join(off_board)} not on this bed board; check that unit directly.")
        tiers.append((label, rank, [u for u in units if u in board_units]))
        rank += 1

    candidates: list[Candidate] = []
    excluded = {"isolation": 0, "sex": 0}
    dirty: list[str] = []

    for label, tier_rank, units in tiers:
        for unit in units:
            for bed in [b for b in store.beds if b.unit == unit]:
                if bed.status == "D":
                    dirty.append(bed.bed_id)
                    info = store.room_info(bed.room)
                    if isolation and info and info.is_isolation_room:
                        notes.append(f"{info.isolation_label} {bed.bed_id} is waiting on EVS.")
                if not bed.is_available:
                    continue
                cand = _evaluate(
                    store, bed, label, tier_rank, isolation, fall_precautions, sex, summary, excluded
                )
                if cand:
                    candidates.append(cand)

    candidates.sort(key=lambda c: c.score(isolation, fall_precautions))

    if isolation and store.rooms:
        if any(c.in_isolation_room for c in candidates):
            notes.insert(0, "Contact isolation: isolation rooms ranked first within each unit tier.")
        else:
            notes.insert(0, "Contact isolation: no isolation room available in these units.")
    if fall_precautions and store.rooms:
        notes.insert(0, "Fall precautions: ranked by distance to the nearest nurses station.")
    unit_plan = [
        {"tier": label, "units": units} for label, _, units in tiers
    ]

    if summary.get("bariatric") == "yes" or summary.get("weight_over_300_lbs") == "yes":
        notes.append("Bariatric bed and equipment are not tracked on the bed board. Confirm with the unit.")

    return {
        "routing": {"specialty": specialty, "requested_level_of_care": requested_loc, "tiers": unit_plan},
        "beds": [c.to_dict() for c in candidates[:limit]],
        "total_matches": len(candidates),
        "excluded": excluded,
        "dirty_beds": dirty,
        "warnings": warnings,
        "notes": notes,
        "message": None if candidates else "No available bed in the RPRB units for this level of care.",
    }


def _evaluate(
    store: DataStore,
    bed: Bed,
    tier: str,
    tier_rank: int,
    isolation: bool,
    fall_precautions: bool,
    sex: str,
    summary: dict[str, Any],
    excluded: dict[str, int],
) -> Candidate | None:
    room = store.beds_in_room(bed.unit, bed.room)
    others = [b for b in room if b is not bed]
    private = not others
    occupied = [b for b in others if b.is_occupied]
    info = store.room_info(bed.room)  # None for rooms not on the proximity CSV (e.g. floor 9)
    cand = Candidate(bed=bed, tier=tier, tier_rank=tier_rank, private=private, room_info=info)

    cand.checks.append(f"{tier} unit for specialty")
    if isolation and cand.in_isolation_room:
        cand.checks.append(info.isolation_label)
    if fall_precautions:
        if info and info.distance_ft is not None:
            cand.checks.append(
                f"{info.distance_ft:.0f} ft to nurses station {info.nearest_nurses_station}".strip()
            )
        else:
            cand.cautions.append("Distance to nurses station not on file")

    if private:
        cand.checks.append("Private room")
    elif not occupied:
        cand.checks.append("Shared room, other bed empty")
    else:
        cand.checks.append("Shared room")

    if isolation:
        if private:
            cand.checks.append("Meets contact isolation")
        elif not occupied:
            cand.checks.append("Meets contact isolation")
            cand.cautions.append(f"Hold the other bed in room {bed.room} for isolation")
        elif all(b.needs_isolation == "Yes" for b in occupied):
            cand.cautions.append("Roommate also on isolation; cohort only if same organism (check with Infection Prevention)")
        else:
            excluded["isolation"] += 1
            return None
    else:
        if any(b.needs_isolation == "Yes" for b in occupied):
            excluded["isolation"] += 1
            return None

    if occupied:
        roommate_sexes = {b.patient_sex for b in occupied}
        if sex == "Unknown" or "Unknown" in roommate_sexes or "" in roommate_sexes:
            cand.cautions.append("Roommate or patient sex unknown; confirm before placing")
        elif roommate_sexes != {sex}:
            excluded["sex"] += 1
            return None
        else:
            cand.checks.append(f"{sex} roommate")

    return cand


def _empty(message: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "routing": None,
        "beds": [],
        "total_matches": 0,
        "excluded": {},
        "dirty_beds": [],
        "warnings": warnings,
        "notes": [],
        "message": message,
    }


def bed_status_label(code: str) -> str:
    return BED_STATUS.get(code, code)
