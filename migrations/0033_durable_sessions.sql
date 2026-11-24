-- Durable session admission and writer ownership. Valkey retains the payload transport;
-- these content-free receipts authorize which retained batches workers may commit.
set local role app_owner;
-- Backfills and FK validation must see every existing workspace. This migration
-- is atomic and restores FORCE before commit; runtime callers remain scoped.
do $$ declare r record; begin
  for r in select oid::regclass as tab from pg_class
    where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
    execute format('alter table %s no force row level security',r.tab);
  end loop;
end $$;

alter table app.sessions
  add column workspace_id uuid generated always as (user_id) stored,
  add column created_by_account_id uuid references app.accounts(id) on delete set null,
  add column payer_account_id uuid references app.accounts(id) on delete set null,
  add column writer_generation bigint not null default 0 check(writer_generation >= 0),
  add column writer_id uuid,
  add column writer_expires_at timestamptz,
  add column admission_state text not null default 'legacy'
    check(admission_state in ('legacy','active','closing','ended','failed')),
  add column billing_price_version text,
  add column billing_currency text,
  add column billing_rate_minor_per_hour integer check(billing_rate_minor_per_hour >= 0);

create table app.session_writer_claims (
  session_id uuid not null references app.sessions(id) on delete cascade,
  generation bigint not null check(generation > 0),
  writer_id uuid not null,
  actor_account_id uuid references app.accounts(id) on delete set null,
  claimed_at timestamptz not null default clock_timestamp(),
  primary key(session_id,generation),
  unique(session_id,writer_id)
);
create table app.session_write_batches (
  session_id uuid not null,
  generation bigint not null,
  batch_id uuid not null,
  entry_hashes text[] not null,
  accepted_at timestamptz not null default clock_timestamp(),
  primary key(session_id,batch_id),
  foreign key(session_id,generation)
    references app.session_writer_claims(session_id,generation) on delete cascade
);
create index session_write_batches_generation on app.session_write_batches(session_id,generation);
alter table app.session_writer_claims enable row level security;
alter table app.session_writer_claims force row level security;
alter table app.session_write_batches enable row level security;
alter table app.session_write_batches force row level security;
create policy session_parent on app.session_writer_claims using (
  exists(select 1 from app.sessions s where s.id=session_id)
) with check(exists(select 1 from app.sessions s where s.id=session_id));
create policy session_parent on app.session_write_batches using (
  exists(select 1 from app.sessions s where s.id=session_id)
) with check(exists(select 1 from app.sessions s where s.id=session_id));
grant select on app.session_writer_claims,app.session_write_batches to app_worker,app_system;

-- Definer functions retain FORCE RLS and additionally require the authenticated actor
-- and selected workspace established by server transaction context.
create function app.session_admit(sid uuid, wid uuid, actor uuid, meta jsonb)
returns void language plpgsql security definer set search_path=pg_catalog,app as $$
declare selected uuid; revision uuid; sha text;
begin
  if actor is distinct from nullif(current_setting('app.account_id',true),'')::uuid
     or wid is distinct from nullif(current_setting('app.user_id',true),'')::uuid
     or not app.has_workspace_write(wid) then
    raise exception 'session admission denied' using errcode='42501';
  end if;
  if meta->>'mode'='interview' then
    if nullif(meta->>'job_id','') is not null then
      selected:=app.paired_resume((meta->>'job_id')::uuid);
    elsif nullif(meta->>'interview_id','') is not null then
      select app.paired_resume(job_id) into selected from app.interviews
        where id=(meta->>'interview_id')::uuid and user_id=wid;
    else
      select id into selected from app.resume_versions where user_id=wid and kind='base';
    end if;
    if selected is not null then
      select current_revision_id,content_sha256 into revision,sha
        from app.resume_versions where id=selected and user_id=wid for share;
      if revision is distinct from nullif(meta->>'resume_revision_id','')::uuid
        or sha is distinct from nullif(meta->>'resume_content_sha256','') then
        raise exception 'resume context is stale or unprepared' using errcode='55000';
      end if;
    end if;
  end if;
  insert into app.sessions(id,user_id,created_by_account_id,payer_account_id,mode,
    job_id,interview_id,platform,title,store_transcript,started_at,employer_id,
    spoken_language,metering_version,billing_price_version,billing_currency,
    billing_rate_minor_per_hour,admission_state,resume_revision_id,resume_content_sha256)
  values(sid,wid,actor,actor,meta->>'mode',
    (select id from app.jobs where id=nullif(meta->>'job_id','')::uuid and user_id=wid),
    coalesce(
      (select id from app.interviews where id=nullif(meta->>'interview_id','')::uuid and user_id=wid),
      -- A job-only start may load the prepared pack of its upcoming interview.
      -- Preserve that association when admitting the parent, before worker flush.
      (select id from app.interviews where pack_id=nullif(meta->>'pack_id','')::uuid
        and user_id=wid and job_id=nullif(meta->>'job_id','')::uuid order by id limit 1)),
    nullif(meta->>'platform',''),nullif(meta->>'title',''),
    coalesce(meta->>'store_transcript','0')='1',(meta->>'started_at')::timestamptz,
    (select id from app.employers where id=nullif(meta->>'employer_id','')::uuid and user_id=wid),
    nullif(meta->>'spoken_language',''),meta->>'metering_version',
    meta->>'billing_price_version',meta->>'billing_currency',
    (meta->>'billing_rate_minor_per_hour')::integer,'active',
    nullif(meta->>'resume_revision_id','')::uuid,nullif(meta->>'resume_content_sha256',''))
  on conflict(id) do nothing;
  if not exists(select 1 from app.sessions where id=sid and user_id=wid
    and created_by_account_id=actor and payer_account_id=actor) then
    raise exception 'session admission identity conflict' using errcode='42501';
  end if;
  -- Preserve authenticated attribution before account deletion or cache loss.
  -- These content-free rows are independent of the content parent by design.
  if meta->>'metering_version'='connected-v1' then
    insert into app.usage_prices(version,currency,rate_minor_per_hour)
      select billing_price_version,billing_currency,billing_rate_minor_per_hour
      from app.sessions where id=sid on conflict(version) do nothing;
    insert into app.live_usage_sessions(session_id,workspace_id,payer_account_id,
      currency,rate_minor_per_hour,price_version)
      select id,user_id,payer_account_id,billing_currency,billing_rate_minor_per_hour,
        billing_price_version from app.sessions where id=sid on conflict(session_id) do nothing;
  end if;
end $$;

create function app.session_claim(sid uuid, actor uuid, wid uuid, writer uuid, ttl_seconds integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,app as $$
declare s app.sessions; prior bigint;
begin
  if actor is distinct from nullif(current_setting('app.account_id',true),'')::uuid
     or wid is distinct from nullif(current_setting('app.user_id',true),'')::uuid
     or not app.has_workspace_write(wid) then
    raise exception 'session claim denied' using errcode='42501';
  end if;
  select * into s from app.sessions where id=sid and user_id=wid for update;
  if not found or s.ended_at is not null or s.admission_state in ('closing','ended','failed')
     or (s.payer_account_id is not null and s.payer_account_id<>actor)
     or (s.private_context_account_id is not null and s.private_context_account_id<>actor) then
    raise exception 'session unavailable' using errcode='42501';
  end if;
  select generation into prior from app.session_writer_claims where session_id=sid and writer_id=writer;
  if prior is not null and (prior<>s.writer_generation or s.writer_id<>writer) then
    raise exception 'stale session claim' using errcode='40001';
  end if;
  if prior is null then
    s.writer_generation := s.writer_generation+1;
    insert into app.session_writer_claims(session_id,generation,writer_id,actor_account_id)
      values(sid,s.writer_generation,writer,actor);
  end if;
  update app.sessions set writer_generation=s.writer_generation,writer_id=writer,
    writer_expires_at=clock_timestamp()+make_interval(secs=>greatest(1,least(ttl_seconds,300))),
    admission_state='active' where id=sid;
  return jsonb_build_object('writer_generation',s.writer_generation,
    'private_context_account_id',s.private_context_account_id,
    'payer_account_id',s.payer_account_id,'metering_version',s.metering_version,
    'billing_price_version',s.billing_price_version,'billing_currency',s.billing_currency,
    'billing_rate_minor_per_hour',s.billing_rate_minor_per_hour,
    'store_transcript',case when s.store_transcript then '1' else '0' end,
    'resume_revision_id',s.resume_revision_id,'resume_content_sha256',s.resume_content_sha256);
end $$;

create function app.session_writer_guard(sid uuid, generation bigint, writer uuid,
  ttl_seconds integer, batch uuid default null, hashes text[] default null)
returns void language plpgsql security definer set search_path=pg_catalog,app as $$
declare s app.sessions; existing text[];
begin
  select * into s from app.sessions where id=sid for update;
  if not found or s.writer_generation<>generation or s.writer_id is distinct from writer
     or s.ended_at is not null or s.admission_state not in ('active','closing')
     or s.payer_account_id is distinct from nullif(current_setting('app.account_id',true),'')::uuid
     or s.user_id is distinct from nullif(current_setting('app.user_id',true),'')::uuid
     or not app.has_workspace_write(s.user_id) then
    raise exception 'stale session writer' using errcode='40001';
  end if;
  update app.sessions set writer_expires_at=clock_timestamp()+
    make_interval(secs=>greatest(1,least(ttl_seconds,300))) where id=sid;
  if batch is not null then
    if hashes is null or cardinality(hashes)>200 then
      raise exception 'invalid session batch';
    end if;
    insert into app.session_write_batches(session_id,generation,batch_id,entry_hashes)
      values(sid,generation,batch,hashes) on conflict(session_id,batch_id) do nothing;
    select entry_hashes into existing from app.session_write_batches where session_id=sid and batch_id=batch;
    if existing is distinct from hashes then raise exception 'session batch identity conflict'; end if;
  end if;
end $$;

create function app.session_writer_close(sid uuid,generation bigint,writer uuid,reason text)
returns boolean language plpgsql security definer set search_path=pg_catalog,app as $$
declare wid uuid; v_operation_key text; message jsonb;
begin
  perform app.session_writer_guard(sid,generation,writer,30);
  update app.sessions set admission_state=case when reason in ('abandoned','admission_failed')
    then 'failed' else 'closing' end where id=sid returning user_id into wid;
  v_operation_key:='session.ended:'||sid||':g'||generation;
  message:=jsonb_build_object('version',1,'type','session.ended','idempotency_key',v_operation_key,
    'user_id',wid,'payload',jsonb_build_object('session_id',sid,'writer_generation',generation),
    'enqueued_at',clock_timestamp());
  insert into app.operations(idempotency_key,job_type,user_id,workspace_id,account_id,
    status,lane,envelope) values(v_operation_key,'session.ended',wid,wid,
    nullif(current_setting('app.account_id',true),'')::uuid,'queued','persistence',message)
    on conflict(idempotency_key) do nothing;
  insert into app.outbox_events(operation_key,user_id,message)
    select v_operation_key,wid,message from app.operations
    where idempotency_key=v_operation_key and status not in ('done','cancelled')
    on conflict(operation_key) do nothing;
  return true;
end $$;
revoke all on function app.session_admit(uuid,uuid,uuid,jsonb),
  app.session_claim(uuid,uuid,uuid,uuid,integer),
  app.session_writer_guard(uuid,bigint,uuid,integer,uuid,text[]),
  app.session_writer_close(uuid,bigint,uuid,text) from public;
grant execute on function app.session_admit(uuid,uuid,uuid,jsonb),
  app.session_claim(uuid,uuid,uuid,uuid,integer),
  app.session_writer_guard(uuid,bigint,uuid,integer,uuid,text[]),
  app.session_writer_close(uuid,bigint,uuid,text) to app_gateway;

-- A direct session relationship removes question-to-session traversal from common reads.
alter table app.questions add constraint questions_session_identity unique(id,session_id,user_id);
alter table app.answers add column session_id uuid;
update app.answers a set session_id=q.session_id from app.questions q where q.id=a.question_id;
alter table app.answers alter column session_id set not null;
alter table app.answers add constraint answers_session_question foreign key(question_id,session_id,user_id)
  references app.questions(id,session_id,user_id) on delete cascade;
create index answers_session on app.answers(session_id,created_at,id);
create function app.answer_session_identity() returns trigger language plpgsql
set search_path=pg_catalog,app as $$ begin
  if new.session_id is null then
    select session_id into new.session_id from app.questions where id=new.question_id and user_id=new.user_id;
  end if;
  return new;
end $$;
create trigger answer_session_identity before insert on app.answers
for each row execute function app.answer_session_identity();

-- Existing orphan guest/companion rows cannot be guessed into historical sessions. Keep
-- them visible for migration review; all new references must have a durable parent.
alter table app.session_seats add constraint session_seats_parent
  foreign key(session_id,user_id) references app.sessions(id,user_id) on delete cascade not valid;
alter table app.companion_attachments add constraint companion_attachments_parent
  foreign key(session_id,profile_id) references app.sessions(id,user_id) on delete cascade not valid;

-- Parent visibility is authoritative for all API history reads. A shared call may
-- become private while it is active; descendants inherit that transition immediately.
create policy session_membership on app.sessions as restrictive to app_api,app_gateway
  using(app.has_workspace_access(user_id)) with check(app.has_workspace_write(user_id));
create policy session_delete_membership on app.sessions as restrictive for delete to app_api
  using(app.has_workspace_write(user_id));
create policy session_privacy on app.sessions as restrictive to app_api
  using(private_context_account_id is null or
        private_context_account_id=nullif(current_setting('app.account_id',true),'')::uuid)
  with check(private_context_account_id is null or
        private_context_account_id=nullif(current_setting('app.account_id',true),'')::uuid);
do $$ declare t text; begin
  foreach t in array array['transcript_segments','questions','answers','session_outputs'] loop
    execute format('create policy session_parent_privacy on app.%I as restrictive to app_api '
      'using(exists(select 1 from app.sessions s where s.id=session_id)) '
      'with check(exists(select 1 from app.sessions s where s.id=session_id))',t);
  end loop;
end $$;

do $$ declare r record; begin
  for r in select oid::regclass as tab from pg_class
    where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
    execute format('alter table %s force row level security',r.tab);
  end loop;
end $$;
