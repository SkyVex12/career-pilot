-- Transactional invalidation, including workers, cascades and administrative writes.
-- A scope counter is a row, NOT a sequence: its lock is held until commit. Readers can
-- never advance past an uncommitted lower revision and miss it on their next sync.
set local role app_owner;

create table app.data_revisions (
  user_id uuid not null references app.users(id) on delete cascade,
  domain text not null,
  revision bigint not null default 0,
  primary key (user_id, domain)
);
-- Latest change per resource, including tombstones. This is a compact change index,
-- not a copy of the content or an unbounded event log. Repeated partial writes replace
-- one entry. Tombstones older than the sync horizon are removed on the next write.
create table app.data_changes (
  user_id uuid not null references app.users(id) on delete cascade,
  domain text not null,
  resource text not null,
  resource_id text not null,
  revision bigint not null,
  deleted boolean not null,
  changed_at timestamptz not null default clock_timestamp(),
  private_account_id uuid,
  primary key (user_id, domain, resource, resource_id)
);
create index data_changes_scan on app.data_changes(user_id, domain, revision);
do $$ declare t text; begin
  foreach t in array array['data_revisions','data_changes'] loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);
    execute format('create policy tenant_isolation on app.%I using '
      '(user_id=current_setting(''app.user_id'')::uuid) with check '
      '(user_id=current_setting(''app.user_id'')::uuid)', t);
  end loop;
end $$;
grant select, insert, update, delete on app.data_revisions, app.data_changes
  to app_api, app_worker, app_system;

create function app.record_data_change(uid uuid, area text, kind text, rid text,
                                      removed boolean, private_id uuid default null)
returns void language plpgsql security invoker set search_path=pg_catalog,app as $$
declare rev bigint;
begin
  -- A cascading account deletion must not re-create metadata for the deleted user.
  if not exists(select 1 from app.users where id=uid) then return; end if;
  -- Every domain of a workspace acquires the SAME clock first. Separate counters
  -- would deadlock when a post-call transaction touches history then work while a
  -- memory edit touches work then history (replay labels depend on memory).
  insert into app.data_revisions(user_id,domain,revision) values(uid,'*',1)
    on conflict(user_id,domain) do update
      set revision=app.data_revisions.revision+1 returning revision into rev;
  insert into app.data_revisions(user_id,domain,revision) values(uid,area,rev)
    on conflict(user_id,domain) do update set revision=excluded.revision;
  insert into app.data_changes(user_id,domain,resource,resource_id,revision,deleted,
                               private_account_id)
    values(uid,area,kind,rid,rev,removed,private_id)
    on conflict(user_id,domain,resource,resource_id) do update set
      revision=excluded.revision, deleted=excluded.deleted,
      private_account_id=excluded.private_account_id, changed_at=clock_timestamp();
  -- Bound tombstone retention; clients older than 30 days must bootstrap again.
  if rev % 256 = 0 then
    delete from app.data_changes where user_id=uid and deleted
      and changed_at < clock_timestamp()-interval '31 days';
  end if;
end $$;

create function app.track_data_change() returns trigger language plpgsql
security invoker set search_path=pg_catalog,app as $$
declare data jsonb; uid uuid; rid text; sid uuid; private_id uuid; kind text;
        parent_ended timestamptz;
begin
  data := case when tg_op='DELETE' then to_jsonb(old) else to_jsonb(new) end;
  uid := (data->>'user_id')::uuid;
  rid := coalesce(data->>'id', data->>'session_id', '*');
  kind := tg_table_name;
  if tg_argv[0]='history' then
    if tg_table_name='sessions' then
      sid := (data->>'id')::uuid;
      private_id := (data->>'private_context_account_id')::uuid;
    elsif tg_table_name='answers' then
      select session_id into sid from app.questions where id=(data->>'question_id')::uuid;
    else
      sid := (data->>'session_id')::uuid;
    end if;
    if tg_table_name<>'sessions' then
      select private_context_account_id,ended_at into private_id,parent_ended
        from app.sessions where id=sid;
      -- Live content is explicitly no-store. The parent's closing revision publishes
      -- all its accumulated children; continuous transcript flushes need no cache I/O.
      if parent_ended is null then return null; end if;
    end if;
    -- Child deletions in a parent cascade need no independent tombstone: the parent
    -- trigger records it. Avoid turning a just-deleted parent back into an upsert.
    if tg_table_name<>'sessions' and not exists(select 1 from app.sessions where id=sid)
      then return null; end if;
    perform app.record_data_change(uid,'history','sessions',sid::text,
                                   tg_table_name='sessions' and tg_op='DELETE',private_id);
  else
    perform app.record_data_change(uid,tg_argv[0],kind,rid,tg_op='DELETE');
    -- Replay reference labels depend on memory titles, not only session rows.
    if tg_table_name='memory_nodes' then
      perform app.record_data_change(uid,'history','references','*',false);
    end if;
  end if;
  return null;
end $$;

do $$ declare t text; begin
  foreach t in array array['sessions','transcript_segments','questions','answers','session_outputs'] loop
    execute format('create trigger data_change after insert or update or delete on app.%I '
                   'for each row execute function app.track_data_change(''history'')',t);
  end loop;
  foreach t in array array['employers','work_logs','company_documents','memory_nodes',
                          'memory_proposals','knowledge_chunks'] loop
    execute format('create trigger data_change after insert or update or delete on app.%I '
                   'for each row execute function app.track_data_change(''work'')',t);
  end loop;
end $$;
create index sessions_user_page on app.sessions(user_id, started_at desc, id desc);
