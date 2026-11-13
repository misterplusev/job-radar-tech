"""Robotics jobs publisher — Supabase persistence + Discord alerts for Job Radar.

Own implementation for the robotics module (independent of every other scraper
module). Behavior contract:

  scrape  ->  group by company
          ->  fetch existing robotics_jobs urls (paginated)
          ->  upsert new/seen rows (on_conflict=company,url, batched, retried)
          ->  deactivate urls that vanished from the provider (closed_at fallback)
          ->  relevance-gate the announce (storage is NEVER filtered)
          ->  Discord-announce NEW jobs posted within POSTED_DAYS_MAX days
              (5-embed chunks, 429-aware retries, 0.5s spacing)
          ->  failure alerts to the same webhook

Tables (this module ONLY, robotics namespaced):
  robotics_jobs             postings
  robotics_job_scrape_runs  per-provider telemetry
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import requests
from requests import HTTPError, Timeout, ConnectionError

try:  # the relevance filter ships only where a module uses it (robotics)
    from .relevance import explain as _relevance_explain
    from .relevance import is_relevant as _is_relevant
except ImportError:  # no relevance module here (the tech repository) or an offline test harness
    _relevance_explain = globals().get("_relevance_explain")
    _is_relevant = globals().get("_is_relevant")

DISCORD_WEBHOOK_JOBS = os.environ.get("DISCORD_WEBHOOK_JOBS", "").strip()
DRY_RUN = False

BASE_URL = "https://api.github.com"  # unused placeholder to keep module import-safe

# COMMON DISCORD SENDER
# queue_new_jobs() -> notify_new_jobs() -> _send_notification_chunk() -> _ack_notifications()
# is the one delivery path for ABET job modules: a durable row per job in the module's
# notification table, sent with ?wait=true, acknowledged only with the Discord message ID.
# JOBRADAR_MODULE selects which module this process delivers for. Exactly one process
# may deliver for a module (workflow concurrency groups enforce it).
DELIVERY_MODULES = {
    "robotics": {
        "jobs_table": "robotics_jobs", "notifications_table": "robotics_job_notifications",
        "runs_table": "robotics_job_scrape_runs", "label": "robotics", "color": 0x34D399,
        "footer": "ABET Robotics Jobs Monitor", "default_title": "New robotics job posting",
        "default_description": "New robotics job discovered.", "posted_field": False,
        "payload_logo": False, "record_destination": False,
        "logo_base": "https://misterplusev.github.io/robotics-jobradar-board/logos",
        "relevance_filter": True, "failure_title": "Robotics",
        # Robotics announces only postings dated within POSTED_DAYS_MAX days (existing behaviour).
        "expire_old_postings": True,
    },
    # Tech is the same pipeline as robotics with its own tables, board and wording, and no
    # relevance filter. This file is byte-identical in misterplusev/job-radar-tech; a test in
    # each repository pins its hash so the two copies cannot drift.
    "tech": {
        "jobs_table": "tech_jobs", "notifications_table": "tech_job_notifications",
        "runs_table": "tech_job_scrape_runs", "label": "tech", "color": 0x34D399,
        "footer": "ABET Tech Jobs Monitor", "default_title": "New tech job posting",
        "default_description": "New tech job discovered.", "posted_field": False,
        "payload_logo": False, "record_destination": False,
        "logo_base": "https://misterplusev.github.io/jobradar-tech-board/logos",
        "relevance_filter": False, "failure_title": "Tech",
        "expire_old_postings": True,
    },
    # Sportsbook jobs are scraped on a Hugging Face Space, which cannot reach Discord.
    # The Space queues rows in sportsbook_job_notifications; `run.py deliver` drains them here.
    "sportsbook": {
        "jobs_table": "jobs", "notifications_table": "sportsbook_job_notifications",
        "runs_table": "job_scrape_runs", "label": "sportsbook", "color": 0x38BDF8,
        "footer": "ABET Sportsbook Jobs Monitor", "default_title": "New job posting",
        "default_description": "New sportsbook job discovered.", "posted_field": True,
        "payload_logo": True, "record_destination": True,
        "logo_base": "", "relevance_filter": False, "failure_title": "Sportsbook",
        # The sportsbook sender this replaces announces EVERY new active job, whatever its
        # posting date. Keep that: nothing is dropped for age.
        "expire_old_postings": False,
    },
}
MODULE_NAME = os.environ.get("JOBRADAR_MODULE", "robotics").strip() or "robotics"
if MODULE_NAME not in DELIVERY_MODULES:
    raise RuntimeError(f"Unknown JOBRADAR_MODULE {MODULE_NAME!r}")
MODULE = DELIVERY_MODULES[MODULE_NAME]
TABLE_JOBS = MODULE["jobs_table"]
TABLE_NOTIFICATIONS = MODULE["notifications_table"]
TABLE_RUNS = MODULE["runs_table"]
LAST_DESTINATION: dict = {}
BATCH_SIZE = 100
CHUNK_SIZE = 5
POSTED_DAYS_MAX = 14
PROVIDER_TIMEOUT = 120
BOARD_LOGOS_BASE = MODULE["logo_base"]
RELEVANCE_ON = (MODULE["relevance_filter"] and _is_relevant is not None
                and os.environ.get("DISCORD_RELEVANCE_FILTER", "1").strip() not in ("0", "false", "False"))
SUPPRESS_SAMPLE = 5
# Discord webhooks are capped at ~30 requests/minute; pace chunks so a big
# backlog announce stays under the limit instead of hammering 429s.
DISCORD_PACE_SECONDS = 2.1
# Sending the full description and source_payload for every stored posting on
# every run rewrote the whole table each time and overloaded the shared database
# (2026-11-11). Each stored posting now carries a hash of its scraped content in
# source_payload. A posting is rewritten in full only when that hash changes, so an
# employer's edit is picked up on the next run; unchanged postings are refreshed
# with TOUCH_COLUMNS only.
TOUCH_COLUMNS = ("company", "title", "url", "location", "is_active", "closed_at",
                 "last_seen_at", "scraped_at", "ats", "posted_at")
# v2 (2026-11-12): source_payload no longer carries description_raw and
# description_clean. Those two copies were two-thirds of the table and pushed the
# shared free database past its 500 MB limit. The key was renamed so every stored
# row counts as "no hash yet" and is rewritten through the capped backfill below
# (BACKFILL_PER_RUN per run) instead of all at once.
HASH_KEY = "content_hash_v2"
HASH_FIELDS = ("title", "location", "description", "ats", "posted_at")
# Postings stored before hashing existed have no hash yet. They are rewritten once,
# at most this many per run, so the first runs after deployment stay small.
BACKFILL_PER_RUN = 1500
FULL_REFRESH = os.environ.get("JOBRADAR_FULL_REFRESH", "").strip() in ("1", "true", "True")


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
def fetch_existing_state(url_base: str, key: str, company: str) -> Dict[str, dict]:
    """Known url -> {is_active, hash} for a company (paginated, 1000/page)."""
    state: Dict[str, dict] = {}
    offset = 0
    while True:
        r = requests.get(
            _rest(url_base, TABLE_JOBS),
            headers=_headers(key),
            params={"company": f"eq.{company}",
                    "select": f"url,is_active,{HASH_KEY}:source_payload->>{HASH_KEY}",
                    "limit": 1000, "offset": offset},
            timeout=120,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            if row.get("url"):
                state[row["url"]] = {"is_active": bool(row.get("is_active", True)),
                                     "hash": row.get(HASH_KEY) or None}
        if len(rows) < 1000:
            break
        offset += 1000
    return state


def fetch_existing_urls(url_base: str, key: str, company: str) -> Set[str]:
    """All known urls for a company, active or not."""
    return set(fetch_existing_state(url_base, key, company))


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
    """Idempotent merge-upsert; bounded HTTP and transport-error retries."""
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
            except (HTTPError, Timeout, ConnectionError) as e:
                resp = e.response
                status = resp.status_code if resp is not None else 0
                retryable = isinstance(e, (Timeout, ConnectionError)) or status in (429, 500, 502, 503, 504)
                if retryable and attempt < 2:
                    wait = 3 * (attempt + 1)
                    reason = type(e).__name__ if not status else str(status)
                    print(f"[PUBLISH] {reason} on batch {n}, retry {attempt + 1} in {wait}s")
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
        "title": (job.get("title") or MODULE["default_title"])[:256],
        "url": (job.get("url") or "")[:2048] or None,
        "description": description or MODULE["default_description"],
        "color": MODULE["color"],
        "fields": [
            {"name": "Company", "value": (job.get("company") or "Unknown")[:256], "inline": True},
            {"name": "Location", "value": (job.get("location") or "Unknown")[:1024], "inline": True},
        ],
        "footer": {"text": MODULE["footer"]},
        "timestamp": _embed_timestamp(job),
    }
    ats = job.get("ats")
    if ats:
        embed["fields"].insert(0, {"name": "Source", "value": str(ats)[:256], "inline": True})
    if MODULE["posted_field"] and job.get("posted_at"):
        embed["fields"].append({"name": "Posted", "value": str(job["posted_at"])[:256], "inline": True})
    thumb = job.get("logo_url") if MODULE["payload_logo"] else _logo_url(job.get("company"))
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    return embed


def _embed_timestamp(job: dict) -> str:
    """Discord rejects the whole message for a non-ISO8601 timestamp. Robotics passes
    its stored value through as before; other modules normalise date-only values."""
    value = job.get("posted_at") or job.get("scraped_at") or now_iso()
    if MODULE_NAME == "robotics":
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now_iso()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _split_relevant(jobs: List[dict]) -> tuple[List[dict], List[dict]]:
    """Partition new postings into (announce, suppress). Storage is untouched.

    Kill switch: DISCORD_RELEVANCE_FILTER=0 restores the old announce-everything
    behavior without a code change.
    """
    if not RELEVANCE_ON:
        return list(jobs), []
    keep: List[dict] = []
    drop: List[dict] = []
    for j in jobs:
        (keep if _is_relevant(j.get("title"), j.get("description"), j.get("company")) else drop).append(j)
    if drop:
        print(f"[RELEVANCE] suppressed {len(drop)} non-{MODULE['label']} postings ({len(keep)} kept)")
        for job in drop[:SUPPRESS_SAMPLE]:
            _, reason = _relevance_explain(job.get("title"), job.get("description"), job.get("company"))
            print(f"[RELEVANCE]   - {job.get('company')}: {(job.get('title') or '')[:70]} ({reason})")
        if len(drop) > SUPPRESS_SAMPLE:
            print(f"[RELEVANCE]   ... and {len(drop) - SUPPRESS_SAMPLE} more")
    return keep, drop


def queue_new_jobs(url_base: str, key: str, new_jobs: List[dict]) -> int:
    """Persist intent BEFORE job upsert; conflicts preserve prior delivery state."""
    recent = [j for j in new_jobs if _is_recently_posted(j.get("posted_at"))]
    recent, _suppressed = _split_relevant(recent)
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


def send_discord_message(webhook: str, payload: dict, *, headers: Optional[dict] = None) -> dict:
    """THE one function that posts a job message to Discord, for every ABET jobs module
    (robotics, tech and sportsbook through notify_new_jobs below; sports teams calls it from
    sports_teams/publish_teams.py).

    `?wait=true` makes Discord answer with the stored message. Returns that message (it
    carries `id`, `channel_id`, `webhook_id`). Waits out HTTP 429 using Discord's
    `retry_after`, at most 8 attempts. Raises if Discord refuses the message or returns no
    message ID, so a caller can never record a delivery that Discord did not confirm.
    """
    for attempt in range(8):
        response = requests.post(webhook, params={"wait": "true"}, json=payload, timeout=30,
                                 **({"headers": headers} if headers else {}))
        if response.status_code == 429 and attempt < 7:
            time.sleep(_discord_retry_after(response))
            continue
        response.raise_for_status()
        body = response.json()
        if not body.get("id"):
            raise RuntimeError("Discord did not return a delivery message ID")
        return body
    raise RuntimeError("Discord retries exhausted")


def _send_notification_chunk(jobs: List[dict]) -> str:
    payload = {"content": f"New {MODULE['label']} jobs discovered: {len(jobs)}",
               "embeds": [_embed(j) for j in jobs]}
    body = send_discord_message(DISCORD_WEBHOOK_JOBS, payload)
    LAST_DESTINATION.clear()
    LAST_DESTINATION.update(channel_id=body.get("channel_id"), webhook_id=body.get("webhook_id"))
    return str(body["id"])


def _ack_notifications(url_base: str, key: str, ids: List[int], state: str,
                       message_id: str = "") -> None:
    payload = {"state": state}
    if state == "delivered":
        payload.update(delivered_at=now_iso(), discord_message_id=message_id)
        if MODULE["record_destination"]:
            payload.update(discord_channel_id=LAST_DESTINATION.get("channel_id"),
                           discord_webhook_id=LAST_DESTINATION.get("webhook_id"))
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
            if MODULE["expire_old_postings"] and not _is_recently_posted(job.get("posted_at")):
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
    print(f"[DISCORD] Confirmed {delivered} delivered {MODULE['label']} jobs; other pending jobs remain queued")
    return delivered


def notify_failure(error_message: str) -> None:
    if not DISCORD_WEBHOOK_JOBS:
        return
    embed = {
        "title": f"{MODULE['failure_title']} Scrape Failed",
        "description": f"```\n{error_message[:1500]}\n```",
        "color": 0xFF0055,
        "footer": {"text": MODULE["footer"]},
        "timestamp": now_iso(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_JOBS,
                      json={"content": f"@here {MODULE['failure_title']} scrape failure", "embeds": [embed]}, timeout=30)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def serialize(job) -> dict:
    """Job contract row -> robotics_jobs row (one description copy; source_payload holds small fields only).

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
        # ONE copy of the description. Do not add description_raw/description_clean
        # back into source_payload: nothing reads them and they tripled the table.
        row["description"] = job.description or job.description_clean or ""
        row["source_payload"] = {
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


def _touch_row(row: dict) -> dict:
    """Tracking columns only, for a stored posting whose content is unchanged."""
    return {k: row[k] for k in TOUCH_COLUMNS}


def _content_hash(row: dict) -> str:
    """Stable hash of what the employer published; ignores per-run bookkeeping."""
    payload = {k: v for k, v in (row.get("source_payload") or {}).items()
               if k not in ("run_id", HASH_KEY)}
    basis = {k: row.get(k) for k in HASH_FIELDS}
    basis["source_payload"] = payload
    encoded = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


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
    totals = {"upserted": 0, "deactivated": 0, "queued_candidates": 0, "delivered": 0,
              "written_new": 0, "written_changed": 0, "written_backfill": 0, "touched": 0}
    backfill_budget = BACKFILL_PER_RUN
    grouped: Dict[str, list] = {}
    for job in jobs:
        grouped.setdefault(job.company, []).append(job)

    for company, company_jobs in grouped.items():
        existing = fetch_existing_state(url_base, key, company)
        existing_urls = set(existing)
        seen: Set[str] = set()
        rows: List[dict] = []
        for job in company_jobs:
            row = serialize(job)
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            row["source_payload"][HASH_KEY] = _content_hash(row)
            rows.append(row)
        new_rows = [r for r in rows if r["url"] not in existing_urls]
        totals["queued_candidates"] += queue_new_jobs(url_base, key, new_rows)
        # New postings and postings whose content changed are written in full.
        # Unchanged postings only get their tracking columns refreshed.
        full_rows = list(new_rows)
        touch_rows: List[dict] = []
        changed = backfilled = 0
        for row in rows:
            if row["url"] not in existing_urls:
                continue
            stored_hash = existing[row["url"]]["hash"]
            if FULL_REFRESH or (stored_hash and stored_hash != row["source_payload"][HASH_KEY]):
                full_rows.append(row)
                changed += 1
            elif not stored_hash and backfill_budget > 0:
                full_rows.append(row)
                backfilled += 1
                backfill_budget -= 1
            else:
                touch_rows.append(row)
        upsert(url_base, key, [r for r in full_rows if "description" in r])
        upsert(url_base, key, [r for r in full_rows if "description" not in r])
        upsert(url_base, key, [_touch_row(r) for r in touch_rows])
        totals["written_new"] += len(new_rows)
        totals["written_changed"] += changed
        totals["written_backfill"] += backfilled
        totals["touched"] += len(touch_rows)
        # Only postings that are still marked active can newly disappear.
        removed = {u for u, stored in existing.items() if stored["is_active"]} - seen
        deactivated = deactivate(url_base, key, company, removed) if allow_deactivate else 0
        totals["upserted"] += len(rows)
        totals["deactivated"] += deactivated
        print(f"[PUBLISH] upserted {len(rows)} (deactivated {deactivated}) for {company}")
        if removed and not allow_deactivate:
            print(f"[PUBLISH] cleanup deferred for {company}: incomplete provider run")

    if allow_deactivate:
        totals["deactivated"] += retire_empty_sources(url_base, key, empty_providers)
    print(f"[PUBLISH] full writes: {totals['written_new']} new, {totals['written_changed']} changed, "
          f"{totals['written_backfill']} first-time hash; light refresh: {totals['touched']}")
    totals["delivered"] = notify_new_jobs([])
    return totals

