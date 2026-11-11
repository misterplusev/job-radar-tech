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

Tables (this module ONLY, tech namespaced):
  tech_jobs             postings
  tech_job_scrape_runs  per-provider telemetry
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
TABLE_NOTIFICATIONS = "tech_job_notifications"
# Must match db/schema_tech.sql. This was hardcoded to the robotics table, so
# every tech receipt was written into public.robotics_job_scrape_runs and the
# tech telemetry table stayed permanently empty (verified: 0 rows in prod).
# NOTE: the table really is lowercase. Postgres folds unquoted identifiers, so
# `create table public.Tech_job_scrape_runs` created `tech_job_scrape_runs`.
TABLE_RUNS = "tech_job_scrape_runs"
BATCH_SIZE = 100
CHUNK_SIZE = 5
POSTED_DAYS_MAX = 14
PROVIDER_TIMEOUT = 120
DISCORD_PACE_SECONDS = 2.1
BOARD_LOGOS_BASE = "https://misterplusev.github.io/jobradar-tech-board/logos"


def _logo_url(company: Optional[str]) -> Optional[str]:
    slug = ((company or "").strip().lower().replace(" ", "-").replace(".", "-"))
    return f"{BOARD_LOGOS_BASE}/{slug}.png" if slug else None


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


def _discord_retry_after(response) -> float:
    """Parse Discord's retry_after (seconds) from a 429 response, bounded."""
    try:
        body = response.json()
        wait = float(body.get("retry_after", 0) or 0)
    except Exception:  # noqa: BLE001
        wait = 0.0
    if wait > 120:
        raise RuntimeError("Discord rate-limit wait exceeds delivery budget; preserve pending queue")
    return max(wait, 1.0)


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


def _deactivation_batches(url_base: str, company: str, urls: Set[str]):
    """Keep encoded request URLs below 6 KB, including PostgREST quoting."""
    batch = []
    for url in sorted(urls):
        candidate = batch + [url]
        params = {"company": f"eq.{company}", "is_active": "eq.true",
                  "url": "in.(" + ",".join(json.dumps(u) for u in candidate) + ")",
                  "select": "id"}
        prepared = requests.Request("PATCH", _rest(url_base, TABLE_JOBS), params=params).prepare()
        if len(prepared.url.encode("utf-8")) > 6000:
            if not batch:
                raise ValueError("A job URL exceeds the safe cleanup request size")
            yield batch
            batch = [url]
            single = dict(params, url="in.(" + json.dumps(url) + ")")
            prepared = requests.Request("PATCH", _rest(url_base, TABLE_JOBS), params=single).prepare()
            if len(prepared.url.encode("utf-8")) > 6000:
                raise ValueError("A job URL exceeds the safe cleanup request size")
        else:
            batch = candidate
    if batch:
        yield batch


def deactivate(url_base: str, key: str, company: str, urls: Set[str]) -> int:
    """Retire absent active URLs in bounded batches; return confirmed row count."""
    updated = 0
    headers = dict(_headers(key), Prefer="return=representation")
    for batch in _deactivation_batches(url_base, company, urls):
        params = {"company": f"eq.{company}", "is_active": "eq.true",
                  "url": "in.(" + ",".join(json.dumps(u) for u in batch) + ")",
                  "select": "id"}
        payload = {"is_active": False, "closed_at": now_iso()}
        response = requests.patch(_rest(url_base, TABLE_JOBS), headers=headers,
                                  params=params, json=payload, timeout=120)
        if (response.status_code == 400 and
                "closed_at" in (response.text or "").lower()):
            response = requests.patch(_rest(url_base, TABLE_JOBS), headers=headers,
                                      params=params, json={"is_active": False}, timeout=120)
        response.raise_for_status()
        updated += len(response.json())
    return updated


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
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    wait = 3 * (attempt + 1)
                    print(f"[PUBLISH] {status} on batch {n}, retry {attempt + 1} in {wait}s")
                    time.sleep(wait)
                    continue
                if resp is not None:
                    print(f"[PUBLISH] {status} on batch {n}/{total_batches}: {(resp.text or '')[:500]}")
                raise


def record_run(url_base: str, key: str, provider: str, status: str, job_count: int,
               error_summary: str = "", trigger: str = "") -> None:
    body = {"provider": provider, "status": status, "job_count": job_count,
            "trigger_source": trigger or os.environ.get("GITHUB_EVENT_NAME", "manual")}
    if error_summary:
        body["error_summary"] = error_summary
    response = requests.post(_rest(url_base, TABLE_RUNS), headers=_headers(key),
                             json=[body], timeout=60)
    response.raise_for_status()


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
        "title": (job.get("title") or "New tech job posting")[:256],
        "url": (job.get("url") or "")[:2048] or None,
        "description": description or "New tech job discovered.",
        "color": 0x34D399,
        "fields": [
            {"name": "Company", "value": (job.get("company") or "Unknown")[:256], "inline": True},
            {"name": "Location", "value": (job.get("location") or "Unknown")[:1024], "inline": True},
        ],
        "footer": {"text": "ABET Tech Jobs Monitor"},
        "timestamp": job.get("posted_at") or job.get("scraped_at") or now_iso(),
    }
    ats = job.get("ats")
    if ats:
        embed["fields"].insert(0, {"name": "Source", "value": str(ats)[:256], "inline": True})
    thumb = _logo_url(job.get("company"))
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    return embed


def queue_new_jobs(url_base: str, key: str, new_jobs: List[dict]) -> int:
    """Persist intent BEFORE job upsert; conflicts preserve prior delivery state."""
    recent = [j for j in new_jobs if _is_recently_posted(j.get("posted_at"))]
    # Tech preserves its existing date-only notification eligibility.
    if DRY_RUN:
        return len(recent)
    headers = dict(_headers(key), Prefer="resolution=ignore-duplicates")
    endpoint = _rest(url_base, TABLE_NOTIFICATIONS) + "?on_conflict=company,job_url"
    for start in range(0, len(recent), BATCH_SIZE):
        rows = [{"company": j["company"], "job_url": j["url"], "payload": j}
                for j in recent[start:start + BATCH_SIZE]]
        response = requests.post(endpoint, headers=headers, json=rows, timeout=60)
        response.raise_for_status()
    return len(recent)


def _send_notification_chunk(jobs: List[dict]) -> str:
    payload = {"content": f"New tech jobs discovered: {len(jobs)}",
               "embeds": [_embed(j) for j in jobs]}
    for attempt in range(8):
        response = requests.post(DISCORD_WEBHOOK_JOBS, params={"wait": "true"},
                                 json=payload, timeout=30)
        if response.status_code == 429 and attempt < 7:
            time.sleep(_discord_retry_after(response))
            continue
        response.raise_for_status()
        message_id = response.json().get("id")
        if not message_id:
            raise RuntimeError("Discord did not return a delivery message ID")
        return str(message_id)
    raise RuntimeError("Discord retries exhausted")


def _ack_notifications(url_base: str, key: str, ids: List[int], state: str,
                       message_id: str = "") -> None:
    payload = {"state": state}
    if state == "delivered":
        payload.update(delivered_at=now_iso(), discord_message_id=message_id)
    response = requests.patch(_rest(url_base, TABLE_NOTIFICATIONS), headers=_headers(key),
                              params={"id": "in.(" + ",".join(str(i) for i in ids) + ")",
                                      "state": "eq.pending"}, json=payload, timeout=60)
    response.raise_for_status()


def notify_new_jobs(new_jobs: List[dict]) -> int:
    """Drain durable pending intent, acknowledging only successful Discord chunks.

    Workflow concurrency must serialize this publisher. Delivery is at-least-once:
    a crash after Discord accepts but before the DB acknowledgement can repeat a chunk.
    """
    if DRY_RUN:
        print(f"[DRY-RUN] {len(new_jobs)} candidate notifications; no state changed")
        return 0
    url_base, key = supabase_env()
    queue_new_jobs(url_base, key, new_jobs)
    delivered = 0
    cursor = 0
    deadline = time.monotonic() + 600
    for _page in range(1000):
        if time.monotonic() >= deadline:
            break
        response = requests.get(_rest(url_base, TABLE_NOTIFICATIONS), headers=_headers(key),
                                params={"state": "eq.pending", "id": f"gt.{cursor}",
                                        "order": "id.asc", "limit": 100,
                                        "select": "id,company,job_url,payload"}, timeout=60)
        response.raise_for_status()
        pending = response.json()
        if not pending:
            break
        ready = []
        for item in pending:
            if time.monotonic() >= deadline:
                break
            cursor = item["id"]
            job = item["payload"]
            if not _is_recently_posted(job.get("posted_at")):
                _ack_notifications(url_base, key, [item["id"]], "expired")
                continue
            # An interrupted queue-before-upsert must never announce an unsaved job.
            check = requests.get(_rest(url_base, TABLE_JOBS), headers=_headers(key),
                                 params={"company": f"eq.{item['company']}",
                                         "url": f"eq.{item['job_url']}",
                                         "select": "id,is_active", "limit": 1}, timeout=30)
            check.raise_for_status()
            saved = check.json()
            if not saved:
                continue  # Preserve pending intent for a later successful ingestion.
            if not saved[0]["is_active"]:
                _ack_notifications(url_base, key, [item["id"]], "inactive")
                continue
            ready.append(item)
            if len(ready) == CHUNK_SIZE:
                if not DISCORD_WEBHOOK_JOBS:
                    raise RuntimeError("Pending alerts exist but DISCORD_WEBHOOK_JOBS is missing")
                message_id = _send_notification_chunk([r["payload"] for r in ready])
                _ack_notifications(url_base, key, [r["id"] for r in ready], "delivered", message_id)
                delivered += len(ready)
                ready.clear()
                time.sleep(DISCORD_PACE_SECONDS)
        if ready:
            if not DISCORD_WEBHOOK_JOBS:
                raise RuntimeError("Pending alerts exist but DISCORD_WEBHOOK_JOBS is missing")
            message_id = _send_notification_chunk([r["payload"] for r in ready])
            _ack_notifications(url_base, key, [r["id"] for r in ready], "delivered", message_id)
            delivered += len(ready)
            time.sleep(DISCORD_PACE_SECONDS)
    print(f"[DISCORD] Confirmed {delivered} delivered tech jobs; other pending jobs remain queued")
    return delivered


def notify_failure(error_message: str) -> None:
    if not DISCORD_WEBHOOK_JOBS:
        return
    embed = {
        "title": "Tech Scrape Failed",
        "description": f"```\n{error_message[:1500]}\n```",
        "color": 0xFF0055,
        "footer": {"text": "ABET Tech Jobs Monitor"},
        "timestamp": now_iso(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_JOBS,
                      json={"content": "@here Tech scrape failure", "embeds": [embed]}, timeout=30)
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
        "closed_at": None if job.is_active else now_iso(),
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


def retire_empty_sources(url_base, key, empty_providers):
    """Only sources verified empty are reconciled; active rows carry their source identity."""
    total = 0
    for source, slug in empty_providers:
        # Always refetch page zero because successful retirement removes these rows.
        for _page in range(1000):
            response = requests.get(_rest(url_base, TABLE_JOBS), headers=_headers(key),
                params={"ats": f"eq.{source}", "source_payload->>provider_slug": f"eq.{slug}",
                        "is_active": "eq.true", "select": "company,url", "order": "id", "limit": 100}, timeout=60)
            response.raise_for_status()
            rows = response.json()
            if not rows:
                break
            grouped = {}
            for row in rows:
                grouped.setdefault(row["company"], set()).add(row["url"])
            for company, urls in grouped.items():
                total += deactivate(url_base, key, company, urls)
        else:
            raise RuntimeError("Empty-source cleanup exceeded safety limit")
    return total


def publish(jobs, *, allow_deactivate: bool = True, empty_providers=()) -> dict:
    """Full publish cycle for one scrape run: upsert -> deactivate -> announce."""
    url_base, key = supabase_env()
    totals = {"upserted": 0, "deactivated": 0, "queued_candidates": 0, "delivered": 0}
    grouped: Dict[str, list] = {}
    for job in jobs:
        grouped.setdefault(job.company, []).append(job)

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
        totals["queued_candidates"] += queue_new_jobs(url_base, key, [r for r in rows if r["url"] not in existing_urls])
        upsert(url_base, key, [r for r in rows if "description" in r])
        upsert(url_base, key, [r for r in rows if "description" not in r])
        removed = existing_urls - seen
        deactivated = deactivate(url_base, key, company, removed) if allow_deactivate else 0
        totals["upserted"] += len(rows)
        totals["deactivated"] += deactivated
        print(f"[PUBLISH] upserted {len(rows)} (deactivated {deactivated}) for {company}")
        if removed and not allow_deactivate:
            print(f"[PUBLISH] cleanup deferred for {company}: incomplete provider run")

    if allow_deactivate:
        totals["deactivated"] += retire_empty_sources(url_base, key, empty_providers)
    totals["delivered"] = notify_new_jobs([])
    return totals

