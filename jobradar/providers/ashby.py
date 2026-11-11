"""Ashby provider — public job-board posting API (no auth).

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""
from __future__ import annotations

import html
import re
from typing import List

from bs4 import BeautifulSoup

from ..models import Job, clean_ws, now_iso
from .base import Provider, http_get

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def _strip(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\n{3,}", "\n\n", BeautifulSoup(html.unescape(raw), "html.parser").get_text("\n", strip=True)).strip()


class AshbyProvider(Provider):
    source = "ashby"

    def fetch(self, run_id: str) -> List[Job]:
        data = http_get(API.format(slug=self.slug)).json()
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("ashby returned an invalid posting list")
        company = clean_ws(data.get("name")) or self.slug.replace("-", " ").title()
        jobs: List[Job] = []
        for p in data.get("jobs", []):
            title = clean_ws(p.get("title"))
            url = p.get("jobUrl") or p.get("applyUrl") or ""
            if not title or not url:
                raise ValueError("Posting missing title or URL; observation is incomplete")
            loc = clean_ws(p.get("location") or p.get("locationName"))
            desc = _strip(p.get("descriptionHtml") or "") or clean_ws(p.get("descriptionPlain"))
            jobs.append(Job(company=company, title=title, url=url, location=loc or None,
                            description=desc, description_raw=p.get("descriptionHtml") or "",
                            description_clean=desc, source=self.source, provider_slug=self.slug,
                            run_id=run_id, remote=bool(p.get("isRemote")) or ("remote" in (loc or "").lower()) or None,
                            scraped_at=now_iso()))
        return jobs
