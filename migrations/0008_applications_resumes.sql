-- 0008_applications_resumes: what Career Pilot called a job becomes an application, shaped
-- like Career-OS's (company, role, URL, source site, stage, note, JD text); one base resume
-- plus tailored resume versions, each tailored version owned by one application; an
-- application's pairing with a version; career memory per resume version; the Career-OS link.
-- Source: ARCHITECTURE.md, "Memory system" (resume versions), "Schema DDL" and "Row-level
-- security".
--
-- Expand only. `app.jobs` becomes a view over `app.applications` (security_invoker, auto
-- updatable), so code from before this migration keeps reading and writing jobs during the
-- deploy; 0009 drops it once no older service or desktop build needs it. Existing data moves
-- in place: jobs keep their ids (interviews and sessions keep pointing at them), the newest
-- ingested resume becomes the base version (with that document's id), and every node
-- extracted from a resume belongs to it.

set local role app_owner;

-- Updates below must see every user's rows, and new keys are validated against existing rows
-- as app_owner: lift forced row-level security for this transaction only, as 0003 does.
alter table app.jobs no force row level security;
alter table app.memory_nodes no force row level security;
alter table app.documents no force row level security;
alter table app.integrations no force row level security;
alter table app.interviews no force row level security;

-- Applications ------------------------------------------------------------------------------

alter table app.jobs rename to applications;
alter table app.applications rename column title to role;
alter table app.applications rename column description to jd_text;
alter table app.applications
  add column source_site text check (length(source_site) <= 200),
  add column note text check (length(note) <= 10000),
  add column stage text not null default 'applied'
    check (stage in ('applied','interview','offer','rejected')),   -- Career-OS's stages
  add column resume_version_id uuid,           -- the user's pairing; null pairs automatically
  add column pairing_rev int not null default 0,   -- bumped on every pairing change
  add column career_os_application_id text,    -- the Career-OS application it was pulled from
  add column career_os_hash text,              -- Career-OS's content hash at that pull
  add column pulled_at timestamptz,
  add column updated_at timestamptz not null default now(),
  add constraint applications_career_os_together
    check ((career_os_application_id is null) = (pulled_at is null));
create unique index applications_career_os on app.applications (user_id, career_os_application_id)
  where career_os_application_id is not null;
create index applications_user on app.applications (user_id, created_at desc);

-- A job with an interview was past the application stage.
update app.applications a set stage = 'interview'
  where exists (select 1 from app.interviews i where i.job_id = a.id);

-- Code from before 0008 still runs during the deploy and reads and writes app.jobs.
create view app.jobs with (security_invoker = true) as
  select id, user_id, company, role as title, jd_text as description, url, created_at
  from app.applications;

-- Resume versions ---------------------------------------------------------------------------
-- One base resume per user, plus tailored versions, each owned by one application (deleted
-- with it). A version keeps its id while its content changes; its memory nodes are extracted
-- again on each new content. The content is an uploaded file (app.documents), pasted text, or
-- a copy pulled from Career-OS (its text, plus its generated file when it has one). Text and
-- pulled files are the user's raw documents: sealed with the user's data key (associated
-- data: this table and the row id).

alter table app.documents add constraint documents_id_user_key unique (id, user_id);

create table app.resume_versions (
  id                uuid primary key,
  user_id           uuid not null references app.users(id) on delete cascade,
  kind              text not null check (kind in ('base','tailored')),
  application_id    uuid,
  label             text not null check (length(label) between 1 and 200),
  source            text not null check (source in ('file','text','career_os')),
  document_id       uuid,                        -- an uploaded file
  text_enc          bytea,                       -- pasted or pulled text, sealed
  key_version       int,
  file_s3_key       text,                        -- a pulled Career-OS file, sealed
  file_name_enc     bytea,
  file_content_type text,
  content_sha256    text not null,
  extracted_sha256  text,                        -- the content its current nodes came from
  status            text not null default 'saved'
                    check (status in ('saved','extracting','extracted','failed')),
  career_os_ref     text,                        -- resume_version:<id> or base:<user id>
  pulled_at         timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (id, user_id),
  check ((kind = 'tailored') = (application_id is not null)),
  check ((text_enc is null) = (key_version is null)),
  check ((file_s3_key is null) = (file_name_enc is null)),
  check (document_id is not null or text_enc is not null),
  foreign key (application_id, user_id) references app.applications(id, user_id)
    on delete cascade,
  -- A version's file is its content: the document cannot go while the version needs it.
  foreign key (document_id, user_id) references app.documents(id, user_id)
);
create unique index resume_versions_one_base on app.resume_versions (user_id) where kind = 'base';
create index resume_versions_application on app.resume_versions (user_id, application_id);

alter table app.applications
  add constraint applications_resume_version_fk
    foreign key (resume_version_id, user_id) references app.resume_versions(id, user_id)
    on delete set null (resume_version_id);

-- A resume upload says what it is for: an application's tailored version (application_id),
-- or a version whose content it replaces (resume_version_id); neither means the base. A job
-- description upload with application_id fills that application's JD.
alter table app.documents
  add column application_id uuid,
  add column resume_version_id uuid,
  add column label text check (length(label) between 1 and 200),
  add constraint documents_application_fk
    foreign key (application_id, user_id) references app.applications(id, user_id)
    on delete set null (application_id),
  add constraint documents_resume_version_fk
    foreign key (resume_version_id, user_id) references app.resume_versions(id, user_id)
    on delete set null (resume_version_id);

-- A pairing is the base or one of the application's own tailored versions, never another
-- application's. Runs as the caller, so row-level security applies to the lookup too.
create function app.check_application_pairing() returns trigger
language plpgsql as $$
begin
  -- A version that does not exist (or is another user's) is left to the foreign key.
  if new.resume_version_id is not null and exists (
    select 1 from app.resume_versions v
    where v.id = new.resume_version_id and v.user_id = new.user_id
      and v.kind = 'tailored' and v.application_id <> new.id
  ) then
    raise exception 'resume version % belongs to another application', new.resume_version_id
      using errcode = '23514';
  end if;
  return new;
end
$$;
create trigger applications_pairing before insert or update of resume_version_id
  on app.applications for each row execute function app.check_application_pairing();

-- A version never moves to another application or changes kind.
create function app.keep_resume_version_owner() returns trigger
language plpgsql as $$
begin
  if new.kind is distinct from old.kind or new.application_id is distinct from old.application_id
  then
    raise exception 'a resume version keeps its kind and application' using errcode = '23514';
  end if;
  return new;
end
$$;
create trigger resume_versions_owner before update of kind, application_id
  on app.resume_versions for each row execute function app.keep_resume_version_owner();

-- The version an application's interview packs use: the user's pairing, else the
-- application's newest tailored version, else the base. Null with no resume at all.
create function app.paired_resume(application uuid) returns uuid
language sql stable security invoker as $$
  select coalesce(
    (select a.resume_version_id from app.applications a where a.id = application),
    (select v.id from app.resume_versions v
      where v.application_id = application and v.kind = 'tailored'
      order by v.created_at desc, v.id desc limit 1),
    (select v.id from app.resume_versions v join app.applications a on a.user_id = v.user_id
      where a.id = application and v.kind = 'base'))
$$;

-- Career memory per resume version: every node extracted from a resume names its version and
-- is deleted with it. An interview's digest and pack read only its version's nodes.
alter table app.memory_nodes add column resume_version_id uuid;
alter table app.memory_nodes
  add constraint memory_nodes_resume_version_fk
    foreign key (resume_version_id, user_id) references app.resume_versions(id, user_id)
    on delete cascade;
create index memory_nodes_resume_version on app.memory_nodes (user_id, resume_version_id, kind)
  where status = 'active' and resume_version_id is not null;

-- Existing data: the newest ingested resume becomes the base version, with that document's id;
-- every resume node (active, or archived by a later resume) belongs to it.
insert into app.resume_versions
  (id, user_id, kind, label, source, document_id, content_sha256, extracted_sha256, status,
   created_at, updated_at)
select distinct on (d.user_id)
  d.id, d.user_id, 'base', 'Base resume', 'file', d.id, d.sha256, d.sha256, 'extracted',
  d.created_at, now()
from app.documents d
where d.kind = 'resume' and d.status = 'ingested'
order by d.user_id, d.created_at desc, d.id desc;

update app.documents d set resume_version_id = d.id
  where exists (select 1 from app.resume_versions v where v.id = d.id);

update app.memory_nodes n set resume_version_id = v.id
  from app.resume_versions v
  where v.user_id = n.user_id and v.kind = 'base' and n.source->>'type' = 'resume';

alter table app.memory_nodes
  add constraint memory_nodes_resume_has_version
    check (source->>'type' is distinct from 'resume' or resume_version_id is not null);

-- The Career-OS link ------------------------------------------------------------------------
-- One link per account (the key is user_id and provider). The read-only token Career-OS
-- issued is sealed with the user's data key, like every tool token.
alter table app.integrations drop constraint integrations_provider_check;
alter table app.integrations
  add constraint integrations_provider_check check (provider in
    ('github','jira','slack','google_calendar','microsoft_calendar','career_os')),
  add column external_id text,                -- the Career-OS user id
  add column external_label text,             -- its name, for the settings screen
  add column connected_at timestamptz;

alter table app.applications force row level security;
alter table app.memory_nodes force row level security;
alter table app.documents force row level security;
alter table app.integrations force row level security;
alter table app.interviews force row level security;

-- Row-level security and grants -------------------------------------------------------------

alter table app.resume_versions enable row level security;
alter table app.resume_versions force row level security;
create policy tenant_isolation on app.resume_versions
  using (user_id = current_setting('app.user_id')::uuid)
  with check (user_id = current_setting('app.user_id')::uuid);

grant select, insert, update, delete on app.resume_versions, app.jobs
  to app_api, app_worker, app_system;
-- The gateway resolves an interview's resume version (metadata only, never the text or file).
grant select on app.jobs to app_gateway;
grant select (id, user_id, kind, application_id, status, label, created_at, updated_at)
  on app.resume_versions to app_gateway;
