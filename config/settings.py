"""Central config: loads .env + config/targets.json and exposes tuning knobs."""
from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # dotenv optional
    pass

ROOT = Path(__file__).resolve().parent.parent


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# storage
SUPABASE_URL = _get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = _get("SUPABASE_ANON_KEY")
SQLITE_PATH = _get("SQLITE_PATH", str(ROOT / "data" / "local" / "jobradar.sqlite"))

# matcher brain
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _get("ANTHROPIC_MODEL", "claude-opus-4-8")
MATCH_MONTHLY_USD_CEILING = float(_get("MATCH_MONTHLY_USD_CEILING", "30") or 30)

# alerts
DISCORD_JOB_RADAR_WEBHOOK = _get("DISCORD_JOB_RADAR_WEBHOOK")
DISCORD_WEBHOOK_JOBS = _get("DISCORD_WEBHOOK_JOBS")
ALERT_MIN_SCORE = int(_get("ALERT_MIN_SCORE", "75") or 75)

# candidate profile
MATRIX_PATH = _get("MATRIX_PATH", str(ROOT / "config" / "skills_matrix.md"))
GENERATED_DOCS_DIR = _get("GENERATED_DOCS_DIR", "D:/Job_Applications_MASTER/Generated")

# candidate targeting (used by the prefilter — vestigial scorer path, not part of
# the hourly `ingest` workflow; kept correct so it is not misleading if revived)
TARGET_METROS = ["phoenix", "scottsdale", "tempe", "arizona", "az", "seattle",
                 "san francisco", "bay area", "san jose", "san diego", "monterey", "denver",
                 "colorado", "austin", "seattle", "new york", "remote"]
TARGET_KEYWORDS = ["software engineer", "backend", "frontend", "full stack", "platform",
                   "infrastructure", "devops", "site reliability", "data engineer",
                   "machine learning", "artificial intelligence", "security engineer",
                   "product engineer", "mobile", "ios", "android", "api", "distributed systems"]
NEGATIVE_KEYWORDS = ["intern", "internship", "seasonal", "part-time", "part time"]


def load_targets() -> dict:
    with open(ROOT / "config" / "targets.json", encoding="utf-8") as f:
        return json.load(f)
