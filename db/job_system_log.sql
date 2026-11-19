create table if not exists public.job_system_log (
  id bigint generated always as identity primary key,
  logged_at timestamptz not null default now(),
  source text not null,
  database_bytes bigint not null,
  detail jsonb not null
);
alter table public.job_system_log enable row level security;
create index if not exists job_system_log_logged_at on public.job_system_log (logged_at desc);

create or replace function public.log_job_system(p_source text)
returns jsonb language plpgsql security definer set search_path = public, pg_catalog
-- The REST API cancels a call after 8 seconds. On 2026-11-19 two log calls were cancelled while another scraper
-- was rewriting 11,006 rows. A setting on the function itself lifts the limit for this call only (proven with a
-- 10-second probe function: cancelled without the setting, HTTP 200 with it).
set statement_timeout = '50s' as $fn$
declare d jsonb; b bigint; ret jsonb;
begin
  b := pg_database_size(current_database());
  -- retention for the scraper that just ran (see job_retention.sql); a failure here never stops the log row
  begin
    if p_source in ('robotics', 'tech', 'sports_teams', 'sportsbook') then
      ret := public.apply_job_retention(p_source);
    end if;
  exception when others then
    ret := jsonb_build_object('error', left(sqlerrm, 300));
  end;
  select jsonb_build_object(
    'jobs', jsonb_build_object('robotics_jobs', (select jsonb_build_object('rows', count(*), 'active', count(*) filter (where is_active), 'newest_seen', max(last_seen_at), 'bytes', pg_total_relation_size('public.robotics_jobs')) from public.robotics_jobs), 'tech_jobs', (select jsonb_build_object('rows', count(*), 'active', count(*) filter (where is_active), 'newest_seen', max(last_seen_at), 'bytes', pg_total_relation_size('public.tech_jobs')) from public.tech_jobs), 'team_jobs', (select jsonb_build_object('rows', count(*), 'active', count(*) filter (where is_active), 'newest_seen', max(last_seen_at), 'bytes', pg_total_relation_size('public.team_jobs')) from public.team_jobs), 'jobs', (select jsonb_build_object('rows', count(*), 'active', count(*) filter (where is_active), 'newest_seen', max(last_seen_at), 'bytes', pg_total_relation_size('public.jobs')) from public.jobs)),
    'run_logs', jsonb_build_object('robotics_job_scrape_runs', (select jsonb_build_object('rows', count(*), 'bytes', pg_total_relation_size('public.robotics_job_scrape_runs'), 'not_ok_24h', count(*) filter (where created_at > now() - interval '24 hours' and status not in ('success', 'empty')), 'newest', max(created_at)) from public.robotics_job_scrape_runs), 'tech_job_scrape_runs', (select jsonb_build_object('rows', count(*), 'bytes', pg_total_relation_size('public.tech_job_scrape_runs'), 'not_ok_24h', count(*) filter (where created_at > now() - interval '24 hours' and status not in ('success', 'empty')), 'newest', max(created_at)) from public.tech_job_scrape_runs),
                                   'team_job_scrape_runs', (select jsonb_build_object('rows', count(*), 'bytes', pg_total_relation_size('public.team_job_scrape_runs'), 'not_ok_24h', count(*) filter (where created_at > now() - interval '24 hours' and status not in ('success', 'empty')), 'newest', max(created_at)) from public.team_job_scrape_runs), 'job_scrape_runs', (select jsonb_build_object('rows', count(*), 'bytes', pg_total_relation_size('public.job_scrape_runs'), 'not_ok_24h', count(*) filter (where started_at > now() - interval '24 hours' and status not in ('success', 'empty')), 'newest', max(started_at)) from public.job_scrape_runs)),
    'waiting_for_discord', jsonb_build_object(
      'robotics', (select count(*) from public.robotics_job_notifications where state = 'pending'),
      'tech', (select count(*) from public.tech_job_notifications where state = 'pending'),
      'sportsbook', (select count(*) from public.sportsbook_job_notifications where state = 'pending')),
    'retention', coalesce(ret, 'null'::jsonb)
  ) into d;
  insert into public.job_system_log (source, database_bytes, detail) values (left(coalesce(p_source, 'unknown'), 40), b, d);
  return jsonb_build_object('logged', true, 'source', p_source, 'database_bytes', b, 'detail', d);
end
$fn$;
revoke all on function public.log_job_system(text) from public, anon, authenticated;
grant execute on function public.log_job_system(text) to service_role;
revoke all on table public.job_system_log from anon, authenticated;
