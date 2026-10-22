-- 0006_company_workspaces: companies the user works at (employers), a daily work log per
-- company, company material, and workplace memory that belongs to one company.
-- Source: ARCHITECTURE.md, "Memory system" (company workspaces), "Security and privacy"
-- (workplace data apart from career data, deletable per company) and "Schema DDL".
--
-- Expand only: new tables, new nullable columns, a wider kind check. Code from before this
-- migration never writes the new columns, and the old standup path keeps working.

set local role app_owner;

-- Employers: companies the user works at, apart from the jobs they apply to (app.jobs).
-- "Current" is an active employer with no end date; more than one may be current.
create table app.employers (
  id          uuid primary key,
  user_id     uuid not null references app.users(id) on delete cascade,
  name        text not null check (length(name) between 1 and 200),
  title       text not null default '',          -- the user's role there
  team        text not null default '',
  started_on  date,
  ended_on    date,
  status      text not null default 'active' check (status in ('active','archived')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (id, user_id)
);
create index employers_user on app.employers (user_id);

-- One work log entry per company and day: what the user worked on, as they wrote it.
-- The text is the user's raw workplace data, so it is sealed with their data key.
-- content_sha256 covers the text and the attachments, so an extraction runs once per content.
create table app.work_logs (
  id               uuid primary key,
  user_id          uuid not null references app.users(id) on delete cascade,
  employer_id      uuid not null,
  day              date not null,
  text_enc         bytea not null,
  key_version      int  not null,
  content_sha256   text not null,
  status           text not null default 'saved'
                   check (status in ('saved','extracting','extracted','failed')),
  extracted_sha256 text,                          -- the content the current nodes came from
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (id, user_id),
  unique (employer_id, day),
  foreign key (employer_id, user_id) references app.employers(id, user_id) on delete cascade
);
create index work_logs_employer_day on app.work_logs (user_id, employer_id, day desc);

-- Company material (architecture notes, wiki pages, meeting notes, specs) and work log
-- attachments (work_log_id set). The object in storage is sealed with the user's data key
-- (associated data: this table and the row id), and so is the file name.
create table app.company_documents (
  id           uuid primary key,
  user_id      uuid not null references app.users(id) on delete cascade,
  employer_id  uuid not null,
  work_log_id  uuid,
  name_enc     bytea not null,
  key_version  int  not null,
  s3_key       text not null,
  sha256       text not null,                     -- of the plaintext file
  content_type text not null
               check (content_type in ('application/pdf','text/plain','text/markdown')),
  size_bytes   int  not null check (size_bytes > 0),
  status       text not null default 'uploaded'
               check (status in ('uploaded','ingesting','ingested','failed')),
  created_at   timestamptz not null default now(),
  unique (id, user_id),
  foreign key (employer_id, user_id) references app.employers(id, user_id) on delete cascade,
  foreign key (work_log_id, user_id) references app.work_logs(id, user_id) on delete cascade
);
create index company_documents_employer on app.company_documents (user_id, employer_id);

-- Workplace memory belongs to one employer; career memory to none. New kinds for meetings:
-- decisions, systems (with their stack) and terms (the company's own words).
-- Adding the keys validates them against existing rows, as app_owner under forced row-level
-- security; lift `force` for this transaction only, as 0003 does.
alter table app.memory_nodes no force row level security;
alter table app.memory_proposals no force row level security;
alter table app.sessions no force row level security;

alter table app.memory_nodes add column employer_id uuid;
alter table app.memory_nodes
  add constraint memory_nodes_employer_fk
    foreign key (employer_id, user_id) references app.employers(id, user_id) on delete cascade;
alter table app.memory_nodes drop constraint memory_nodes_kind_check;
alter table app.memory_nodes
  add constraint memory_nodes_kind_check check (kind in
    ('role','project','story','skill','work_item','commitment','company','person',
     'decision','system','term'));
-- No code wrote workplace nodes before this migration; NOT VALID keeps any such row as it is
-- while every new or changed row must name its employer exactly when it is workplace data.
alter table app.memory_nodes
  add constraint memory_nodes_employer_domain
    check ((domain = 'workplace') = (employer_id is not null)) not valid;
create index memory_nodes_employer on app.memory_nodes (user_id, employer_id, kind)
  where status = 'active' and employer_id is not null;

alter table app.memory_proposals add column employer_id uuid;
alter table app.memory_proposals add column company_document_id uuid;
alter table app.memory_proposals
  add constraint memory_proposals_employer_fk
    foreign key (employer_id, user_id) references app.employers(id, user_id) on delete cascade;
alter table app.memory_proposals
  add constraint memory_proposals_document_fk
    foreign key (company_document_id, user_id)
    references app.company_documents(id, user_id) on delete cascade;
create index memory_proposals_pending on app.memory_proposals (user_id, employer_id)
  where status = 'pending';

-- A standup or meeting at a company. Deleting the company deletes these sessions with their
-- transcripts: they are that company's workplace data too.
alter table app.sessions add column employer_id uuid;
alter table app.sessions add column agenda_enc bytea;
alter table app.sessions add column agenda_key_version int;
alter table app.sessions
  add constraint sessions_employer_fk
    foreign key (employer_id, user_id) references app.employers(id, user_id) on delete cascade;
alter table app.sessions
  add constraint sessions_agenda_and_key_together
    check ((agenda_enc is null) = (agenda_key_version is null));

alter table app.memory_nodes force row level security;
alter table app.memory_proposals force row level security;
alter table app.sessions force row level security;

-- The commitments view gains the employer (a new last column keeps `create or replace`).
create or replace view app.commitments with (security_invoker = true) as
  select id, user_id, title, body->>'due_date' as due_date, body->>'state' as state,
         body->>'session_id' as session_id, employer_id
  from app.memory_nodes where kind = 'commitment';

-- Row-level security and grants, as for every user table.
do $$
declare
  t text;
begin
  foreach t in array array['employers', 'work_logs', 'company_documents'] loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);
    execute format(
      'create policy tenant_isolation on app.%I'
      ' using (user_id = current_setting(''app.user_id'')::uuid)'
      ' with check (user_id = current_setting(''app.user_id'')::uuid)', t);
  end loop;
end
$$;

grant select, insert, update, delete on app.employers, app.work_logs, app.company_documents
  to app_api, app_worker, app_system;
-- The gateway reads the employer (its name for the prompt) next to the memory tables; never
-- the raw logs or documents.
grant select on app.employers to app_gateway;
