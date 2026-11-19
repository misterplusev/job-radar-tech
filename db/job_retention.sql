-- Retention for the four job scrapers. Applied 2026-11-19 by tools\apply_retention.py.
--
-- What it removes, and nothing else:
--   1. The description text of a job that is closed AND was last seen more than p_closed_days ago (default 60).
--      The row stays: company, title, link, location, dates. A job with an application on file is never touched.
--      The saved change marker is cleared with the text, so if the job is ever posted again the scraper
--      writes the full row, text included.
--   2. Run-log rows older than p_log_days (default 90).
-- What it closes (2026-11-19): a robotics or tech job marked active that no run has seen for 14 days.
-- Nothing is removed from such a row; it is marked closed.
-- What it deletes when, and only when, the setting delete_closed_after_days exists (added 2026-11-19, the operator:
-- "once we have identified a job is no longer active, we probably only need to retain it for like 30 days"):
--   3. The whole row of a job that has been closed for more than that many days, counted from the time it was
--      marked closed (closed_at). A job with an application on file is never deleted. For sports teams the
--      sightings of a deleted job are deleted with it. The Discord delivery records are kept, so a job that is
--      posted again under the same link is not announced a second time.
--      Without that setting nothing is deleted. A value under 30 is refused.
--      Switch it on:  insert into public.job_system_settings (key, value) values ('delete_closed_after_days', '30')
--                     on conflict (key) do update set value = excluded.value, changed_at = now();
--      Switch it off: delete from public.job_system_settings where key = 'delete_closed_after_days';
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
  delete_days integer;
  delcut timestamptz;
begin
  if p_closed_days < 30 or p_log_days < 30 then
    raise exception 'retention windows under 30 days are refused';
  end if;
  if coalesce((select value from public.job_system_settings where key = 'retention'), 'on') <> 'on' then
    return r || jsonb_build_object('skipped', 'retention is switched off');
  end if;
  -- Deleting closed jobs is off unless the setting exists and holds a whole number of days.
  select value::integer into delete_days from public.job_system_settings
   where key = 'delete_closed_after_days' and value ~ '^[0-9]{1,4}$';
  if delete_days is not null then
    if delete_days < 30 then
      raise exception 'retention windows under 30 days are refused';
    end if;
    delcut := now() - make_interval(days => delete_days);
    r := r || jsonb_build_object('delete_closed_after_days', delete_days);
  end if;

  if p_source is null or p_source = 'robotics' then
    -- Added 2026-11-19. A job still marked active that no run has seen for 14 days is closed. Every run
    -- stamps every job it finds, so 14 days unseen means no source reports it any more. On 2026-11-19,
    -- 1,068 robotics jobs stored under company spellings retired on 2026-10-17 ("Openai", "Figureai",
    -- "Physicalintelligence", "Standardbots", "Figure") were still marked active. If a source comes back,
    -- its next run marks the job active again.
    update public.robotics_jobs set is_active = false, closed_at = now()
     where is_active and last_seen_at < now() - interval '14 days';
    get diagnostics n = row_count; r := r || jsonb_build_object('robotics_jobs_closed_unseen', n);
    update public.robotics_jobs j set description = null, source_payload = coalesce(j.source_payload, '{}'::jsonb) - 'content_hash_v2'
     where not j.is_active and j.last_seen_at < cut and j.description is not null and j.description <> ''
       and not exists (select 1 from public.robotics_applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('robotics_jobs_text_removed', n);
    if delcut is not null then
      delete from public.robotics_jobs j where not j.is_active and j.closed_at < delcut
         and not exists (select 1 from public.robotics_applications a where a.company = j.company and a.job_url = j.url);
      get diagnostics n = row_count; r := r || jsonb_build_object('robotics_jobs_closed_deleted', n);
    end if;
    delete from public.robotics_job_scrape_runs where created_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('robotics_job_scrape_runs_deleted', n);
  end if;

  if p_source is null or p_source = 'tech' then
    update public.tech_jobs set is_active = false, closed_at = now()
     where is_active and last_seen_at < now() - interval '14 days';
    get diagnostics n = row_count; r := r || jsonb_build_object('tech_jobs_closed_unseen', n);
    update public.tech_jobs j set description = null, source_payload = coalesce(j.source_payload, '{}'::jsonb) - 'content_hash_v2'
     where not j.is_active and j.last_seen_at < cut and j.description is not null and j.description <> ''
       and not exists (select 1 from public.tech_applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('tech_jobs_text_removed', n);
    if delcut is not null then
      delete from public.tech_jobs j where not j.is_active and j.closed_at < delcut
         and not exists (select 1 from public.tech_applications a where a.company = j.company and a.job_url = j.url);
      get diagnostics n = row_count; r := r || jsonb_build_object('tech_jobs_closed_deleted', n);
    end if;
    delete from public.tech_job_scrape_runs where created_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('tech_job_scrape_runs_deleted', n);
  end if;

  if p_source is null or p_source = 'sports_teams' then
    update public.team_jobs j set description = null, detail_status = null, detail_checked_at = null
     where not j.is_active and j.last_seen_at < cut and j.description is not null and j.description <> ''
       and not exists (select 1 from public.team_applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('team_jobs_text_removed', n);
    if delcut is not null then
      delete from public.team_jobs j where not j.is_active and j.closed_at < delcut
         and not exists (select 1 from public.team_applications a where a.company = j.company and a.job_url = j.url);
      get diagnostics n = row_count; r := r || jsonb_build_object('team_jobs_closed_deleted', n);
      -- sightings of jobs that are no longer stored (only inactive sightings: an active one belongs to a job being written)
      delete from public.team_job_observations o where not o.is_active
         and not exists (select 1 from public.team_jobs j where j.url = o.url);
      get diagnostics n = row_count; r := r || jsonb_build_object('team_job_observations_deleted', n);
    end if;
    delete from public.team_job_scrape_runs where created_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('team_job_scrape_runs_deleted', n);
  end if;

  if p_source is null or p_source = 'sportsbook' then
    update public.jobs j set description = '', description_raw = null, description_clean = null, source_payload = '{}'::jsonb
     where not j.is_active and j.last_seen_at < cut
       and (coalesce(j.description, '') <> '' or coalesce(j.description_raw, '') <> '' or coalesce(j.description_clean, '') <> '')
       and not exists (select 1 from public.applications a where a.company = j.company and a.job_url = j.url);
    get diagnostics n = row_count; r := r || jsonb_build_object('jobs_text_removed', n);
    if delcut is not null then
      -- this table stores closed_at as text; a value that is not a timestamp is never deleted
      delete from public.jobs j where not j.is_active
         and (case when j.closed_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' then j.closed_at::timestamptz end) < delcut
         and not exists (select 1 from public.applications a where a.company = j.company and a.job_url = j.url);
      get diagnostics n = row_count; r := r || jsonb_build_object('jobs_closed_deleted', n);
    end if;
    delete from public.job_scrape_runs where started_at < logcut;
    get diagnostics n = row_count; r := r || jsonb_build_object('job_scrape_runs_deleted', n);
  end if;
  return r;
end
$fn$;
revoke all on function public.apply_job_retention(text, integer, integer) from public, anon, authenticated;
grant execute on function public.apply_job_retention(text, integer, integer) to service_role;
