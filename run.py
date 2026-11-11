#!/usr/bin/env python
"""Job Radar orchestrator CLI.

Commands:
  python run.py ingest     # scrape all providers -> storage
  python run.py score      # score unscored jobs against the matrix
  python run.py alert      # push strong matches to Discord
  python run.py generate --job-id N   # draft resume + cover for a job
  python run.py report     # print top matches
  python run.py pipeline   # ingest -> score -> alert
"""
from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings  # noqa: E402
from jobradar.models import now_iso  # noqa: E402
from jobradar.providers import build_providers  # noqa: E402
from jobradar.storage import get_storage  # noqa: E402

PROVIDER_TIMEOUT = 120


def _run_id(slug: str) -> str:
    return f"{slug}-{int(time.time())}"


def _scrape_all() -> list:
    """Scrape every provider with a per-provider hard timeout ( tech module)."""
    import concurrent.futures

    providers = build_providers(settings.load_targets())
    all_jobs: list = []
    for p in providers:
        rid = _run_id(f"{p.source}:{p.provider_slug}")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(p.fetch, rid)
                jobs = future.result(timeout=PROVIDER_TIMEOUT)
            print(f"  [{p.source}:{p.provider_slug}] {len(jobs)} jobs")
            all_jobs.extend(jobs)
        except concurrent.futures.TimeoutError:
            print(f"  [{p.source}:{p.provider_slug}] TIMEOUT after {PROVIDER_TIMEOUT}s")
        except Exception as e:  # noqa: BLE001
            print(f"  [{p.source}:{p.provider_slug}] ERROR: {e}")
    return all_jobs


def _ingest_supabase() -> int:
    """Fetch complete source observations; persist truthful health and delivery receipts."""
    import json
    import os
    from jobradar import publish
    from jobradar.fetch_worker import fetch_provider

    manifest = {"schema": "abet.jobs.scrape-manifest.v2", "module": "tech",
                "started_at": now_iso(), "status": "running", "providers": [],
                "trigger": os.environ.get("JOBRADAR_TRIGGER", os.environ.get("GITHUB_EVENT_NAME", "manual")),
                "scheduler_slot": os.environ.get("JOBRADAR_SCHEDULER_SLOT", ""),
                "github_run_id": os.environ.get("GITHUB_RUN_ID", "")}
    manifest_path = Path("data/runtime/latest_manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    def save():
        temp = manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temp.replace(manifest_path)
    save()
    try:
        url_base, key = publish.supabase_env()
        providers = build_providers(settings.load_targets())
        if not providers:
            raise RuntimeError("No providers configured")
        manifest["expected_providers"] = len(providers)
        all_jobs, failures, empty_providers = [], [], []
        for provider in providers:
            label = f"{provider.source}:{provider.provider_slug}"
            started = time.monotonic()
            receipt = {"provider": label}
            try:
                jobs = fetch_provider(provider, _run_id(label),
                                      timeout=300 if provider.source == "amazon" else 180)
                all_jobs.extend(jobs)
                if not jobs:
                    empty_providers.append((provider.source, provider.provider_slug))
                receipt.update(status="success", job_count=len(jobs), complete=True)
                print(f"  [{label}] {len(jobs)} jobs", flush=True)
            except Exception as exc:
                failures.append(label)
                receipt.update(status="error", job_count=0, complete=False, error=str(exc)[:1200])
                print(f"  [{label}] ERROR: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
            receipt["seconds"] = round(time.monotonic() - started, 2)
            manifest["providers"].append(receipt)
            try:
                publish.record_run(url_base, key, label, receipt["status"], receipt["job_count"],
                                   receipt.get("error", ""), trigger=manifest["trigger"])
            except Exception as exc:
                failures.append(f"telemetry:{label}")
                receipt["telemetry_error"] = type(exc).__name__
            save()
        manifest["collected_jobs"] = len(all_jobs)
        if not all_jobs and failures:
            raise RuntimeError("No jobs collected and at least one provider failed")
        manifest["publish"] = publish.publish(all_jobs, allow_deactivate=not failures,
                                             empty_providers=empty_providers)
        if failures:
            manifest["status"] = "degraded"
            raise RuntimeError(f"Partial scrape: {len(failures)} failures: " + ", ".join(failures))
        manifest["status"] = "healthy"
        return 0
    except Exception as exc:
        if manifest["status"] != "degraded":
            manifest["status"] = "failed"
        manifest["error"] = str(exc)[:2000]
        raise
    finally:
        manifest["finished_at"] = now_iso()
        save()


def _ingest_sqlite() -> int:
    """Dev path: legacy per-provider SQLite upsert (no Supabase env configured)."""
    store = get_storage(settings)
    providers = build_providers(settings.load_targets())
    total = 0
    print(f"[ingest] {len(providers)} providers | storage={type(store).__name__}")
    for p in providers:
        rid = _run_id(f"{p.source}:{p.provider_slug}")
        started = now_iso()
        try:
            jobs = p.fetch(rid)
            n = store.upsert_jobs(jobs)
            total += len(jobs)
            store.log_run({"run_id": rid, "source": p.source, "provider_slug": p.provider_slug,
                           "status": "success", "job_count": len(jobs), "started_at": started})
            print(f"  [{p.source}:{p.provider_slug}] {len(jobs)} jobs")
        except Exception as e:  # noqa: BLE001
            store.log_run({"run_id": rid, "source": p.source, "provider_slug": p.provider_slug,
                           "status": "error", "error_summary": str(e), "started_at": started})
            print(f"  [{p.source}:{p.provider_slug}] ERROR: {e}")
    print(f"[ingest] done — {total} jobs seen")
    return 0


def cmd_ingest(args) -> int:
    import concurrent.futures  # noqa: F401

    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
        try:
            return _ingest_supabase()
        except Exception as e:  # noqa: BLE001
            from jobradar.publish import notify_failure
            print(f"[FATAL] {e}")
            notify_failure(str(e))
            return 1
    return _ingest_sqlite()


def cmd_score(args) -> int:
    from jobradar.matcher.score import score_unscored
    n = score_unscored(settings, get_storage(settings))
    print(f"[score] scored {n} jobs")
    return 0


def cmd_alert(args) -> int:
    from jobradar.alerts.discord import send_alerts
    n = send_alerts(settings, get_storage(settings))
    print(f"[alert] sent {n} matches to Discord")
    return 0


def cmd_generate(args) -> int:
    from jobradar.generate.resume import generate_for_job
    paths = generate_for_job(settings, get_storage(settings), args.job_id)
    print(f"[generate] {paths}")
    return 0


def cmd_report(args) -> int:
    store = get_storage(settings)
    rows = store.recent_scored(args.limit)
    if not rows:
        print("no scored jobs yet — run: python run.py score")
        return 0
    for r in rows:
        job = r if "company" in r else (r.get("jobs") or {})
        print(f"  {r.get('fit_score'):>3}  {job.get('company','?'):22.22}  {job.get('title','?'):48.48}  {job.get('location') or ''}")
    return 0


def cmd_render(args) -> int:
    # Never swallow this. The render step used to catch every exception, print
    # "[render] skipped" and exit 0, so a broken query looked like a green run
    # while uploading no artifact — silently broken on every execution.
    from jobradar.dashboard import render_dashboard
    path = render_dashboard(settings, get_storage(settings), args.out)
    print(f"[render] dashboard -> {path}")
    return 0


def cmd_pipeline(args) -> int:
    cmd_ingest(args)
    cmd_score(args)
    cmd_alert(args)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="jobradar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    sub.add_parser("score")
    sub.add_parser("alert")
    g = sub.add_parser("generate"); g.add_argument("--job-id", type=int, required=True)
    r = sub.add_parser("report"); r.add_argument("--limit", type=int, default=25)
    rn = sub.add_parser("render"); rn.add_argument("--out", default="out/dashboard.html")
    sub.add_parser("pipeline")
    args = ap.parse_args()
    return {"ingest": cmd_ingest, "score": cmd_score, "alert": cmd_alert,
            "generate": cmd_generate, "report": cmd_report, "render": cmd_render,
            "pipeline": cmd_pipeline}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

