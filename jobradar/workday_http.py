"""Workday read-only HTTP policy. No database writes or notification delivery."""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

ATTEMPTS = 5
BACKOFF_SECONDS = (5, 10, 20, 40)
HOST_INTERVAL_SECONDS = 0.25
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_LOG_BYTES = 256 * 1024
_SOURCE_DEADLINE = ContextVar("workday_source_deadline", default=None)
_HOSTS = {}
_HOSTS_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_LOG_DROPPED = 0
_LOG_NAME = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + f"-{os.getpid()}.jsonl"


class WorkdayChallenge(RuntimeError):
    """Explicit upstream browser/security challenge; not an ordinary rate-limit retry."""
    retryable = False
    failure_kind = "access_challenge"


class WorkdayDeferred(RuntimeError):
    """Source remains incomplete; do not publish a partial source or close jobs."""


@contextmanager
def source_budget(deadline):
    token = _SOURCE_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _SOURCE_DEADLINE.reset(token)


def is_workday_read(method, url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")):
        return False
    if not parsed.path.startswith("/wday/cxs/"):
        return False
    if method.upper() in ("GET", "HEAD"):
        return True
    return method.upper() == "POST" and bool(
        re.fullmatch(r"/wday/cxs/[^/]+/[^/]+/jobs/?", parsed.path))


def retry_after_seconds(raw, now=None):
    if not raw:
        return None
    raw = str(raw).strip()
    if re.fullmatch(r"[0-9]{1,10}", raw):
        return float(raw)
    try:
        date = parsedate_to_datetime(raw)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - (time.time() if now is None else now))
    except (ValueError, TypeError, OverflowError):
        return None


def diagnostic_path():
    root = Path(os.environ.get("ABET_BASE_DIR") or ".")
    return root / "data/runtime/workday-http" / _LOG_NAME


def _clean(value, limit=320):
    value = str(value)
    for name, secret in os.environ.items():
        if len(secret) >= 8 and any(k in name.upper() for k in ("TOKEN", "KEY", "SECRET", "PASSWORD", "WEBHOOK")):
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", "", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"(?i)Client IP:\s*[0-9a-f:.]+", "Client IP: [redacted]", value)
    value = re.sub(r"https?://[^\s<>]+", "[url]", value)
    value = re.sub(r"(?i)\b(?:bearer\s+\S+|[\w.+-]+@[\w.-]+\.[a-z]{2,})", "[redacted]", value)
    return " ".join(value.split())[:limit]


def emit(event):
    global _LOG_DROPPED
    record = {"ts": datetime.now(timezone.utc).isoformat(),
              "run_id": os.environ.get("GITHUB_RUN_ID") or os.environ.get("ABET_JOBS_RUN_ID", ""),
              **event}
    line = json.dumps(record, ensure_ascii=True, sort_keys=True)
    print("[WORKDAY_HTTP] " + line, file=sys.stderr, flush=True)
    # Full attempt evidence goes to bounded cloud files, not Supabase.
    with _LOG_LOCK:
        path = diagnostic_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            files = sorted(path.parent.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[47:]:
                old.unlink()
            for old in files[:47]:
                if time.time() - old.stat().st_mtime > 7 * 86400:
                    old.unlink(missing_ok=True)
            if not path.exists() or path.stat().st_size + len(line.encode()) + 1 <= MAX_LOG_BYTES:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            else:
                _LOG_DROPPED += 1
                if _LOG_DROPPED == 1:
                    print("[WORKDAY_HTTP] file_limit_reached; full events continue in stderr", file=sys.stderr, flush=True)
        except OSError:
            print("[WORKDAY_HTTP] diagnostic_file_write_failed; see cloud stderr", file=sys.stderr, flush=True)


def diagnostic_summary():
    path = diagnostic_path()
    if not path.exists():
        return {"attempts_recorded": 0, "events": [], "path": str(path)}
    events, unreadable = [], 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
                else:
                    unreadable += 1
            except (ValueError, TypeError):
                unreadable += 1
        size = path.stat().st_size
    except OSError:
        return {"error": "diagnostic_file_read_failed", "path": str(path)}
    interesting = [e for e in events if e.get("decision") != "success" or e.get("attempt", 1) > 1]
    return {"attempts_recorded": len(events), "file_events_dropped": _LOG_DROPPED,
            "unreadable_records": unreadable, "events": interesting[-32:], "path": str(path),
            "file_size": size,
            "retention": "At most 48 files, 7 days, 256 KiB each; cloud disk is ephemeral."}


class WorkdayHTTP:
    def __init__(self, send, *, source, deadline=None, clock=None, sleep=None, jitter=None, logger=None):
        self.send = send
        self.source = source
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.jitter = jitter or (lambda: random.uniform(0, 1))
        self.logger = logger or emit
        limit = float(os.environ.get("WORKDAY_SOURCE_SECONDS", "175"))
        self.deadline = deadline if deadline is not None else (_SOURCE_DEADLINE.get() or self.clock() + limit)
        self.scope = uuid.uuid4().hex

    def request(self, method, url, **kwargs):
        if not is_workday_read(method, url):
            raise ValueError("Workday policy only accepts known read-only career requests")
        parsed = urlsplit(url)
        host = parsed.hostname.lower()
        with _HOSTS_LOCK:
            state = _HOSTS.setdefault(host, {"lock": threading.Lock(), "next": 0.0, "cooldown": 0.0})
        offset = (kwargs.get("json") or {}).get("offset")
        for attempt in range(1, ATTEMPTS + 1):
            base = {"source": self.source, "scope": self.scope, "host": host,
                    "path": parsed.path, "method": method.upper(), "offset": offset, "attempt": attempt}
            response = None
            retry = False
            reason = ""
            with state["lock"]:
                wait = max(0.0, state["next"] - self.clock(), state["cooldown"] - self.clock())
                if self.clock() + wait + 1 >= self.deadline:
                    self.logger({**base, "decision": "deferred", "wait_seconds": round(wait, 3),
                                 "reason": "host cooldown exceeds remaining source budget"})
                    raise WorkdayDeferred(f"{host}: cooldown exceeds source budget; wait at least {wait:.1f}s")
                if wait:
                    self.sleep(wait)
                remaining = self.deadline - self.clock()
                if remaining <= 1:
                    raise WorkdayDeferred(f"{host}: source budget exhausted")
                options = dict(kwargs)
                # Bound each connect/read timeout by the remaining source budget.
                options["timeout"] = (min(10.0, remaining / 2), min(25.0, remaining / 2))
                options["allow_redirects"] = False
                started = self.clock()
                try:
                    response = self.send(method, url, **options)
                    status = response.status_code
                    retry = status in RETRY_STATUSES
                    reason = f"HTTP {status}"
                    if 200 <= status < 300 and method.upper() != "HEAD":
                        try:
                            response.json()
                        except ValueError:
                            retry, reason = True, "non-JSON response"
                except (requests.Timeout, requests.ConnectionError) as exc:
                    status, retry, reason = None, True, type(exc).__name__
                finally:
                    state["next"] = self.clock() + HOST_INTERVAL_SECONDS
                elapsed = self.clock() - started
                headers = getattr(response, "headers", {}) or {}
                retry_after = retry_after_seconds(headers.get("Retry-After"))
                if status == 429 and retry_after is not None:
                    state["cooldown"] = max(state["cooldown"], self.clock() + retry_after)
                fields = {**base, "status": status, "elapsed_ms": round(elapsed * 1000),
                          "reason": reason, "retry_after_raw": _clean(headers.get("Retry-After", ""), 128),
                          "cf_ray": _clean(headers.get("CF-Ray", ""), 128),
                          "server": _clean(headers.get("Server", ""), 128),
                          "server_date": _clean(headers.get("Date", ""), 128),
                          "cf_mitigated": _clean(headers.get("CF-Mitigated", ""), 80),
                          "content_type": _clean(headers.get("Content-Type", ""), 128)}
                if response is not None and (retry or not 200 <= status < 300):
                    raw = getattr(response, "content", b"")[:65536]
                    fields.update(response_excerpt=_clean(raw.decode("utf-8", "replace")),
                                  response_prefix_sha256=hashlib.sha256(raw).hexdigest())
            challenge = str(headers.get("CF-Mitigated", "")).lower() == "challenge"
            if not challenge and status in (403, 429) and "html" in str(headers.get("Content-Type", "")).lower():
                readable = _clean(getattr(response, "content", b"")[:65536].decode("utf-8", "replace"), 2000).lower()
                challenge = "security check" in readable and "enable javascript and cookies" in readable
            if challenge:
                with state["lock"]:
                    state["cooldown"] = max(state["cooldown"], self.clock() + 86400)
                self.logger({**fields, "decision": "access_challenge", "retryable": False,
                             "retry_not_before": datetime.fromtimestamp(time.time() + 86400, timezone.utc).isoformat()})
                raise WorkdayChallenge(f"{host} offset={offset}: upstream security challenge; "
                                       f"HTTP {status}; CF-Ray={headers.get('CF-Ray', 'unknown')}; no automatic retry")
            if not retry:
                if not 200 <= status < 300:
                    self.logger({**fields, "decision": "terminal"})
                    response.raise_for_status()
                    raise RuntimeError(f"Unexpected Workday HTTP {status}; redirect not followed")
                self.logger({**fields, "decision": "success"})
                return response
            delay = max(BACKOFF_SECONDS[min(attempt - 1, 3)] + self.jitter(), retry_after or 0)
            if status == 429:
                with state["lock"]:
                    state["cooldown"] = max(state["cooldown"], self.clock() + delay)
            if attempt == ATTEMPTS:
                self.logger({**fields, "decision": "exhausted", "wait_seconds": round(delay, 3)})
                raise WorkdayDeferred(f"{host} offset={offset}: {reason}; {attempt} attempts exhausted")
            if self.clock() + delay + 1 >= self.deadline:
                self.logger({**fields, "decision": "deferred", "wait_seconds": round(delay, 3),
                             "retry_not_before": datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()})
                raise WorkdayDeferred(f"{host} offset={offset}: {reason}; requested wait {delay:.1f}s exceeds source budget")
            self.logger({**fields, "decision": "retry", "wait_seconds": round(delay, 3)})
            self.sleep(delay)


# Import last so the policy remains easy to inspect without executing a provider.
import requests
