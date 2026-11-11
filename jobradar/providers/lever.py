"""Lever provider — public postings API (no auth).

Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
"""
from __future__ import annotations

from typing import List

from ..models import Job, clean_ws, now_iso
from .base import Provider, http_get

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


class LeverProvider(Provider):
    source = "lever"

    def fetch(self, run_id: str) -> List[Job]:
        data = http_get(API.format(slug=self.slug)).json()
        if not isinstance(data, list):
            raise ValueError("Lever returned an invalid posting list")
        company = self.slug.replace("-", " ").title()
        jobs: List[Job] = []
        for p in data:
            title = clean_ws(p.get("text"))
            url = p.get("hostedUrl") or p.get("applyUrl") or ""
            if not title or not url:
                raise ValueError("Posting missing title or URL; observation is incomplete")
            loc = clean_ws((p.get("categories") or {}).get("location"))
            desc = clean_ws(p.get("descriptionPlain") or p.get("description"))
            jobs.append(Job(company=company, title=title, url=url, location=loc or None,
                            description=desc, description_raw=p.get("description") or "",
                            description_clean=desc, source=self.source, provider_slug=self.slug,
                            run_id=run_id, remote=("remote" in (loc or "").lower()) or None,
                            scraped_at=now_iso()))
        return jobs
