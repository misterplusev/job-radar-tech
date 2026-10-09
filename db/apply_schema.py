"""Apply db/schema_tech.sql to the tech Supabase project via the Management API.

Env:
  SUPABASE_ACCESS_TOKEN  personal access token (Management API)
  SUPABASE_PROJECT_REF   project ref (default read from env, no hand-typed literals)

Idempotent: the schema file only creates-if-not-exists / replaces tech_* objects.
"""
import os
import sys
from pathlib import Path

import requests

TOKEN = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
REF = os.environ.get("SUPABASE_PROJECT_REF", "").strip()

if not TOKEN or not REF:
    print("FATAL: SUPABASE_ACCESS_TOKEN and/or SUPABASE_PROJECT_REF not set", file=sys.stderr)
    sys.exit(1)

SQL_PATH = Path(__file__).resolve().parent / "schema_tech.sql"
sql = SQL_PATH.read_text(encoding="utf-8")

r = requests.post(
    f"https://api.supabase.com/v1/projects/{REF}/database/query",
    headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    json={"query": sql},
    timeout=90,
)
if r.status_code in (200, 201):
    print(f"[MIGRATE] OK {SQL_PATH.name}")
else:
    print(f"[MIGRATE] FAIL {r.status_code} {r.text[:300]}", file=sys.stderr)
    sys.exit(1)
