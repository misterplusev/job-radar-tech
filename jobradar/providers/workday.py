"""Workday cxs API provider (direct career-portal JSON, no auth).

Discovered tenant/site combos (verified 2026-10-09):
  bostondynamics      -> bostondynamics.wd1.myworkdayjobs.com  / Boston_Dynamics
  autostore           -> autostore.wd3.myworkdayjobs.com       / autostore
  rockwellautomation  -> rockwellautomation.wd1.myworkdayjobs.com / External_Rockwell_Automation

The per-job detail endpoint is disabled (HTTP 400) on these instances, so
list-only fields are used: title, location, url (no descriptions).
"""
import requests
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
            r = requests.post(
                f"https://{host}/wday/cxs/{self.slug}/{site}/jobs",
                headers=_HEADERS,
                json={"limit": 20, "offset": offset, "searchText": ""},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data.get("jobPostings"), list) or "total" not in data:
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
