## Production reliability update — November 10, 2026

Cloudflare Durable Object alarms in `abet-jobs-scheduler` now own dispatch (every 12 hours, at 09:37 and 21:37 UTC)
(:09 UTC robotics, :37 UTC tech). GitHub Actions remains the execution host;
the old GitHub cron is removed to avoid two scheduler owners. The scheduler is
activated only after release verification. Health: https://abet-jobs-scheduler.abetscrape.workers.dev/health

Providers execute in bounded subprocesses and incomplete HTTP/pagination results
raise errors. Cleanup uses request-size-bounded batches and confirmed update counts.
Partial source runs preserve existing active jobs and produce a degraded manifest.
Notifications use an RLS-protected persistent queue; delivery is acknowledged after
Discord returns a message ID. Existing listings are not mass-replayed. Delivery is
at-least-once if acceptance succeeds but the acknowledgement is lost.

Run receipts are uploaded as `job-radar-receipt`, including
`data/runtime/latest_manifest.json`. Inspect provider completeness and delivery
totals, not just the workflow icon. The scheduler source and pause/rollback runbook
live in `misterplusev/job-radar/runtime/cloudflare` and its shared scraper docs.

# Tech Job Radar

Production: employer careers / official ATS sources -> Python ingestion -> Supabase -> searchable board and Discord job alerts.

**Start here:** [shared architecture, operations and new-industry build guide](https://github.com/misterplusev/job-radar/tree/master/docs/job-scrapers). Verified November 10, 2026. Machine inventory and a read-only schedule/health checker are included there.

## Current production

- Repository default branch: `master`; workflow `.github/workflows/pipeline.yml`.
- Schedule: every 12 hours at 09:37 and 21:37 UTC, started by the Cloudflare scheduler (`runtime/cloudflare/worker.mjs` in `misterplusev/job-radar`).
- Entry point: `python run.py ingest`; then `python run.py render --out out/dashboard.html` uploads a workflow artifact.
- Targets: `config/targets.json`; providers in `jobradar/providers/` include Greenhouse, Lever, Ashby, Amazon and Workday.
- Tables: `tech_jobs`, `tech_job_scrape_runs`, `tech_applications`; view `tech_jobs_with_applied` in the shared Supabase project.
- Board: [Tech jobs](https://misterplusev.github.io/jobradar-tech-board/). Its static frontend is a separate board repository; rendering the workflow artifact does not redeploy it.
- Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `DISCORD_WEBHOOK_JOBS`; schema step uses `SUPABASE_ACCESS_TOKEN` and `SUPABASE_PROJECT_REF`.

Tech collection and notification configuration is separate from the robotics module. New jobs are written to a notification table before they are stored (`tech_job_notifications`), and every run sends whatever is still waiting, so a failed send is retried on the next run. See the shared architecture for actual pacing and recovery limitations.

## Working on the code

Read the workflow, current provider source, module schema and applicable local instructions before running commands. `run.py ingest` can write production and send alerts when secrets are present; it is not a read-only inspection. `db/apply_schema.py` applies SQL. Preserve local uncommitted work.

Optional scoring, LLM judging and resume/cover-letter generation code remains in this repository. Those commands are separate from the scheduled ingestion workflow and are not automatically run in production. A generated document or board checkbox is not an application submission.

This repository is public. Keep credentials, personal skills matrices, resumes and private application records out of new commits and public artifacts. Never put service-role keys in a frontend. Preserve employer evidence and immutable discovery dates.
