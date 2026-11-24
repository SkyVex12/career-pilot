-- Connected-time metering is independent of retained interview content. No FK to
-- users/sessions: deleting history or an account must not silently erase accounting.
-- Runtime roles cannot edit accepted events, intervals, estimates or adjustments.
set local role app_owner;

alter table app.sessions add column metering_version text not null default 'legacy-wall-v0'
  check (metering_version in ('legacy-wall-v0', 'connected-v1'));

-- Global product configuration, containing no customer data. A version identifies
-- exactly one currency/rate forever; a pricing change needs a new version.
create table app.usage_prices (
  version text primary key check (length(version) between 1 and 128),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  rate_minor_per_hour bigint not null check (rate_minor_per_hour >= 0),
  unique(version, currency, rate_minor_per_hour)
);
grant select on app.usage_prices to app_api,app_worker,app_system;
grant insert on app.usage_prices to app_worker;

create table app.live_usage_sessions (
  session_id uuid primary key,
  workspace_id uuid not null,
  payer_account_id uuid not null,
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  rate_minor_per_hour bigint not null check (rate_minor_per_hour >= 0),
  price_version text not null check (length(price_version) between 1 and 128),
  covered tstzmultirange not null default '{}',
  created_at timestamptz not null default clock_timestamp(),
  unique (session_id, workspace_id, payer_account_id),
  foreign key (price_version,currency,rate_minor_per_hour)
    references app.usage_prices(version,currency,rate_minor_per_hour)
);
create index live_usage_sessions_payer on app.live_usage_sessions(payer_account_id, session_id);

create table app.live_usage_events (
  event_id uuid primary key,
  session_id uuid not null,
  workspace_id uuid not null,
  payer_account_id uuid not null,
  started_at timestamptz not null,
  ended_at timestamptz not null check (ended_at > started_at),
  recorded_microseconds bigint not null check (recorded_microseconds >= 0),
  recorded_at timestamptz not null default clock_timestamp(),
  unique (event_id, session_id, workspace_id, payer_account_id),
  foreign key (session_id, workspace_id, payer_account_id)
    references app.live_usage_sessions(session_id, workspace_id, payer_account_id)
);

create table app.live_usage_intervals (
  event_id uuid not null,
  part int not null check (part >= 0),
  session_id uuid not null,
  workspace_id uuid not null,
  payer_account_id uuid not null,
  started_at timestamptz not null,
  ended_at timestamptz not null check (ended_at > started_at),
  duration_microseconds bigint generated always as
    ((extract(epoch from ended_at - started_at) * 1000000)::bigint) stored,
  day date generated always as ((started_at at time zone 'UTC')::date) stored,
  primary key (event_id, part),
  foreign key (event_id, session_id, workspace_id, payer_account_id)
    references app.live_usage_events(event_id, session_id, workspace_id, payer_account_id),
  -- Every accepted segment fits within one UTC day; reporting cannot assign an
  -- entire cross-midnight call to its start date/month.
  check (ended_at <= (date_trunc('day', started_at at time zone 'UTC')
                     + interval '1 day') at time zone 'UTC')
);
create index live_usage_intervals_payer_day on app.live_usage_intervals(payer_account_id, day);
create index live_usage_intervals_day_workspace on app.live_usage_intervals(day, workspace_id);
create index live_usage_intervals_session on app.live_usage_intervals(session_id);

create table app.usage_adjustments (
  adjustment_id uuid primary key,
  session_id uuid not null,
  workspace_id uuid not null,
  payer_account_id uuid not null,
  amount_minor bigint not null check (amount_minor <> 0),
  reason text not null check (length(reason) between 1 and 1024),
  effective_at timestamptz not null,
  recorded_at timestamptz not null default clock_timestamp(),
  foreign key (session_id, workspace_id, payer_account_id)
    references app.live_usage_sessions(session_id, workspace_id, payer_account_id)
);
create index usage_adjustments_payer_time on app.usage_adjustments(payer_account_id, effective_at);

create table app.legacy_usage_estimates (
  session_id uuid primary key,
  workspace_id uuid not null,
  -- Retained sessions do not reliably identify the original paying account.
  payer_account_id uuid,
  started_at timestamptz not null,
  ended_at timestamptz not null check (ended_at > started_at),
  duration_microseconds bigint generated always as
    ((extract(epoch from ended_at - started_at) * 1000000)::bigint) stored,
  source text not null default 'legacy-wall-v0' check (source = 'legacy-wall-v0'),
  billable boolean not null default false check (not billable),
  recorded_at timestamptz not null default clock_timestamp()
);
create index legacy_usage_estimates_workspace_time
  on app.legacy_usage_estimates(workspace_id, started_at);

do $$ declare t text; begin
  foreach t in array array['live_usage_sessions','live_usage_events','live_usage_intervals',
                          'usage_adjustments','legacy_usage_estimates'] loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);
    -- Migration owner also runs the strictly trigger-only legacy snapshot function.
    execute format('create policy migration_owner on app.%I to app_owner '
                   'using (true) with check (true)', t);
    execute format('create policy worker_scope on app.%I to app_worker '
      'using (workspace_id=nullif(current_setting(''app.user_id'',true),'''')::uuid) '
      'with check (workspace_id=nullif(current_setting(''app.user_id'',true),'''')::uuid)',t);
  end loop;
  foreach t in array array['live_usage_sessions','live_usage_events','live_usage_intervals',
                          'usage_adjustments'] loop
    execute format('create policy payer_reads on app.%I for select to app_api '
      'using (payer_account_id=nullif(current_setting(''app.user_id'',true),'''')::uuid)',t);
  end loop;
end $$;
create policy workspace_estimates on app.legacy_usage_estimates for select to app_api
  using (workspace_id=nullif(current_setting('app.user_id',true),'')::uuid);

grant select on app.live_usage_sessions, app.live_usage_events, app.live_usage_intervals,
  app.usage_adjustments, app.legacy_usage_estimates to app_api, app_worker, app_system;
grant insert on app.live_usage_sessions, app.live_usage_events, app.live_usage_intervals,
  app.usage_adjustments to app_worker;
grant update(covered) on app.live_usage_sessions to app_worker;

-- An INSERT/UPDATE of a completed old session preserves its first historical
-- estimate, including when an older gateway finalizes after this migration.
create function app.snapshot_legacy_usage() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
begin
  if new.metering_version='legacy-wall-v0' and new.ended_at is not null
     and new.billed_seconds>0 then
    insert into app.legacy_usage_estimates(session_id,workspace_id,started_at,ended_at)
      values(new.id,new.user_id,new.started_at,
             new.started_at+make_interval(secs=>new.billed_seconds))
      on conflict(session_id) do nothing;
  end if;
  return null;
end $$;
revoke all on function app.snapshot_legacy_usage() from public;
create trigger usage_legacy_snapshot after insert or update of ended_at,billed_seconds
  on app.sessions for each row execute function app.snapshot_legacy_usage();

alter table app.sessions no force row level security;
insert into app.legacy_usage_estimates(session_id,workspace_id,started_at,ended_at)
  select id,user_id,started_at,started_at+make_interval(secs=>billed_seconds)
  from app.sessions where ended_at is not null and billed_seconds>0;
alter table app.sessions force row level security;
