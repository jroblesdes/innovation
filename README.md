# Bed Placement Review

Proof of concept for the Keck bed capacity management project: shorten in-house
transfer time by putting the patient's placement needs, the RPRB routing rules
and live bed availability on one PowerChart-style screen, so a nurse or capacity
coordinator can pick a bed quickly and accurately.

Uses fictional MIMIC-IV data only. This is not a clinical decision tool.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # add OPENAI_API_KEY (and OPENAI_MODEL if different)
uvicorn backend.app:app --reload
```

Open http://localhost:8000

Summaries come from `summarize_encounter()` in `summarize_patient_encounter.py`
(OpenAI Responses API). Each result is cached in `cache/`, so a patient is only
summarized once; the Refresh link in the Summary panel re-runs it. If the key is
missing or a call fails, the Summary panel shows the error and the rest of the
page still loads.

## How it works

```
data/patient_charts.json ─┐
                          ├─ summarizer.py ── AI summary (OpenAI)
data/transfer_scenarios ──┤
data/RPRB_Rule_Set.xlsx ──┼─ matcher.py ───── ranked bed recommendations
data/bed_board_and_status ┘
                               app.py (FastAPI) ── /api/* ── frontend/ (HTML, CSS, JS)
```

1. **Worklist.** Each `transfer_id` in `transfer_scenarios.csv` is one transfer
   request (194 after merging repeated service-change rows).
2. **Summary.** The chart is cut to the selected encounter plus earlier ones
   (`select_current_and_prior_encounters`), then summarized into the
   `BedPlacementSummary` schema: care-need flags, isolation reason, drips,
   lines and tubes.
3. **Routing.** The RPRB rule set maps specialty + requested level of care to
   units, primary first, then overflow.
4. **Bed matching.** Only available beds (`A`) in those units. Shared-room rules:
   - contact isolation needs a private room or an empty shared room (the other
     bed must be held);
   - patients not on isolation are not placed with a roommate on isolation;
   - occupied roommates must be the same sex.
5. **Ranking.** Primary units first. Isolation patients get private rooms first;
   other patients get shared rooms first so private rooms stay free.
6. **Vital signs.** Latest reading at or before the transfer time:
   blood pressure, weight and BMI from OMR, temperature from blood gas, and
   O2 saturation from arterial blood gas only. Anything not charted is `null`
   in the API and shows as "–" on screen. Heart rate and respiratory rate are in
   the API but always `null` because the charts don't contain them yet.
7. **Safety prompts.** Warns when an ICU-only device (balloon pump, ECMO,
   Impella, ventilator, LVAD) is flagged but a lower level of care is requested,
   lists dirty beds waiting on EVS, and notes units not on the bed board (Norris).

## API

| Endpoint | Returns |
| --- | --- |
| `GET /api/health` | whether AI summaries are on |
| `GET /api/requests` | worklist of transfer requests |
| `GET /api/requests/{transfer_id}` | banner, transfer and vital signs (`null` when not charted) |
| `GET /api/requests/{transfer_id}/placement?refresh=` | summary + bed recommendations |
| `GET /api/bed-board` | bed counts by unit and status |

## Files

```
backend/
  app.py                          FastAPI routes, serves the frontend
  data_store.py                   loads the four data files
  summarizer.py                   AI summary + cache
  preview_summaries.py            keyword sample summaries, used only by build_preview.py
  matcher.py                      RPRB routing and bed matching
  summarize_patient_encounter.py  original script, unchanged
frontend/
  index.html, styles.css, app.js  PowerChart-style one-screen UI
data/                             project data files
build_preview.py                  builds preview.html (no server needed)
```

## Placeholders and gaps

- **Not in the data, shown as "Not Documented":** patient names, allergies,
  code status and blood acceptance. If the chart flags the patient as bloodless,
  "Blood Acceptable: No" is shown.
- **PowerChart link:** the "Open source order in PowerChart" button is turned
  off until `POWERCHART_URL_TEMPLATE` in `frontend/app.js` is set.
- **Not on the bed board:** bariatric equipment and patient room preferences,
  so neither one affects the ranking yet.
- **preview.html** uses keyword sample summaries so it works without the API.
  It labels them on screen. The live site always uses the AI summary.
