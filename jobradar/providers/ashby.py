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
from ..observations import PartialBoard
from .names import board_company

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def _strip(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\n{3,}", "\n\n", BeautifulSoup(html.unescape(raw), "html.parser").get_text("\n", strip=True)).strip()


class AshbyProvider(Provider):
    source = "ashby"

    def __init__(self, slug: str, content: bool = True):
        super().__init__(slug)
        self.content = content

    def fetch(self, run_id: str) -> List[Job]:
        data = http_get(API.format(slug=self.slug)).json()
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("ashby returned an invalid posting list")
        company = board_company(self.slug, data.get("name"))
        jobs: List[Job] = []
        untitled, rejected, seen = [], 0, set()
        for p in data["jobs"]:
            if not isinstance(p, dict):
                rejected += 1
                continue
            title = clean_ws(p.get("title"))
            url = p.get("jobUrl") or p.get("applyUrl") or ""
            if not url or url in seen:
                rejected += 1
                continue
            seen.add(url)
            loc = clean_ws(p.get("location") or p.get("locationName"))
            if self.content:
                desc = _strip(p.get("descriptionHtml") or "") or clean_ws(p.get("descriptionPlain"))
                raw = p.get("descriptionHtml") or ""
            else:
                desc, raw = "", ""
            group = jobs if title else untitled
            group.append(Job(company=company, title=title, url=url, location=loc or None,
                            description=desc, description_raw=raw,
                            description_clean=desc, source=self.source, provider_slug=self.slug,
                            run_id=run_id, remote=bool(p.get("isRemote")) or ("remote" in (loc or "").lower()) or None,
                            # Ashby's own published time. Without it a new job is never sent to Discord.
                            posted_at=p.get("publishedAt") or None,
                            scraped_at=now_iso()))
        if untitled or rejected:
            raise PartialBoard(jobs, untitled, rejected)
        return jobs
