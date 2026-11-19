"""Preserve verified jobs when a board includes malformed records."""
from dataclasses import replace
from urllib.parse import urlsplit

import requests


class PartialBoard(ValueError):
    def __init__(self, jobs, untitled, rejected):
        self.jobs, self.untitled, self.rejected = jobs, untitled, rejected
        super().__init__(f"{len(jobs)} valid listings, {len(untitled)} untitled, {rejected} invalid identities")


def resolve_ashby_titles(partial, url_base, key):
    """An exact, same-source historical title can fill a missing display field for a live URL."""
    from . import publish
    jobs = list(partial.jobs)
    retained = []
    unresolved = partial.rejected + len(partial.untitled)
    for job in partial.untitled[:10]:
        p = urlsplit(job.url)
        if (job.source != "ashby" or p.scheme != "https" or p.hostname != "jobs.ashbyhq.com"
                or not p.path.startswith("/" + job.provider_slug + "/")):
            continue
        params = {"select":"title","company":"eq."+job.company,"url":"eq."+job.url,
                  "ats":"eq.ashby","source_payload->>provider_slug":"eq."+job.provider_slug,"limit":2}
        try:
            response = requests.get(publish._rest(url_base, publish.TABLE_JOBS),
                                    headers=publish._headers(key), params=params, timeout=8)
            response.raise_for_status()
            rows = response.json()
        except (requests.RequestException, ValueError):
            continue
        if len(rows) == 1 and isinstance(rows[0].get("title"), str) and rows[0]["title"].strip():
            jobs.append(replace(job, title=rows[0]["title"]))
            retained.append(job.url)
            unresolved -= 1
    return jobs, retained, unresolved
