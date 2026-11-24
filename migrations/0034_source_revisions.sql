-- Stable source containers with immutable content revisions. Existing UUIDs and encryption
-- associated data stay valid. Current rows remain convenient projections for existing clients.
set local role app_owner;

do $$ declare r record; begin
 for r in select oid::regclass as tab from pg_class
  where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
  execute format('alter table %s no force row level security',r.tab);
 end loop;
end $$;

alter table app.resume_versions no force row level security;
alter table app.work_logs no force row level security;
alter table app.company_documents no force row level security;
alter table app.knowledge_chunks no force row level security;
alter table app.sessions no force row level security;
alter table app.resume_versions add column file_sha256 text;

create table app.resume_revisions (
  id uuid primary key default gen_random_uuid(),
  resume_version_id uuid not null,
  workspace_id uuid not null references app.workspaces(id) on delete cascade,
  revision_number int not null check(revision_number>0),
  content_sha256 text not null,
  source text not null,
  document_id uuid,
  text_enc bytea,
  key_version int,
  file_s3_key text,
  file_name_enc bytea,
  file_content_type text,
  file_sha256 text,
  created_at timestamptz not null default now(),
  unique(resume_version_id,revision_number),
  unique(id,workspace_id),
  unique(id,resume_version_id,workspace_id),
  foreign key(resume_version_id,workspace_id) references app.resume_versions(id,user_id)
    on delete cascade,
  check((text_enc is null)=(key_version is null))
);
create table app.resume_revision_extractions (
  revision_id uuid primary key,
  workspace_id uuid not null,
  text_enc bytea not null,
  key_version int not null,
  created_at timestamptz not null default now(),
  foreign key(revision_id,workspace_id) references app.resume_revisions(id,workspace_id)
    on delete cascade
);
create table app.work_log_revisions (
  id uuid primary key default gen_random_uuid(),
  work_log_id uuid not null,
  workspace_id uuid not null references app.workspaces(id) on delete cascade,
  employer_id uuid not null,
  revision_number int not null check(revision_number>0),
  content_sha256 text not null,
  text_enc bytea not null,
  key_version int not null,
  day date not null,
  created_at timestamptz not null default now(),
  unique(work_log_id,revision_number),
  unique(id,workspace_id),
  unique(id,work_log_id,workspace_id),
  foreign key(work_log_id,workspace_id) references app.work_logs(id,user_id) on delete cascade,
  foreign key(employer_id,workspace_id) references app.employers(id,user_id) on delete cascade
);
create table app.company_document_revisions (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null,
  workspace_id uuid not null references app.workspaces(id) on delete cascade,
  employer_id uuid not null,
  revision_number int not null check(revision_number>0),
  content_sha256 text not null,
  s3_key text not null,
  name_enc bytea not null,
  key_version int not null,
  content_type text not null,
  size_bytes int not null check(size_bytes>0),
  created_at timestamptz not null default now(),
  unique(document_id,revision_number),
  unique(id,workspace_id),
  unique(id,document_id,workspace_id),
  foreign key(document_id,workspace_id) references app.company_documents(id,user_id)
    on delete cascade,
  foreign key(employer_id,workspace_id) references app.employers(id,user_id) on delete cascade
);

alter table app.resume_versions add column current_revision_id uuid;
alter table app.work_logs add column current_revision_id uuid;
alter table app.company_documents add column current_revision_id uuid;

insert into app.resume_revisions
 (resume_version_id,workspace_id,revision_number,content_sha256,source,document_id,text_enc,
  key_version,file_s3_key,file_name_enc,file_content_type,created_at)
 select id,user_id,1,content_sha256,source,document_id,text_enc,key_version,file_s3_key,
        file_name_enc,file_content_type,created_at from app.resume_versions;
update app.resume_versions v set current_revision_id=r.id
 from app.resume_revisions r where r.resume_version_id=v.id;
insert into app.resume_revision_extractions(revision_id,workspace_id,text_enc,key_version)
 select current_revision_id,user_id,original_text_enc,original_text_key_version
 from app.resume_versions where original_text_enc is not null
 and original_content_sha256=content_sha256;
insert into app.work_log_revisions
 (work_log_id,workspace_id,employer_id,revision_number,content_sha256,text_enc,key_version,day,
  created_at)
 select id,user_id,employer_id,1,content_sha256,text_enc,key_version,day,created_at
 from app.work_logs;
update app.work_logs v set current_revision_id=r.id
 from app.work_log_revisions r where r.work_log_id=v.id;
insert into app.company_document_revisions
 (document_id,workspace_id,employer_id,revision_number,content_sha256,s3_key,name_enc,key_version,
  content_type,size_bytes,created_at)
 select id,user_id,employer_id,1,sha256,s3_key,name_enc,key_version,content_type,size_bytes,created_at
 from app.company_documents;
update app.company_documents v set current_revision_id=r.id
 from app.company_document_revisions r where r.document_id=v.id;

alter table app.resume_versions alter column current_revision_id set not null;
alter table app.work_logs alter column current_revision_id set not null;
alter table app.company_documents alter column current_revision_id set not null;
alter table app.resume_versions add constraint resume_current_revision_fk
 foreign key(current_revision_id,id,user_id)
 references app.resume_revisions(id,resume_version_id,workspace_id) deferrable initially deferred;
alter table app.work_logs add constraint work_log_current_revision_fk
 foreign key(current_revision_id,id,user_id)
 references app.work_log_revisions(id,work_log_id,workspace_id) deferrable initially deferred;
alter table app.company_documents add constraint company_document_current_revision_fk
 foreign key(current_revision_id,id,user_id)
 references app.company_document_revisions(id,document_id,workspace_id)
 deferrable initially deferred;

-- The parent row lock serializes revision numbers; callers cannot pick another source's revision.
create function app.select_source_revision() returns trigger language plpgsql as $$
declare changed boolean;
begin
 if tg_op='INSERT' then
  new.current_revision_id:=gen_random_uuid();
  return new;
 end if;
 if tg_table_name='resume_versions' then
  changed := row(new.content_sha256,new.source,new.document_id,
                 new.file_s3_key,new.file_content_type,new.file_sha256)
    is distinct from row(old.content_sha256,old.source,old.document_id,
                         old.file_s3_key,old.file_content_type,old.file_sha256);
 elsif tg_table_name='work_logs' then
  changed := row(new.content_sha256,new.day)
    is distinct from row(old.content_sha256,old.day);
 else
  changed := row(new.sha256,new.s3_key,new.name_enc,new.key_version,new.content_type,new.size_bytes)
    is distinct from row(old.sha256,old.s3_key,old.name_enc,old.key_version,old.content_type,
                         old.size_bytes);
 end if;
 new.current_revision_id := case when changed then gen_random_uuid() else old.current_revision_id end;
 return new;
end $$;

create function app.record_source_revision() returns trigger language plpgsql
 security definer set search_path=pg_catalog,app as $$
begin
 if tg_table_name='resume_versions' then
  insert into app.resume_revisions
   (id,resume_version_id,workspace_id,revision_number,content_sha256,source,document_id,text_enc,
    key_version,file_s3_key,file_name_enc,file_content_type,file_sha256)
   select new.current_revision_id,new.id,new.user_id,
    coalesce(max(revision_number),0)+1,new.content_sha256,new.source,new.document_id,new.text_enc,
    new.key_version,new.file_s3_key,new.file_name_enc,new.file_content_type,new.file_sha256
   from app.resume_revisions where resume_version_id=new.id
   on conflict(id) do nothing;
  if new.original_text_enc is not null and new.original_content_sha256=new.content_sha256 then
   insert into app.resume_revision_extractions(revision_id,workspace_id,text_enc,key_version)
    values(new.current_revision_id,new.user_id,new.original_text_enc,new.original_text_key_version)
    on conflict(revision_id) do nothing;
  end if;
 elsif tg_table_name='work_logs' then
  insert into app.work_log_revisions
   (id,work_log_id,workspace_id,employer_id,revision_number,content_sha256,text_enc,key_version,day)
   select new.current_revision_id,new.id,new.user_id,new.employer_id,
    coalesce(max(revision_number),0)+1,new.content_sha256,new.text_enc,new.key_version,new.day
   from app.work_log_revisions where work_log_id=new.id on conflict(id) do nothing;
 else
  insert into app.company_document_revisions
   (id,document_id,workspace_id,employer_id,revision_number,content_sha256,s3_key,name_enc,key_version,
    content_type,size_bytes)
   select new.current_revision_id,new.id,new.user_id,new.employer_id,
    coalesce(max(revision_number),0)+1,new.sha256,new.s3_key,new.name_enc,new.key_version,
    new.content_type,new.size_bytes
   from app.company_document_revisions where document_id=new.id on conflict(id) do nothing;
 end if;
 return null;
end $$;
revoke all on function app.record_source_revision() from public;
create trigger source_revision_select before insert or update on app.resume_versions
 for each row execute function app.select_source_revision();
create trigger source_revision_record after insert or update on app.resume_versions
 for each row execute function app.record_source_revision();
create trigger source_revision_select before insert or update on app.work_logs
 for each row execute function app.select_source_revision();
create trigger source_revision_record after insert or update on app.work_logs
 for each row execute function app.record_source_revision();
create trigger source_revision_select before insert or update on app.company_documents
 for each row execute function app.select_source_revision();
create trigger source_revision_record after insert or update on app.company_documents
 for each row execute function app.record_source_revision();

-- Enforce same-employer relationships as well as same-workspace ownership.
alter table app.work_logs add constraint work_logs_id_workspace_employer unique(id,user_id,employer_id);
alter table app.company_documents add constraint documents_id_workspace_employer unique(id,user_id,employer_id);
alter table app.company_documents add constraint document_log_employer_fk
 foreign key(work_log_id,user_id,employer_id) references app.work_logs(id,user_id,employer_id)
 on delete cascade;
alter table app.knowledge_chunks add constraint chunk_document_employer_fk
 foreign key(document_id,user_id,employer_id) references app.company_documents(id,user_id,employer_id)
 on delete cascade;
alter table app.knowledge_chunks add column document_revision_id uuid;
update app.knowledge_chunks k set document_revision_id=d.current_revision_id
 from app.company_documents d where d.id=k.document_id;
alter table app.knowledge_chunks alter column document_revision_id set not null;
alter table app.knowledge_chunks add constraint chunk_revision_fk
 foreign key(document_revision_id,document_id,user_id)
 references app.company_document_revisions(id,document_id,workspace_id) on delete cascade;
create function app.pin_chunk_revision() returns trigger language plpgsql as $$
begin
 if new.document_revision_id is null then
  select current_revision_id into new.document_revision_id
   from app.company_documents where id=new.document_id and user_id=new.user_id;
 end if;
 return new;
end $$;
create trigger chunk_revision before insert on app.knowledge_chunks
 for each row execute function app.pin_chunk_revision();

alter table app.sessions add column resume_revision_id uuid;
alter table app.sessions add column resume_content_sha256 text;
alter table app.sessions add constraint session_resume_revision_fk
 foreign key(resume_revision_id,user_id) references app.resume_revisions(id,workspace_id)
 on delete set null(resume_revision_id);
create function app.pin_session_resume() returns trigger language plpgsql as $$
declare selected uuid;
begin
 if new.resume_revision_id is not null then
  select content_sha256 into new.resume_content_sha256 from app.resume_revisions
   where id=new.resume_revision_id and workspace_id=new.user_id;
  if not found then raise exception 'resume revision unavailable' using errcode='23503'; end if;
  return new;
 end if;
 if new.resume_revision_id is null and new.mode='interview' then
  if new.job_id is not null then selected:=app.paired_resume(new.job_id);
  elsif new.interview_id is not null then
   select app.paired_resume(job_id) into selected from app.interviews where id=new.interview_id;
  else select id into selected from app.resume_versions where user_id=new.user_id and kind='base';
  end if;
  select current_revision_id,content_sha256 into new.resume_revision_id,new.resume_content_sha256
   from app.resume_versions where id=selected and user_id=new.user_id;
 end if;
 return new;
end $$;
create trigger session_resume_revision before insert on app.sessions
 for each row execute function app.pin_session_resume();

create function app.keep_session_resume() returns trigger language plpgsql as $$
begin
 if (new.resume_revision_id is distinct from old.resume_revision_id
     or new.resume_content_sha256 is distinct from old.resume_content_sha256)
    and pg_trigger_depth()=1 then
  raise exception 'session source revision is fixed at admission' using errcode='23514';
 end if;
 return new;
end $$;
create trigger session_resume_fixed before update of resume_revision_id,resume_content_sha256
 on app.sessions for each row execute function app.keep_session_resume();

create function app.keep_source_revision_immutable() returns trigger language plpgsql as $$
begin raise exception 'source revisions are immutable' using errcode='23514'; end $$;
do $$ declare t text; begin
 foreach t in array array['resume_revisions','resume_revision_extractions','work_log_revisions',
                         'company_document_revisions'] loop
  execute format('alter table app.%I enable row level security',t);
  execute format('alter table app.%I force row level security',t);
  execute format('create policy owner_revision on app.%I to app_owner using(true) with check(true)',t);
  execute format('create policy api_revision on app.%I to app_api using
   (workspace_id=current_setting(''app.user_id'',true)::uuid and app.has_workspace_access(workspace_id))',t);
  execute format('create policy worker_revision on app.%I to app_worker,app_gateway using
   (workspace_id=current_setting(''app.user_id'',true)::uuid)',t);
  execute format('grant select on app.%I to app_api,app_worker,app_system',t);
  execute format('create trigger immutable_revision before update on app.%I
   for each row execute function app.keep_source_revision_immutable()',t);
 end loop;
end $$;
grant select(id,resume_version_id,workspace_id,revision_number,content_sha256)
 on app.resume_revisions to app_gateway;
grant select(current_revision_id,content_sha256) on app.resume_versions to app_gateway;

alter table app.work_logs add column extracted_revision_id uuid;
update app.work_logs set extracted_revision_id=current_revision_id
 where extracted_sha256=content_sha256;
alter table app.work_logs add constraint work_log_extracted_revision_fk
 foreign key(extracted_revision_id,id,user_id)
 references app.work_log_revisions(id,work_log_id,workspace_id) deferrable initially deferred;

alter table app.resume_versions force row level security;
alter table app.work_logs force row level security;
alter table app.company_documents force row level security;
alter table app.knowledge_chunks force row level security;
alter table app.sessions force row level security;
do $$ declare r record; begin
 for r in select oid::regclass as tab from pg_class
  where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
  execute format('alter table %s force row level security',r.tab);
 end loop;
end $$;
