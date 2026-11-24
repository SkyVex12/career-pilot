-- Durable accepted work, transactional publication, and renewable worker fencing.
set local role app_owner;

alter table app.job_runs rename to operations;
alter table app.operations drop constraint job_runs_status_check;
alter table app.operations
  add column id uuid not null default gen_random_uuid() unique,
  add column account_id uuid,
  add column workspace_id uuid,
  add column lane text not null default 'ingestion'
    check (lane in ('persistence','interactive','ingestion','maintenance')),
  add column transport text not null default 'queue' check (transport in ('queue','local')),
  add column envelope jsonb,
  add column input_revision text,
  add column available_at timestamptz not null default now(),
  add column lease_owner uuid,
  add column lease_generation bigint not null default 0 check (lease_generation >= 0),
  add column lease_expires_at timestamptz,
  add column created_at timestamptz not null default now(),
  add constraint operations_status_check
    check (status in ('queued','running','done','failed','cancelled','dead'));
update app.operations set workspace_id=user_id;
update app.operations set account_id=user_id,workspace_id=null where job_type='user.delete';
alter table app.operations alter column attempts set default 0;
create index operations_runnable on app.operations(lane,available_at,created_at)
  where status in ('queued','failed');
create index operations_expired on app.operations(lease_expires_at)
  where status='running';
create index operations_scope on app.operations(user_id,created_at desc);
create index operations_pack_context on app.operations
  (user_id,(envelope->'payload'->>'context'),created_at desc) where job_type='pack.build';
create index operations_worklog_source on app.operations
  (user_id,(envelope->'payload'->>'work_log_id'),
   (envelope->'payload'->>'work_log_revision_id')) where job_type='worklog.extract';
create index operations_flush_retention on app.operations(finished_at,idempotency_key)
  where job_type='session.flush' and status in ('done','cancelled');

-- Old diagnostics and read contracts still work; execution uses operations directly.
create view app.job_runs with (security_invoker=true) as
  select idempotency_key,job_type,user_id,status,attempts,
  started_at,finished_at,error from app.operations;
grant select,insert,update,delete on app.job_runs to app_worker,app_system;
alter table app.operations enable row level security;
alter table app.operations force row level security;
create policy operations_scope on app.operations to app_api
  using (
    ((transport='local' or job_type='user.delete')
      and account_id=nullif(current_setting('app.account_id',true),'')::uuid
      and user_id=account_id)
    or (transport='queue' and job_type<>'user.delete'
      and user_id=nullif(current_setting('app.user_id',true),'')::uuid
      and app.has_workspace_access(user_id)))
  with check (
    ((transport='local' or job_type='user.delete')
      and account_id=nullif(current_setting('app.account_id',true),'')::uuid
      and user_id=account_id
      and (job_type='user.delete' or app.has_workspace_access(workspace_id)))
    or (transport='queue' and job_type<>'user.delete'
      and user_id=nullif(current_setting('app.user_id',true),'')::uuid
      and app.has_workspace_write(user_id)));
create policy operations_workers on app.operations to app_worker,app_owner
  using (true) with check (true);
grant select,insert,update on app.operations to app_api,app_worker,app_system;
revoke delete on app.operations from app_worker;
grant delete on app.operations to app_system;

create table app.outbox_events (
  id uuid primary key default gen_random_uuid(),
  operation_key text not null unique references app.operations(idempotency_key) on delete cascade,
  user_id uuid,
  message jsonb not null,
  available_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  published_at timestamptz,
  lease_owner uuid,
  lease_expires_at timestamptz,
  attempts int not null default 0,
  last_error text
);
create index outbox_pending on app.outbox_events(available_at,created_at)
  where published_at is null;
create index outbox_recovery on app.outbox_events(published_at)
  where published_at is not null;
alter table app.outbox_events enable row level security;
alter table app.outbox_events force row level security;
create policy outbox_scope on app.outbox_events to app_api
  using (exists(select 1 from app.operations o where o.idempotency_key=operation_key))
  with check (exists(select 1 from app.operations o where o.idempotency_key=operation_key
    and (o.job_type='user.delete' or app.has_workspace_write(o.workspace_id))));
create policy outbox_workers on app.outbox_events to app_worker,app_owner
  using (true) with check (true);
grant select,insert,update on app.outbox_events to app_api,app_worker,app_system;
grant delete on app.outbox_events to app_system;

create table app.scheduled_jobs (
  name text not null,
  slot text not null,
  created_at timestamptz not null default now(),
  primary key(name,slot)
);
grant select,insert,delete on app.scheduled_jobs to app_worker,app_system;

-- A local interactive runner needs to discover work across accounts. This is its
-- only cross-scope claim API: it cannot claim queued ingestion or maintenance.
create function app.claim_next_interactive(p_owner uuid,p_lease_s double precision)
returns setof app.operations language plpgsql security definer
set search_path=pg_catalog,app as $$
begin
  if p_lease_s <= 0 or p_lease_s > 3600 then raise exception 'invalid lease duration'; end if;
  return query
  with next as (
    select idempotency_key from app.operations
    where lane='interactive' and transport='local'
      and job_type in ('ask.generate','practice.generate')
      and available_at <= clock_timestamp()
      and (status in ('queued','failed')
        or (status='running' and lease_expires_at <= clock_timestamp()))
    order by available_at,created_at for update skip locked limit 1
  )
  update app.operations o set status='running',attempts=o.attempts+1,
    started_at=clock_timestamp(),finished_at=null,error=null,lease_owner=p_owner,
    lease_generation=o.lease_generation+1,
    lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_s)
  from next where o.idempotency_key=next.idempotency_key returning o.*;
end $$;
revoke all on function app.claim_next_interactive(uuid,double precision) from public;
grant execute on function app.claim_next_interactive(uuid,double precision) to app_api,app_worker,app_system;

-- Lease ownership is a capability held only by the runner. Locking this row until
-- result commit serializes takeover with every guarded database transaction.
create function app.guard_operation(p_key text,p_owner uuid,p_generation bigint)
returns boolean language plpgsql security definer
set search_path=pg_catalog,app as $$
declare found_key text;
begin
  select idempotency_key into found_key from app.operations
    where idempotency_key=p_key and lease_owner=p_owner and lease_generation=p_generation
      and status='running' and lease_expires_at>clock_timestamp() for update;
  return found_key is not null;
end $$;
revoke all on function app.guard_operation(text,uuid,bigint) from public;
grant execute on function app.guard_operation(text,uuid,bigint) to app_api,app_worker,app_system;

create function app.renew_operation(p_key text,p_owner uuid,p_generation bigint,p_lease_s double precision)
returns boolean language plpgsql security definer
set search_path=pg_catalog,app as $$
declare changed int;
begin
  if p_lease_s <= 0 or p_lease_s > 3600 then raise exception 'invalid lease duration'; end if;
  update app.operations set lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_s)
    where idempotency_key=p_key and lease_owner=p_owner and lease_generation=p_generation
      and status='running' and lease_expires_at>clock_timestamp();
  get diagnostics changed=row_count;
  return changed=1;
end $$;
revoke all on function app.renew_operation(text,uuid,bigint,double precision) from public;
grant execute on function app.renew_operation(text,uuid,bigint,double precision) to app_api,app_worker,app_system;

create function app.settle_operation(p_key text,p_owner uuid,p_generation bigint,
  p_status text,p_error text,p_delay_s double precision)
returns boolean language plpgsql security definer
set search_path=pg_catalog,app as $$
declare changed int;
begin
  if p_status not in ('done','failed','cancelled') or p_delay_s<0 then
    raise exception 'invalid settlement';
  end if;
  update app.operations set status=case when p_status='failed' and attempts>=5
      and lane in ('interactive','ingestion') then 'dead' else p_status end,
      finished_at=clock_timestamp(),
      error=left(p_error,2000),available_at=clock_timestamp()+make_interval(secs=>p_delay_s),
      lease_expires_at=null
    where idempotency_key=p_key and lease_owner=p_owner and lease_generation=p_generation
      and status='running' and lease_expires_at>clock_timestamp();
  get diagnostics changed=row_count;
  return changed=1;
end $$;
revoke all on function app.settle_operation(text,uuid,bigint,text,text,double precision) from public;
grant execute on function app.settle_operation(text,uuid,bigint,text,text,double precision)
  to app_api,app_worker,app_system;
