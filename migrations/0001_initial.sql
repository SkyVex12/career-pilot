-- 0001_initial: roles, schema `app`, tables, grants and row-level security.
-- Source: ARCHITECTURE.md, "2. Data model" (Conventions and database roles, Schema DDL,
-- Row-level security).
--
-- Run by a role that can create roles: locally the `postgres` superuser, in AWS the
-- cluster's master user. Roles are created NOLOGIN; each environment grants LOGIN and
-- credentials outside migrations, so no passwords live in this repo. Every object
-- below is created as, and owned by, app_owner.

-- Roles ---------------------------------------------------------------------------

do $$
begin
  if not exists (select from pg_roles where rolname = 'app_owner') then
    create role app_owner nologin;       -- migrations only; owns all tables
  end if;
  if not exists (select from pg_roles where rolname = 'app_api') then
    create role app_api nologin;         -- Copilot API: user tables, under RLS
  end if;
  if not exists (select from pg_roles where rolname = 'app_gateway') then
    create role app_gateway nologin;     -- Gateway: read only, memory tables
  end if;
  if not exists (select from pg_roles where rolname = 'app_worker') then
    create role app_worker nologin;      -- Workers: user tables, under RLS
  end if;
  if not exists (select from pg_roles where rolname = 'app_system') then
    create role app_system nologin bypassrls;  -- system jobs only (usage report, user delete)
  end if;

  -- The migration runner must be able to act as app_owner.
  if not pg_has_role(current_user, 'app_owner', 'SET') then
    execute format('grant app_owner to %I', current_user);
  end if;
  execute format('grant create on database %I to app_owner', current_database());
end
$$;

create extension if not exists vector;

set local role app_owner;

-- Schema DDL (verbatim from ARCHITECTURE.md, except where marked) ------------------

create schema if not exists app;

-- Users and encryption keys
create table app.users (
  id            uuid primary key,              -- Cognito sub
  email         text not null unique,
  display_name  text not null,
  plan          text not null default 'free',
  settings      jsonb not null default '{}',   -- retention days, standup time, consent defaults
  external_ref  jsonb not null default '{}',   -- later: Career-OS user id
  created_at    timestamptz not null default now()
);

create table app.user_keys (
  user_id       uuid not null references app.users(id) on delete cascade,
  version       int  not null,
  encrypted_dek bytea not null,                -- data key, encrypted by the KMS key
  created_at    timestamptz not null default now(),
  destroyed_at  timestamptz,
  primary key (user_id, version)
);

-- Jobs and interviews
create table app.jobs (
  id          uuid primary key,
  user_id     uuid not null references app.users(id) on delete cascade,
  company     text not null,
  title       text not null,
  description text not null,
  url         text,
  created_at  timestamptz not null default now()
);

create table app.interviews (
  id         uuid primary key,
  user_id    uuid not null references app.users(id) on delete cascade,
  job_id     uuid not null references app.jobs(id) on delete cascade,
  round      text not null,                    -- phone_screen, technical, system_design, final
  starts_at  timestamptz not null,
  pack_id    uuid
);

-- Memory graph
create table app.memory_nodes (
  id          uuid primary key,
  user_id     uuid not null references app.users(id) on delete cascade,
  kind        text not null check (kind in
                ('role','project','story','skill','work_item','commitment','company','person')),
  domain      text not null check (domain in ('career','workplace')),
  title       text not null,
  summary     text not null,                   -- one line, used in the digest and in search
  body        jsonb not null default '{}',     -- kind-specific fields
  source      jsonb not null,                  -- {"type": "resume|interview|session|github|jira|slack", "ref": "..."}
  valid_from  date,
  valid_to    date,
  status      text not null default 'active' check (status in ('active','archived')),
  search      tsvector generated always as
                (to_tsvector('english', title || ' ' || summary)) stored,
  embedding   vector(1024),                    -- must match the embedding model's dimension
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index memory_nodes_user_kind on app.memory_nodes (user_id, kind) where status = 'active';
create index memory_nodes_search on app.memory_nodes using gin (search);

create table app.memory_edges (
  user_id  uuid not null references app.users(id) on delete cascade,
  src      uuid not null references app.memory_nodes(id) on delete cascade,
  rel      text not null check (rel in
             ('part_of','demonstrates','used','worked_with','about','follows_up')),
  dst      uuid not null references app.memory_nodes(id) on delete cascade,
  primary key (src, rel, dst)
);
create index memory_edges_dst on app.memory_edges (dst);

-- Not in the doc's DDL: security_invoker makes the view apply the caller's privileges
-- and row-level security instead of the view owner's.
create view app.commitments with (security_invoker = true) as
  select id, user_id, title, body->>'due_date' as due_date, body->>'state' as state,
         body->>'session_id' as session_id
  from app.memory_nodes where kind = 'commitment';

create table app.memory_proposals (
  id             uuid primary key,
  user_id        uuid not null references app.users(id) on delete cascade,
  session_id     uuid,
  source         jsonb not null,
  action         text not null check (action in ('create','update','merge')),
  target_node_id uuid references app.memory_nodes(id) on delete cascade,
  payload        jsonb not null,               -- proposed node, or changed fields
  status         text not null default 'pending' check (status in ('pending','approved','rejected')),
  created_at     timestamptz not null default now(),
  decided_at     timestamptz
);

-- Sessions and logs
create table app.sessions (
  id               uuid primary key,
  user_id          uuid not null references app.users(id) on delete cascade,
  mode             text not null check (mode in ('interview','standup','meeting')),
  job_id           uuid references app.jobs(id) on delete set null,
  interview_id     uuid references app.interviews(id) on delete set null,
  platform         text,
  title            text,
  store_transcript boolean not null,
  started_at       timestamptz not null,
  ended_at         timestamptz,
  billed_seconds   int not null default 0,
  me_audio_status  text not null default 'pending'
                   check (me_audio_status in ('pending','uploaded','transcribed','none'))
);
create index sessions_user_started on app.sessions (user_id, started_at desc);

create table app.transcript_segments (
  session_id  uuid not null references app.sessions(id) on delete cascade,
  channel     text not null check (channel in ('them','me')),
  seg_id      int  not null,
  user_id     uuid not null,
  start_ms    int  not null,
  end_ms      int  not null,
  text_enc    bytea not null,
  key_version int  not null,
  primary key (session_id, channel, seg_id)
);

create table app.questions (
  id                uuid primary key,
  session_id        uuid not null references app.sessions(id) on delete cascade,
  user_id           uuid not null,
  text_enc          bytea not null,
  key_version       int  not null,
  type              text not null,
  addressed_to_user boolean not null,
  speculative_kept  boolean not null,
  forced_by_hotkey  boolean not null default false,
  detected_at       timestamptz not null
);

create table app.answers (
  id              uuid primary key,
  question_id     uuid not null references app.questions(id) on delete cascade,
  user_id         uuid not null,
  pass            text not null check (pass in ('fast','deep')),
  model           text not null,
  tier            text not null,
  text_enc        bytea,
  key_version     int,
  input_tokens    int,
  cached_tokens   int,
  output_tokens   int,
  cost_usd        numeric(10,6),
  latency_ms      jsonb not null,              -- {"turn_end": 410, "retrieval": 38, "first_token": 520}
  memory_node_ids uuid[] not null default '{}',
  expanded        boolean not null default false,
  feedback        text check (feedback in ('up','down')),
  created_at      timestamptz not null default now()
);

create table app.session_outputs (
  id          uuid primary key,
  session_id  uuid not null references app.sessions(id) on delete cascade,
  user_id     uuid not null,
  kind        text not null check (kind in ('summary','feedback','follow_up_email','standup_report')),
  content_enc bytea not null,
  key_version int  not null,
  created_at  timestamptz not null default now()
);

-- Documents, integrations, usage
create table app.documents (
  id         uuid primary key,
  user_id    uuid not null references app.users(id) on delete cascade,
  kind       text not null check (kind in ('resume','job_description','linkedin_export','other')),
  s3_key     text not null,
  sha256     text not null,
  status     text not null default 'uploaded' check (status in ('uploaded','ingested','failed')),
  created_at timestamptz not null default now()
);

create table app.integrations (
  user_id      uuid not null references app.users(id) on delete cascade,
  provider     text not null check (provider in
                 ('github','jira','slack','google_calendar','microsoft_calendar')),
  token_enc    bytea not null,                 -- OAuth tokens, encrypted with the user's data key
  key_version  int   not null,
  scopes       text[] not null,
  status       text not null default 'active',
  last_sync_at timestamptz,
  primary key (user_id, provider)
);

create table app.usage_daily (
  user_id      uuid not null references app.users(id) on delete cascade,
  day          date not null,
  live_seconds int  not null default 0,
  reported_at  timestamptz,
  primary key (user_id, day)
);

-- System table, no row-level security, workers only
create table app.job_runs (
  idempotency_key text primary key,
  job_type        text not null,
  user_id         uuid,
  status          text not null check (status in ('running','done','failed')),
  attempts        int  not null default 1,
  started_at      timestamptz not null default now(),
  finished_at     timestamptz,
  error           text
);

-- Row-level security ----------------------------------------------------------------
-- Every user table, keyed on user_id (users on id). current_setting('app.user_id')
-- raises when the setting is missing, so a query without a user fails closed.
-- migrations/tests/test_rls.py checks that no user table is left out.

alter table app.users enable row level security;
alter table app.users force row level security;
create policy tenant_isolation on app.users
  using (id = current_setting('app.user_id')::uuid)
  with check (id = current_setting('app.user_id')::uuid);

do $$
declare
  t text;
begin
  foreach t in array array[
    'user_keys', 'jobs', 'interviews', 'memory_nodes', 'memory_edges', 'memory_proposals',
    'sessions', 'transcript_segments', 'questions', 'answers', 'session_outputs',
    'documents', 'integrations', 'usage_daily'
  ] loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);
    execute format(
      'create policy tenant_isolation on app.%I'
      ' using (user_id = current_setting(''app.user_id'')::uuid)'
      ' with check (user_id = current_setting(''app.user_id'')::uuid)', t);
  end loop;
end
$$;

-- Grants (ARCHITECTURE.md, "Conventions and database roles") ------------------------

grant usage on schema app to app_api, app_gateway, app_worker, app_system;

-- API and workers: read and write user tables, under row-level security.
grant select, insert, update, delete on
  app.users, app.user_keys, app.jobs, app.interviews, app.memory_nodes, app.memory_edges,
  app.memory_proposals, app.sessions, app.transcript_segments, app.questions, app.answers,
  app.session_outputs, app.documents, app.integrations, app.usage_daily
  to app_api, app_worker;
grant select on app.commitments to app_api, app_worker;

-- Gateway: read only, and only the tables it needs for live answers.
grant select on app.users, app.jobs, app.interviews, app.memory_nodes, app.memory_edges
  to app_gateway;

-- job_runs is a system table for workers.
grant select, insert, update, delete on app.job_runs to app_worker;

-- System jobs bypass row-level security (BYPASSRLS on the role).
grant select, insert, update, delete on all tables in schema app to app_system;
