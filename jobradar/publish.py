"""Robotics jobs publisher — Supabase persistence + Discord alerts for Job Radar.

Own implementation for the robotics module (independent of every other scraper
module). Behavior contract:

  scrape  ->  group by company
          ->  fetch existing tech_jobs urls (paginated)
          ->  upsert new/seen rows (on_conflict=company,url, batched, retried)
          ->  deactivate urls that vanished from the provider (closed_at fallback)
          ->  Discord-announce NEW jobs posted within POSTED_DAYS_MAX days
              (5-embed chunks, 429-aware retries, 0.5s spacing)
          ->  failure alerts to the same webhook

Tables (this module ONLY, robotics namespaced):
  tech_jobs             postings
  robotics_job_scrape_runs  per-provider telemetry
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import requests
from requests import HTTPError

DISCORD_WEBHOOK_JOBS = os.environ.get("DISCORD_WEBHOOK_JOBS", "").strip()
DRY_RUN = False

BASE_URL = "https://api.github.com"  # unused placeholder to keep module import-safe
TABLE_JOBS = "tech_jobs"
TABLE_RUNS = "robotics_job_scrape_runs"
BATCH_SIZE = 100
CHUNK_SIZE = 5
POSTED_DAYS_MAX = 14
PROVIDER_TIMEOUT = 120


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def supabase_env() -> tuple[str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and/or SUPABASE_SERVICE_ROLE_KEY in environment.")
    return url, key


def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _rest(url_base: str, table: str) -> str:
    return f"{url_base}/rest/v1/{table}"


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def fetch_existing_urls(url_base: str, key: str, company: str) -> Set[str]:
    """All known urls for a company (paginated, 1000/page)."""
    urls: Set[str] = set()
    offset = 0
    while True:
        r = requests.get(
            _rest(url_base, TABLE_JOBS),
            headers=_headers(key),
            params={"company": f"eq.{company}", "select": "url", "limit": 1000, "offset": offset},
            timeout=120,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        urls.update(row["url"] for row in rows if row.get("url"))
        if len(rows) < 1000:
            break
        offset += 1000
    return urls


def deactivate(url_base: str, key: str, company: str, urls: Set[str]) -> None:
    """Mark vanished postings inactive. Falls back if closed_at is missing."""
    if not urls:
        return
    payload = {"is_active": False, "closed_at": now_iso()}
    try:
        r = requests.patch(
            _rest(url_base, TABLE_JOBS),
            headers=_headers(key),
            params={"company": f"eq.{company}", "url": f"in.({','.join(urls)})"},
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
    except HTTPError as e:
        resp = e.response
        if resp is not None and resp.status_code == 400 and "closed_at" in (resp.text or "").lower():
            r = requests.patch(
                _rest(url_base, TABLE_JOBS),
                headers=_headers(key),
                params={"company": f"eq.{company}", "url": f"in.({','.join(urls)})"},
                json={"is_active": False},
                timeout=120,
            )
            r.raise_for_status()
        else:
            print(f"[PUBLISH] deactivate failed for {company}: {e}")


def upsert(url_base: str, key: str, rows: List[dict]) -> None:
    """Batched merge-upsert with 429/5xx retries and a 400-body report."""
    if not rows:
        return
    endpoint = f"{_rest(url_base, TABLE_JOBS)}?on_conflict=company,url"
    headers = _headers(key)
    headers["Prefer"] = "resolution=merge-duplicates"
    total_batches = (len(rows) - 1) // BATCH_SIZE + 1
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        n = i // BATCH_SIZE + 1
        for attempt in range(3):
            try:
                r = requests.post(endpoint, headers=headers, json=batch, timeout=120)
                r.raise_for_status()
                print(f"[PUBLISH] upserted batch {n}/{total_batches} ({len(batch)} rows)")
                break
            except HTTPError as e:
                resp = e.response
                status = resp.status_code if resp is not None else 0
                if status in (429, 500, 502, 503, 504):
                    wait = 3 * (attempt + 1)
                    print(f"[PUBLISH] {status} on batch {n}, retry {attempt + 1} in {wait}s")
                    time.sleep(wait)
                    continue
                if resp is not None:
                    print(f"[PUBLISH] {status} on batch {n}/{total_batches}: {(resp.text or '')[:500]}")
                raise


def record_run(url_base: str, key: str, provider: str, status: str, job_count: int,
               error_summary: str = "", trigger: str = "scheduled") -> None:
    body: dict = {"provider": provider, "status": status, "job_count": job_count,
                  "trigger_source": trigger}
    if error_summary:
        body["error_summary"] = error_summary
    try:
        requests.post(_rest(url_base, TABLE_RUNS), headers=_headers(key),
                      json=[body], timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"[TELEMETRY] failed to record scrape run: {e}")


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def _is_recently_posted(posted_at: Optional[str]) -> bool:
    if not posted_at:
        return False
    try:
        dt = datetime.fromisoformat(posted_at)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(days=POSTED_DAYS_MAX)


def _embed(job: dict) -> dict:
    description = " ".join((job.get("description") or "").split())
    if len(description) > 350:
        description = description[:347] + "..."
    embed = {
        "title": (job.get("title") or "New robotics job posting")[:256],
        "url": (job.get("url") or "")[:2048] or None,
        "description": description or "New robotics job discovered.",
        "color": 0x34D399,
        "fields": [
            {"name": "Company", "value": (job.get("company") or "Unknown")[:256], "inline": True},
            {"name": "Location", "value": (job.get("location") or "Unknown")[:1024], "inline": True},
        ],
        "footer": {"text": "ABET Robotics Jobs Monitor"},
        "timestamp": job.get("posted_at") or job.get("scraped_at") or now_iso(),
    }
    ats = job.get("ats")
    if ats:
        embed["fields"].insert(0, {"name": "Source", "value": str(ats)[:256], "inline": True})
    return embed


def notify_new_jobs(new_jobs: List[dict]) -> None:
    if not DISCORD_WEBHOOK_JOBS and not DRY_RUN:
        print("[DISCORD] DISCORD_WEBHOOK_JOBS not configured; skipping new job alerts")
        return
    recent = [j for j in new_jobs if _is_recently_posted(j.get("posted_at"))]
    if not recent:
        print("[DISCORD] No recently posted jobs to announce")
        return
    for idx in range(0, len(recent), CHUNK_SIZE):
        chunk = recent[idx : idx + CHUNK_SIZE]
        payload = {"content": f"New robotics jobs discovered: {len(chunk)}",
                   "embeds": [_embed(j) for j in chunk]}
        if DRY_RUN:
            print(f"[DRY-RUN] Chunk {idx // CHUNK_SIZE + 1}: {json.dumps(payload, indent=2)[:400]}")
            continue
        for attempt in range(3):
            r = requests.post(DISCORD_WEBHOOK_JOBS, json=payload, timeout=30)
            if r.status_code == 429:
                wait = 3 * (attempt + 1)
                print(f"[DISCORD] 429 on chunk {idx // CHUNK_SIZE + 1}, retrying in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        time.sleep(0.5)
    print(f"[DISCORD] Posted {len(recent)} new robotics jobs")


def notify_failure(error_message: str) -> None:
    if not DISCORD_WEBHOOK_JOBS:
        return
    embed = {
        "title": "Robotics Scrape Failed",
        "description": f"```\n{error_message[:1500]}\n```",
        "color": 0xFF0055,
        "footer": {"text": "ABET Robotics Jobs Monitor"},
        "timestamp": now_iso(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_JOBS,
                      json={"content": "@here Robotics scrape failure", "embeds": [embed]}, timeout=30)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def serialize(job) -> dict:
    """Job contract row -> tech_jobs row (source_payload keeps raw/clean).

    Empty description fields are OMITTED so the merge-upsert keeps existing
    descriptions instead of wiping them (content-less boards, e.g. greenhouse
    boards fetched with content=false).
    """
    row = {
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "location": job.location,
        "is_active": bool(job.is_active),
        "last_seen_at": now_iso(),
        "scraped_at": now_iso(),
        "ats": job.source,
        "posted_at": job.posted_at,
    }
    if job.description or job.description_raw or job.description_clean:
        row["description"] = job.description
        row["source_payload"] = {
            "description_raw": job.description_raw,
            "description_clean": job.description_clean,
            "provider_slug": job.provider_slug,
            "run_id": job.run_id,
            "salary": job.salary,
            "remote": job.remote,
        }
    else:
        row["source_payload"] = {
            "provider_slug": job.provider_slug,
            "run_id": job.run_id,
            "salary": job.salary,
            "remote": job.remote,
        }
    return row


def publish(jobs) -> None:
    """Full publish cycle for one scrape run: upsert -> deactivate -> announce."""
    url_base, key = supabase_env()
    grouped: Dict[str, list] = {}
    for job in jobs:
        grouped.setdefault(job.company, []).append(job)

    new_jobs: List[dict] = []
    for company, company_jobs in grouped.items():
        existing_urls = fetch_existing_urls(url_base, key, company)
        seen: Set[str] = set()
        rows: List[dict] = []
        for job in company_jobs:
            row = serialize(job)
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            rows.append(row)
        upsert(url_base, key, [r for r in rows if "description" in r])
        upsert(url_base, key, [r for r in rows if "description" not in r])
        new_jobs.extend(r for r in rows if r["url"] not in existing_urls)
        removed = existing_urls - seen
        if removed:
            deactivate(url_base, key, company, removed)
        print(f"[PUBLISH] upserted {len(rows)} (deactivated {len(removed)}) for {company}")

    notify_new_jobs(new_jobs)
