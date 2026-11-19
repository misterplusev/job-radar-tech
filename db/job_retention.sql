-- Retention for the four job scrapers. Applied 2026-11-19 by tools\apply_retention.py.
--
-- What it removes, and nothing else:
--   1. The description text of a job that is closed AND was last seen more than p_closed_days ago (default 60).
--      The row stays: company, title, link, location, dates. A job with an application on file is never touched.
--      The saved change marker is cleared with the text, so if the job is ever posted again the scraper
--      writes the full row, text included.
--   2. Run-log rows older than p_log_days (default 90).
-- Windows under 30 days are refused. p_source limits the pass to one scraper; null means all four.
-- Each scraper's last workflow step calls log_job_system('<scraper>'), which calls this for that scraper
-- and records what was removed in job_system_log.detail->'retention'.
-- Switch it off: update public.job_system_settings set value = 'off' where key = 'retention';

create table if not exists public.job_system_settings (
  key text primary key,
  value text not null,
  changed_at timestamptz not null default now()
);
alter table public.job_system_settings enable row level security;
revoke all on table public.job_system_settings from anon, authenticated;
insert into public.job_system_settings (key, value) values ('retention', 'on') on conflict (key) do nothing;

create or replace function public.apply_job_retention(p_source text default null, p_closed_days integer default 60, p_log_days integer default 90)
returns jsonb language plpgsql security definer set search_path = public, pg_catalog as $fn$
declare
  cut timestamptz := now() - make_interval(days => p_closed_days);
  logcut timestamptz := now() - make_interval(days => p_log_days);
  n integer;
  r jsonb := jsonb_build_object('closed_days', p_closed_days, 'log_days', p_log_days);
begin
  if p_closed_days < 30 or p_log_days < 30 then
    raise exception 'retention windows under 30 days are refused';
  end if;
  if coalesce((select value from public.job_system_settings where key = 'retention'), 'on') <> 'on' then
    return r || jsonb_build_object('skipped', 'retention is switched off');
  end if;

  if p_source is null or p_source = 'robotics' then
    update public.robotics_jobs j set description = null, source_payload = coalesce(j.source_payload, '{}'::jsonb) - 'content_hash_v2'
     where not j.is_active and j.last_seen_at < cut and j.description is not null and j.description <> ''
       and not exists (select 1 from public.robotics_applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('robotics_jobs_text_removed', n);
    delete from public.robotics_job_scrape_runs where created_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('robotics_job_scrape_runs_deleted', n);
  end if;

  if p_source is null or p_source = 'tech' then
    update public.tech_jobs j set description = null, source_payload = coalesce(j.source_payload, '{}'::jsonb) - 'content_hash_v2'
     where not j.is_active and j.last_seen_at < cut and j.description is not null and j.description <> ''
       and not exists (select 1 from public.tech_applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('tech_jobs_text_removed', n);
    delete from public.tech_job_scrape_runs where created_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('tech_job_scrape_runs_deleted', n);
  end if;

  if p_source is null or p_source = 'sports_teams' then
    update public.team_jobs j set description = null, detail_status = null, detail_checked_at = null
     where not j.is_active and j.last_seen_at < cut and j.description is not null and j.description <> ''
       and not exists (select 1 from public.team_applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('team_jobs_text_removed', n);
    delete from public.team_job_scrape_runs where created_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('team_job_scrape_runs_deleted', n);
  end if;

  if p_source is null or p_source = 'sportsbook' then
    update public.jobs j set description = '', description_raw = null, description_clean = null, source_payload = '{}'::jsonb
     where not j.is_active and j.last_seen_at < cut
       and (coalesce(j.description, '') <> '' or coalesce(j.description_raw, '') <> '' or coalesce(j.description_clean, '') <> '')
       and not exists (select 1 from public.applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('jobs_text_removed', n);
    delete from public.job_scrape_runs where started_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('job_scrape_runs_deleted', n);
  end if;
  return r;
end
$fn$;
revoke all on function public.apply_job_retention(text, integer, integer) from public, anon, authenticated;
grant execute on function public.apply_job_retention(text, integer, integer) to service_role;
