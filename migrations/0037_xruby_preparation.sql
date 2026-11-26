-- Confirmed XRuby preparation is independent of retryable pack.build operations.
set local role app_owner;
create table app.xruby_preparation_requests (
 id uuid primary key,
 account_id uuid not null,
 profile_id uuid not null,
 interview_id uuid not null,
 grant_id text not null,
 grant_ref text not null check(grant_ref ~ '^xruby-oauth:access:[a-f0-9]{64}$'),
 grant_hash text not null check(grant_hash ~ '^[a-f0-9]{64}$'),
 source_hash text not null check(source_hash ~ '^[a-f0-9]{64}$'),
 request_hash text not null check(request_hash ~ '^[a-f0-9]{64}$'),
 key_hash text not null check(key_hash ~ '^[a-f0-9]{64}$'),
 state text not null default 'queued' check(state in ('queued','leased','dispatched','succeeded','rejected','uncertain')),
 lease_owner uuid,
 lease_generation bigint not null default 0 check(lease_generation>=0),
 lease_expires_at timestamptz,
 grant_expires_at timestamptz not null,
 dispatched_at timestamptz,
 finished_at timestamptz,
 revoked_at timestamptz,
 result_enc bytea,
 key_version integer,
 usage jsonb not null default '{}' check(jsonb_typeof(usage)='object'),
 created_at timestamptz not null default now(),
 unique(account_id,key_hash),
 check((result_enc is null and key_version is null) or (state='succeeded' and key_version is not null)),
 check(dispatched_at is null or state in ('dispatched','succeeded','uncertain'))
);
create index xruby_preparation_runnable on app.xruby_preparation_requests(created_at,id)
 where state in ('queued','leased');
create index xruby_preparation_dispatched on app.xruby_preparation_requests(lease_expires_at,id)
 where state='dispatched';
create index xruby_preparation_grant on app.xruby_preparation_requests(account_id,grant_id);
create table app.xruby_preparation_budgets (
 account_id uuid not null, day date not null, reservations integer not null check(reservations>0),
 primary key(account_id,day)
);
alter table app.xruby_preparation_requests enable row level security;
alter table app.xruby_preparation_requests force row level security;
alter table app.xruby_preparation_budgets enable row level security;
alter table app.xruby_preparation_budgets force row level security;
create policy xruby_preparation_account on app.xruby_preparation_requests to app_api
 using(account_id=nullif(current_setting('app.account_id',true),'')::uuid)
 with check(account_id=nullif(current_setting('app.account_id',true),'')::uuid);
create policy xruby_preparation_owner on app.xruby_preparation_requests to app_owner using(true) with check(true);
create policy xruby_budget_account on app.xruby_preparation_budgets to app_api
 using(account_id=nullif(current_setting('app.account_id',true),'')::uuid)
 with check(account_id=nullif(current_setting('app.account_id',true),'')::uuid);
create policy xruby_budget_owner on app.xruby_preparation_budgets to app_owner using(true) with check(true);
revoke all on app.xruby_preparation_requests,app.xruby_preparation_budgets from public,app_worker,app_system,app_gateway;
grant select,insert,update on app.xruby_preparation_requests,app.xruby_preparation_budgets to app_api;
create function app.xruby_preparation_immutable() returns trigger language plpgsql
 set search_path=pg_catalog,app as $$
begin
 if row(new.id,new.account_id,new.profile_id,new.interview_id,new.grant_id,new.grant_ref,
        new.grant_hash,new.source_hash,new.request_hash,new.key_hash,new.grant_expires_at,new.created_at)
 is distinct from row(old.id,old.account_id,old.profile_id,old.interview_id,old.grant_id,old.grant_ref,
        old.grant_hash,old.source_hash,old.request_hash,old.key_hash,old.grant_expires_at,old.created_at)
 or new.lease_generation<old.lease_generation
 or (old.dispatched_at is not null and new.dispatched_at is distinct from old.dispatched_at)
 or (old.state in ('succeeded','uncertain','rejected') and new.state<>old.state) then
  raise exception 'immutable XRuby dispatch receipt';
 end if;
 return new;
end $$;
create trigger xruby_preparation_immutable before update on app.xruby_preparation_requests
 for each row execute function app.xruby_preparation_immutable();
revoke all on function app.xruby_preparation_immutable() from public;
-- Trusted API executor only. Returns authority identifiers, never source/result/token bodies.
create function app.claim_xruby_preparation(owner_id uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,app as $$
declare r app.xruby_preparation_requests%rowtype;
begin
 if owner_id is null then raise exception 'lease owner required'; end if;
 with expired as (select id from app.xruby_preparation_requests
  where state='dispatched' and lease_expires_at<clock_timestamp()
  order by lease_expires_at,id limit 20 for update skip locked)
 update app.xruby_preparation_requests p set state='uncertain',finished_at=clock_timestamp()
 from expired where p.id=expired.id;
 select * into r from app.xruby_preparation_requests
 where (state='queued' or (state='leased' and lease_expires_at<clock_timestamp()))
 order by created_at,id limit 1 for update skip locked;
 if not found then return null; end if;
 update app.xruby_preparation_requests set state='leased',lease_owner=owner_id,
 lease_generation=lease_generation+1,lease_expires_at=clock_timestamp()+interval '90 seconds'
 where id=r.id returning * into r;
 return jsonb_build_object('id',r.id,'account_id',r.account_id,'profile_id',r.profile_id,
   'grant_ref',r.grant_ref,'grant_hash',r.grant_hash,'grant_id',r.grant_id,
   'interview_id',r.interview_id,'source_hash',r.source_hash,
   'lease_owner',r.lease_owner,'lease_generation',r.lease_generation);
end $$;
revoke all on function app.claim_xruby_preparation(uuid) from public,app_worker,app_system,app_gateway;
grant execute on function app.claim_xruby_preparation(uuid) to app_api;

-- Source locking is the only elevated source capability; returns no content and grants no UPDATE.
create function app.lock_xruby_preparation_resume(version_id uuid, revision_id uuid) returns boolean
 language plpgsql security definer set search_path=pg_catalog,app as $$
begin
 perform id from app.resume_versions where id=version_id and current_revision_id=revision_id
  and user_id=nullif(current_setting('app.user_id',true),'')::uuid
  and app.has_workspace_write(user_id) for share;
 if not found then return false; end if;
 perform id from app.resume_revisions where id=revision_id and resume_version_id=version_id for share;
 if not found then return false; end if;
 perform x.revision_id from app.resume_revision_extractions x where x.revision_id=lock_xruby_preparation_resume.revision_id for share;
 return true;
end $$;
revoke all on function app.lock_xruby_preparation_resume(uuid,uuid) from public,app_worker,app_system,app_gateway;
grant execute on function app.lock_xruby_preparation_resume(uuid,uuid) to app_api;

-- The actual provider payer has an independent platform-wide ceiling.
create table app.xruby_preparation_global_budgets (
 day date primary key, reservations integer not null check(reservations>0)
);
alter table app.xruby_preparation_global_budgets enable row level security;
alter table app.xruby_preparation_global_budgets force row level security;
create policy xruby_global_budget_owner on app.xruby_preparation_global_budgets
 to app_owner using(true) with check(true);
revoke all on app.xruby_preparation_global_budgets from public,app_api,app_worker,app_system,app_gateway;
create function app.reserve_xruby_preparation_budget(aid uuid, account_limit integer, global_limit integer)
 returns boolean language plpgsql security definer set search_path=pg_catalog,app as $$
declare d date := (clock_timestamp() at time zone 'UTC')::date;
begin
 if aid is null or account_limit is null or global_limit is null
 or aid is distinct from nullif(current_setting('app.account_id',true),'')::uuid
 or account_limit not between 1 and 20 or global_limit not between 1 and 10000
 or not exists(select 1 from app.accounts where id=aid and status='active') then
  raise exception 'invalid preparation payer reservation';
 end if;
 perform pg_advisory_xact_lock(hashtextextended('xruby-prepare-global-budget',0));
 if coalesce((select reservations from app.xruby_preparation_global_budgets where day=d),0)>=global_limit
 or coalesce((select reservations from app.xruby_preparation_budgets where account_id=aid and day=d),0)>=account_limit
 then return false; end if;
 insert into app.xruby_preparation_global_budgets(day,reservations) values(d,1)
 on conflict(day) do update set reservations=app.xruby_preparation_global_budgets.reservations+1;
 insert into app.xruby_preparation_budgets(account_id,day,reservations) values(aid,d,1)
 on conflict(account_id,day) do update set reservations=app.xruby_preparation_budgets.reservations+1;
 return true;
end $$;
revoke all on function app.reserve_xruby_preparation_budget(uuid,integer,integer)
 from public,app_worker,app_system,app_gateway;
grant execute on function app.reserve_xruby_preparation_budget(uuid,integer,integer) to app_api;
