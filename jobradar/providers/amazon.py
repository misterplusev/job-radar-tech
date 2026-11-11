"""Amazon.jobs provider - public JSON search API, no auth.

Covers Amazon Robotics + Autonomy (FSD-adjacent) postings. Two queries merged:
'robotics' and 'autonomy'. Deduped by job id.
"""
from __future__ import annotations

import time
from typing import List

from ..models import Job, clean_ws, now_iso
from .base import Provider, http_get

API = "https://www.amazon.jobs/en/search.json?result_limit=100&offset={offset}&base_query={query}"
QUERIES = ["robotics", "autonomy"]
DETAIL_JSON = "https://www.amazon.jobs/en/jobs/{job_id}"


class AmazonProvider(Provider):
    source = "amazon"

    def fetch(self, run_id: str) -> List[Job]:
        jobs: List[Job] = []
        seen = set()
        for query in [self.slug]:
            offset = 0
            for _page in range(100):
                data = http_get(API.format(offset=offset, query=query.replace(" ", "%20"))).json()
                if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or "hits" not in data:
                    raise ValueError("Amazon returned an invalid result page")
                batch = data.get("jobs") or []
                if not batch:
                    if offset < int(data["hits"]):
                        raise ValueError("Amazon pagination ended before the advertised total")
                    break
                for j in batch:
                    jid = str(j.get("job_id") or j.get("id_icims") or "")
                    path = j.get("job_path") or ""
                    url = f"https://www.amazon.jobs{path}" if path.startswith("/") else (path or "")
                    key = jid or url
                    if not key or key in seen or not url:
                        continue
                    seen.add(key)
                    title = clean_ws(j.get("title") or "")
                    if not title:
                        continue
                    loc = clean_ws(j.get("normalized_location") or j.get("location") or "")
                    desc = clean_ws(j.get("description") or "")[:3000]
                    posted = j.get("posted_date")
                    jobs.append(Job(
                        company="Amazon",
                        title=title,
                        url=url,
                        location=loc or None,
                        description=desc,
                        description_raw=desc,
                        description_clean=desc,
                        source=self.source,
                        provider_slug=self.slug,
                        run_id=run_id,
                        remote=("remote" in (loc or "").lower()) or None,
                        posted_at=posted,
                        scraped_at=now_iso(),
                    ))
                total = int(data.get("hits") or 0)
                offset += 100
                if offset >= total:
                    break
                time.sleep(0.4)
            else:
                raise RuntimeError("Amazon pagination safety limit reached; incomplete observation")
        return jobs
