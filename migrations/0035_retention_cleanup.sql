-- Explicit history expiry and restartable object cleanup, independent of accounting.
set local role app_owner;
do $$ declare r record; begin
 for r in select oid::regclass as tab from pg_class
  where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
  execute format('alter table %s no force row level security',r.tab);
 end loop;
end $$;

alter table app.sessions add column expires_at timestamptz;
alter table app.sessions add column retention_days integer check(retention_days between 1 and 3650);
alter table app.sessions add column retention_policy_version text not null default 'workspace-v1';
update app.sessions s set retention_days=(w.settings->>'retention_days')::integer,
 expires_at=s.started_at+make_interval(days=>(w.settings->>'retention_days')::integer)
 from app.workspaces w where w.id=s.user_id and w.settings->>'retention_days' ~ '^[0-9]{1,4}$'
 and (w.settings->>'retention_days')::integer between 1 and 3650;
create index sessions_retention_due on app.sessions(expires_at,id)
 where expires_at is not null and ended_at is not null;

create function app.session_retention() returns trigger language plpgsql as $$
declare days text;
begin
 select settings->>'retention_days' into days from app.workspaces where id=new.user_id;
 if days ~ '^[0-9]{1,4}$' and days::integer between 1 and 3650 then
  new.retention_days:=days::integer;
  new.expires_at:=new.started_at+make_interval(days=>days::integer);
 else new.retention_days:=null; new.expires_at:=null;
 end if;
 return new;
end $$;
create trigger session_retention_snapshot before insert on app.sessions
 for each row execute function app.session_retention();

-- No content or credentials: a deletion intent survives deletion of the owning domain row.
-- The janitor checks that no retained revision still references the object before removing it.
create table app.object_deletions (
 object_key text primary key,
 workspace_id uuid not null,
 requested_at timestamptz not null default clock_timestamp(),
 attempts integer not null default 0,
 next_attempt_at timestamptz not null default clock_timestamp(),
 completed_at timestamptz,
 state text not null default 'pending' check(state in ('pending','deleting','done')),
 last_error text
);
create index object_deletions_pending on app.object_deletions(next_attempt_at,object_key)
 where completed_at is null;
revoke all on app.object_deletions from public,app_api,app_gateway,app_worker;
grant select,insert,update,delete on app.object_deletions to app_system;

create function app.record_object_deletion() returns trigger language plpgsql
 security definer set search_path=pg_catalog,app as $$
declare k text; wid uuid;
begin
 if tg_table_name='resume_revisions' then k:=old.file_s3_key; wid:=old.workspace_id;
 elsif tg_table_name='company_document_revisions' then k:=old.s3_key; wid:=old.workspace_id;
 else k:=old.s3_key; wid:=old.user_id;
 end if;
 if k is not null then
  -- Serialize the very first intent with references whose insert has not committed.
  perform pg_advisory_xact_lock(hashtextextended(k,735012));
  insert into app.object_deletions(object_key,workspace_id) values(k,wid)
   on conflict(object_key) do update set completed_at=null,next_attempt_at=clock_timestamp(),
    state=case when app.object_deletions.state='deleting' then 'deleting' else 'pending' end;
 end if;
 return null;
end $$;
revoke all on function app.record_object_deletion() from public;
create trigger document_object_delete after delete on app.documents
 for each row execute function app.record_object_deletion();
create trigger company_object_delete after delete on app.company_documents
 for each row execute function app.record_object_deletion();
create trigger company_revision_object_delete after delete on app.company_document_revisions
 for each row execute function app.record_object_deletion();
create trigger resume_object_delete after delete on app.resume_revisions
 for each row execute function app.record_object_deletion();
create index resume_revisions_object on app.resume_revisions(file_s3_key) where file_s3_key is not null;
create index resume_versions_object on app.resume_versions(file_s3_key) where file_s3_key is not null;
create index company_documents_object on app.company_documents(s3_key);
create index company_revisions_object on app.company_document_revisions(s3_key);
create index documents_object on app.documents(s3_key);

-- New references serialize with the deletion reservation. Deleted object keys cannot be
-- recycled; uploads allocate a new key. This closes check/delete/re-reference races.
create function app.guard_object_reference() returns trigger language plpgsql
 security definer set search_path=pg_catalog,app as $$
declare k text; state text;
begin
 if tg_table_name in ('resume_revisions','resume_versions') then k:=new.file_s3_key;
 else k:=new.s3_key;
 end if;
 if k is null then return new; end if;
 perform pg_advisory_xact_lock(hashtextextended(k,735012));
 select d.state into state from app.object_deletions d where d.object_key=k for update;
 if state in ('deleting','done') then
  raise exception 'object is being deleted; allocate a new immutable object key' using errcode='23514';
 end if;
 return new;
end $$;
revoke all on function app.guard_object_reference() from public;
create trigger guard_object before insert or update of s3_key on app.documents
 for each row execute function app.guard_object_reference();
create trigger guard_object before insert or update of s3_key on app.company_documents
 for each row execute function app.guard_object_reference();
create trigger guard_object before insert or update of file_s3_key on app.resume_versions
 for each row execute function app.guard_object_reference();
create trigger guard_object before insert on app.resume_revisions
 for each row execute function app.guard_object_reference();
create trigger guard_object before insert on app.company_document_revisions
 for each row execute function app.guard_object_reference();

do $$ declare r record; begin
 for r in select oid::regclass as tab from pg_class
  where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
  execute format('alter table %s force row level security',r.tab);
 end loop;
end $$;
