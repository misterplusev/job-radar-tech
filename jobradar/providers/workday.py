"""Workday cxs API provider (direct career-portal JSON, no auth).

Discovered tenant/site combos (verified 2026-10-09):
  bostondynamics      -> bostondynamics.wd1.myworkdayjobs.com  / Boston_Dynamics
  autostore           -> autostore.wd3.myworkdayjobs.com       / autostore
  rockwellautomation  -> rockwellautomation.wd1.myworkdayjobs.com / External_Rockwell_Automation

The per-job detail endpoint is disabled (HTTP 400) on these instances, so
list-only fields are used: title, location, url (no descriptions).
"""
import requests
import sys
import time
from typing import List

from .base import Provider
from ..models import Job, clean_ws

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_BOARDS = {
    "bostondynamics": ("bostondynamics.wd1.myworkdayjobs.com", "Boston_Dynamics"),
    "autostore": ("autostore.wd3.myworkdayjobs.com", "autostore"),
    "rockwellautomation": ("rockwellautomation.wd1.myworkdayjobs.com", "External_Rockwell_Automation"),
}

_BOARD_NAMES = {
    "bostondynamics": "Boston Dynamics",
    "autostore": "AutoStore",
    "rockwellautomation": "Rockwell Automation",
}

_RETRY_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_PAGE_ATTEMPTS = 3


def _page(url, payload):
    """Retry the same read-only CXS search page; never turn an error into [].

    Workday sometimes returns a 521 or a successful HTTP response containing HTML
    instead of JSON. The provider process still has its 180-second hard deadline.
    Only transport failures, selected transient statuses and JSON decoding retry.
    Valid JSON with an invalid schema is a source failure, not an empty board.
    """
    reason = "unknown response"
    for attempt in range(1, _PAGE_ATTEMPTS + 1):
        response = None
        try:
            response = requests.post(url, headers=_HEADERS, json=payload, timeout=(10, 25))
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                reason = "non-JSON response"
        except (requests.Timeout, requests.ConnectionError) as exc:
            reason = type(exc).__name__
        except requests.HTTPError:
            if response is None or response.status_code not in _RETRY_STATUS:
                raise
            reason = f"HTTP {response.status_code}"
        headers = getattr(response, "headers", {}) or {}
        context = (f"status={getattr(response, 'status_code', 'transport')} "
                   f"content_type={headers.get('Content-Type', 'unknown')[:80]} "
                   f"cf_ray={headers.get('CF-Ray', 'none')[:80]}")
        if attempt == _PAGE_ATTEMPTS:
            raise RuntimeError(f"Workday page offset={payload['offset']} failed after "
                               f"{attempt} attempts: {reason}; {context}")
        delay = float(2 ** (attempt - 1))
        try:
            delay = max(delay, float(headers.get('Retry-After', 0)))
        except (TypeError, ValueError):
            pass
        if delay > 30:
            raise RuntimeError("Workday Retry-After exceeds the bounded page retry budget")
        print(f"[workday retry] offset={payload['offset']} attempt={attempt} "
              f"reason={reason} {context} wait={delay}s", file=sys.stderr, flush=True)
        time.sleep(delay)


class WorkdayProvider(Provider):
    source = "workday"

    def __init__(self, slug: str):
        super().__init__(slug)

    def fetch(self, run_id: str) -> List[Job]:
        board = _BOARDS.get(self.slug)
        if not board:
            raise ValueError("Unknown Workday board")
        host, site = board
        company = _BOARD_NAMES.get(self.slug, self.slug.replace("-", " ").title())
        jobs: List[Job] = []
        seen = set()
        offset = 0
        expected_total = None
        for _page in range(50):
            data = _page(
                f"https://{host}/wday/cxs/{self.slug}/{site}/jobs",
                {"limit": 20, "offset": offset, "searchText": ""},
            )
            if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list) or "total" not in data:
                raise ValueError("Workday returned an invalid result page")
            # Workday commonly returns total=0 on every page after page one.
            # Retain the initial advertised total instead of ending at page two.
            if expected_total is None:
                expected_total = int(data["total"])
            else:
                expected_total = max(expected_total, int(data["total"]))
            fresh = [jp for jp in (data.get("jobPostings") or [])
                     if jp.get("externalPath") not in seen]
            if not fresh:
                if offset < expected_total:
                    raise ValueError("Workday pagination ended before the advertised total")
                break
            for jp in fresh:
                title = clean_ws(jp.get("title"))
                external = (jp.get("externalPath") or "").strip()
                if not title or not external:
                    raise ValueError("Workday posting missing title or URL")
                seen.add(external)
                url = external if external.startswith("http") else f"https://{host}/{site}{external}"
                jobs.append(Job(
                    company=company,
                    title=title,
                    url=url,
                    location=clean_ws(jp.get("locationsText")) or None,
                    source=self.source,
                    provider_slug=self.slug,
                    run_id=run_id,
                    is_active=True,
                ))
            offset += len(data["jobPostings"])
            if offset >= expected_total:
                break
        else:
            raise RuntimeError("Workday pagination safety limit reached; incomplete observation")
        return jobs
