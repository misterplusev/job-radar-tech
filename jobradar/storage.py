"""Storage abstraction: SQLite for local dev, Supabase (PostgREST) for prod.

Selection: if SUPABASE_URL is set -> SupabaseBackend, else SQLiteBackend.
Both expose the same interface so the rest of the app is storage-agnostic.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

import requests

from .models import Job, JOB_COLUMNS, now_iso

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122 Safari/537.36"

# The table the deployed module schema actually creates (see db/schema_tech.sql).
# The legacy `jobs`/`job_scores` tables below belong to the v0.1 scorer design; they
# do NOT exist in the module schema, so any Supabase query against them returns a
# PostgREST error object instead of rows.
MODULE_JOBS_TABLE = "tech_jobs"


def get_storage(settings) -> "Storage":
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
        return SupabaseBackend(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return SQLiteBackend(settings.SQLITE_PATH)


class Storage:
    def init_schema(self) -> None: ...
    def upsert_jobs(self, jobs: List[Job]) -> int: ...
    def log_run(self, run: dict) -> None: ...
    def fetch_unscored(self, model: str, limit: int = 300) -> List[dict]: ...
    def save_score(self, job_id: int, score: dict) -> None: ...
    def fetch_alertable(self, min_score: int, model: str) -> List[dict]: ...
    def mark_alerted(self, score_ids: List[int]) -> None: ...
    def get_job(self, job_id: int) -> Optional[dict]: ...
    def upsert_application(self, app: dict) -> None: ...
    def recent_scored(self, limit: int = 50) -> List[dict]: ...
    def recent_active_jobs(self, limit: int = 500) -> List[dict]: ...


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
class SQLiteBackend(Storage):
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init_schema(self) -> None:
        with self._conn() as c:
            c.executescript("""
            create table if not exists jobs (
              id integer primary key autoincrement,
              company text not null, title text not null, url text not null unique,
              location text, description text, description_raw text, description_clean text,
              source text, provider_slug text, run_id text, salary text,
              remote integer, posted_at text, is_active integer not null default 1,
              scraped_at text not null
            );
            create table if not exists job_scores (
              id integer primary key autoincrement,
              job_id integer not null references jobs(id) on delete cascade,
              fit_score integer not null, archetype text, matched_strengths text,
              gaps text, lead_with text, rationale text, model text not null,
              scored_at text not null, alerted integer not null default 0,
              unique(job_id, model)
            );
            create table if not exists applications (
              id integer primary key autoincrement,
              company text not null, job_url text not null, job_title text,
              status text not null default 'draft', fit_score integer,
              resume_path text, cover_path text, applied_at text,
              updated_at text not null, notes text, unique(company, job_url)
            );
            create table if not exists scrape_runs (
              id integer primary key autoincrement, run_id text not null, source text,
              provider_slug text, status text not null, job_count integer not null default 0,
              error_summary text, started_at text, finished_at text not null
            );
            """)

    def upsert_jobs(self, jobs: List[Job]) -> int:
        n = 0
        with self._conn() as c:
            for j in jobs:
                row = {k: j.to_row().get(k) for k in JOB_COLUMNS}
                row["remote"] = 1 if row.get("remote") else (0 if row.get("remote") is not None else None)
                cols = ",".join(JOB_COLUMNS)
                ph = ",".join("?" for _ in JOB_COLUMNS)
                updates = ",".join(f"{k}=excluded.{k}" for k in JOB_COLUMNS if k != "url")
                c.execute(
                    f"insert into jobs ({cols}) values ({ph}) "
                    f"on conflict(url) do update set {updates}",
                    [row[k] for k in JOB_COLUMNS],
                )
                n += 1
        return n

    def log_run(self, run: dict) -> None:
        with self._conn() as c:
            c.execute(
                "insert into scrape_runs (run_id,source,provider_slug,status,job_count,error_summary,started_at,finished_at)"
                " values (?,?,?,?,?,?,?,?)",
                [run.get("run_id"), run.get("source"), run.get("provider_slug"), run.get("status"),
                 run.get("job_count", 0), run.get("error_summary"), run.get("started_at"),
                 run.get("finished_at", now_iso())],
            )

    def fetch_unscored(self, model: str, limit: int = 300) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "select j.* from jobs j left join job_scores s on s.job_id=j.id and s.model=? "
                "where s.id is null and j.is_active=1 order by j.scraped_at desc limit ?",
                [model, limit],
            ).fetchall()
            return [dict(r) for r in rows]

    def save_score(self, job_id: int, score: dict) -> None:
        with self._conn() as c:
            c.execute(
                "insert into job_scores (job_id,fit_score,archetype,matched_strengths,gaps,lead_with,rationale,model,scored_at,alerted)"
                " values (?,?,?,?,?,?,?,?,?,0) on conflict(job_id,model) do update set "
                "fit_score=excluded.fit_score,archetype=excluded.archetype,matched_strengths=excluded.matched_strengths,"
                "gaps=excluded.gaps,lead_with=excluded.lead_with,rationale=excluded.rationale,scored_at=excluded.scored_at",
                [job_id, score["fit_score"], score.get("archetype"),
                 json.dumps(score.get("matched_strengths")), json.dumps(score.get("gaps")),
                 score.get("lead_with"), score.get("rationale"), score["model"], now_iso()],
            )

    def fetch_alertable(self, min_score: int, model: str) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "select s.id as score_id, s.fit_score, s.archetype, s.rationale, s.lead_with, "
                "j.* from job_scores s join jobs j on j.id=s.job_id "
                "where s.model=? and s.fit_score>=? and s.alerted=0 order by s.fit_score desc",
                [model, min_score],
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_alerted(self, score_ids: List[int]) -> None:
        if not score_ids:
            return
        with self._conn() as c:
            c.executemany("update job_scores set alerted=1 where id=?", [(i,) for i in score_ids])

    def get_job(self, job_id: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("select * from jobs where id=?", [job_id]).fetchone()
            return dict(r) if r else None

    def upsert_application(self, app: dict) -> None:
        with self._conn() as c:
            c.execute(
                "insert into applications (company,job_url,job_title,status,fit_score,resume_path,cover_path,updated_at,notes)"
                " values (?,?,?,?,?,?,?,?,?) on conflict(company,job_url) do update set "
                "status=excluded.status,fit_score=excluded.fit_score,resume_path=excluded.resume_path,"
                "cover_path=excluded.cover_path,updated_at=excluded.updated_at,notes=excluded.notes",
                [app["company"], app["job_url"], app.get("job_title"), app.get("status", "draft"),
                 app.get("fit_score"), app.get("resume_path"), app.get("cover_path"), now_iso(), app.get("notes")],
            )

    def recent_scored(self, limit: int = 50) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "select s.fit_score,s.archetype,s.rationale,j.company,j.title,j.location,j.url,j.source "
                "from job_scores s join jobs j on j.id=s.job_id order by s.fit_score desc limit ?",
                [limit],
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_active_jobs(self, limit: int = 500) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "select company,title,location,url,source,posted_at,scraped_at,is_active "
                "from jobs where is_active=1 order by scraped_at desc limit ?",
                [limit],
            ).fetchall()
            return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Supabase (PostgREST)
# --------------------------------------------------------------------------- #
class SupabaseBackend(Storage):
    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip("/")
        self.key = service_key

    def _h(self, extra: dict | None = None) -> dict:
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
             "Content-Type": "application/json", "User-Agent": UA}
        if extra:
            h.update(extra)
        return h

    def init_schema(self) -> None:
        # Schema is applied via db/schema.sql in the Supabase SQL editor (PostgREST can't DDL).
        pass

    def upsert_jobs(self, jobs: List[Job]) -> int:
        if not jobs:
            return 0
        rows = [{k: j.to_row().get(k) for k in JOB_COLUMNS} for j in jobs]
        r = requests.post(f"{self.url}/rest/v1/jobs?on_conflict=url",
                          headers=self._h({"Prefer": "resolution=merge-duplicates"}),
                          data=json.dumps(rows), timeout=120)
        r.raise_for_status()
        return len(rows)

    def log_run(self, run: dict) -> None:
        run = {**run, "finished_at": run.get("finished_at", now_iso())}
        requests.post(f"{self.url}/rest/v1/scrape_runs", headers=self._h(),
                      data=json.dumps([run]), timeout=60)

    def fetch_unscored(self, model: str, limit: int = 300) -> List[dict]:
        jobs = requests.get(
            f"{self.url}/rest/v1/jobs?select=*&is_active=eq.true&order=scraped_at.desc&limit={limit}",
            headers=self._h(), timeout=60).json()
        scored = requests.get(
            f"{self.url}/rest/v1/job_scores?select=job_id&model=eq.{model}",
            headers=self._h(), timeout=60).json()
        done = {s["job_id"] for s in scored}
        return [j for j in jobs if j["id"] not in done]

    def save_score(self, job_id: int, score: dict) -> None:
        row = {"job_id": job_id, "fit_score": score["fit_score"], "archetype": score.get("archetype"),
               "matched_strengths": score.get("matched_strengths"), "gaps": score.get("gaps"),
               "lead_with": score.get("lead_with"), "rationale": score.get("rationale"),
               "model": score["model"]}
        requests.post(f"{self.url}/rest/v1/job_scores?on_conflict=job_id,model",
                      headers=self._h({"Prefer": "resolution=merge-duplicates"}),
                      data=json.dumps([row]), timeout=60)

    def fetch_alertable(self, min_score: int, model: str) -> List[dict]:
        scores = requests.get(
            f"{self.url}/rest/v1/job_scores?select=id,job_id,fit_score,archetype,rationale,lead_with"
            f"&model=eq.{model}&fit_score=gte.{min_score}&alerted=eq.false&order=fit_score.desc",
            headers=self._h(), timeout=60).json()
        out = []
        for s in scores:
            job = self.get_job(s["job_id"])
            if job:
                out.append({**job, **s, "score_id": s["id"]})
        return out

    def mark_alerted(self, score_ids: List[int]) -> None:
        for sid in score_ids:
            requests.patch(f"{self.url}/rest/v1/job_scores?id=eq.{sid}", headers=self._h(),
                           data=json.dumps({"alerted": True}), timeout=60)

    def get_job(self, job_id: int) -> Optional[dict]:
        r = requests.get(f"{self.url}/rest/v1/jobs?id=eq.{job_id}&limit=1", headers=self._h(), timeout=60).json()
        return r[0] if r else None

    def upsert_application(self, app: dict) -> None:
        app = {**app, "updated_at": now_iso()}
        requests.post(f"{self.url}/rest/v1/applications?on_conflict=company,job_url",
                      headers=self._h({"Prefer": "resolution=merge-duplicates"}),
                      data=json.dumps([app]), timeout=60)

    def recent_scored(self, limit: int = 50) -> List[dict]:
        # Legacy v0.1 scorer path. `job_scores` does not exist in the deployed
        # module schema, so this raises instead of handing back a PostgREST
        # error dict that downstream code would iterate as if it were rows.
        r = requests.get(
            f"{self.url}/rest/v1/job_scores?select=fit_score,archetype,rationale,jobs(company,title,location,url,source)"
            f"&order=fit_score.desc&limit={limit}", headers=self._h(), timeout=60)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"recent_scored expected rows, got {type(rows).__name__}: {rows}")
        return rows

    def recent_active_jobs(self, limit: int = 500) -> List[dict]:
        r = requests.get(
            f"{self.url}/rest/v1/{MODULE_JOBS_TABLE}"
            f"?select=company,title,location,url,ats,posted_at,scraped_at,is_active"
            f"&is_active=eq.true&order=scraped_at.desc&limit={limit}",
            headers=self._h(), timeout=60)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"recent_active_jobs expected rows, got {type(rows).__name__}: {rows}")
        for row in rows:
            row["source"] = row.pop("ats", None)
        return rows
