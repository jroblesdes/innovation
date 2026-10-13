"""Build preview.html: one self-contained page with the API responses baked in.

For sharing a click-through demo without running the server or calling the
API. Summaries in the preview come from backend/preview_summaries.py (keyword
sample), and the page labels them as preview data.
Run:  python build_preview.py
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import backend.summarizer as summarizer
from backend.app import app
from backend.preview_summaries import sample_summary

ROOT = Path(__file__).resolve().parent
FRONT = ROOT / "frontend"

# Use the sample summary instead of the API, and keep the real cache untouched.
os.environ.setdefault("OPENAI_API_KEY", "preview")
summarizer.summarize_encounter = lambda chart, model: sample_summary(chart)
summarizer.CACHE_DIR = Path(tempfile.mkdtemp())

client = TestClient(app)
requests = client.get("/api/requests").json()
data = {
    "health": client.get("/api/health").json(),
    "requests": requests,
    "details": {},
    "placements": {},
}
for r in requests:
    tid = r["transfer_id"]
    data["details"][tid] = client.get(f"/api/requests/{tid}").json()
    data["placements"][tid] = client.get(f"/api/requests/{tid}/placement").json()

html = (FRONT / "index.html").read_text(encoding="utf-8")
css = (FRONT / "styles.css").read_text(encoding="utf-8")
js = (FRONT / "app.js").read_text(encoding="utf-8")
blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")

html = html.replace('<link rel="stylesheet" href="styles.css" />', f"<style>\n{css}\n</style>")
html = html.replace(
    '<script src="app.js"></script>',
    f"<script>window.STATIC_DATA = {blob};</script>\n<script>\n{js}\n</script>",
)
out = ROOT / "preview.html"
out.write_text(html, encoding="utf-8")
print(f"Wrote {out} ({out.stat().st_size // 1024} KB, {len(requests)} transfer requests)")
