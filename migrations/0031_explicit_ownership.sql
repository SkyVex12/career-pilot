-- Keep UUIDs/encryption contexts stable while making sign-in principals and data
-- workspaces distinct. Legacy SQL names are compatibility views, not authorities.
set local role app_owner;
-- Foreign-key validation must see every legacy tenant while references move.
-- This entire migration is one transaction; FORCE is restored before commit.
do $$ declare r record; begin
  for r in select oid::regclass as tab from pg_class
    where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
    execute format('alter table %s no force row level security',r.tab);
  end loop;
end $$;
alter table app.users rename to workspaces;
alter table app.workspaces no force row level security;
-- A retained workspace's historical display metadata must not reserve the email
-- address of its deleted creator. Only accounts owns sign-in email uniqueness.
alter table app.workspaces drop constraint users_email_key;

create table app.accounts (
  id uuid primary key,
  email text not null unique,
  display_name text not null,
  plan text not null default 'free',
  settings jsonb not null default '{}',
  external_ref jsonb not null default '{}',
  status text not null default 'active' check(status in ('active','deleting','disabled')),
  created_at timestamptz not null default now()
);
insert into app.accounts(id,email,display_name,plan,settings,external_ref,created_at)
  select id,email,display_name,plan,settings,external_ref,created_at
  from app.workspaces where owner_id is null;

-- IDs remain reserved after account deletion: a deterministic sign-in fallback
-- must never inherit retained workspaces or usage attributed to an earlier account.
create table app.account_tombstones (
  account_id uuid primary key,
  retired_at timestamptz not null default clock_timestamp()
);
alter table app.account_tombstones enable row level security;
alter table app.account_tombstones force row level security;
create policy account_scope on app.account_tombstones for select
  using(account_id=nullif(current_setting('app.account_id',true),'')::uuid);
create policy migration_owner on app.account_tombstones to app_owner using(true) with check(true);
grant select on app.account_tombstones to app_api,app_system;
create function app.guard_account_id_lifetime() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
begin
  if tg_op='DELETE' then
    insert into app.account_tombstones(account_id) values(old.id) on conflict do nothing;
    return old;
  end if;
  if exists(select 1 from app.account_tombstones where account_id=new.id) then
    raise exception 'retired account ID cannot be reused' using errcode='23514';
  end if;
  return new;
end $$;
create trigger account_id_lifetime before insert or delete on app.accounts
  for each row execute function app.guard_account_id_lifetime();

-- Historical private ciphertext used the personal workspace's key. Preserve the
-- wrapped bytes/version/AAD once, then maintain independent account key lifetime.
create table app.account_keys (
  account_id uuid not null references app.accounts(id) on delete cascade,
  version integer not null,
  encrypted_dek bytea not null,
  created_at timestamptz not null default now(),
  destroyed_at timestamptz,
  primary key(account_id,version)
);
insert into app.account_keys(account_id,version,encrypted_dek,created_at,destroyed_at)
  select k.user_id,k.version,k.encrypted_dek,k.created_at,k.destroyed_at
  from app.user_keys k join app.accounts a on a.id=k.user_id;
alter table app.account_keys enable row level security;
alter table app.account_keys force row level security;
create policy account_scope on app.account_keys
  using(account_id=nullif(current_setting('app.account_id',true),'')::uuid)
  with check(account_id=nullif(current_setting('app.account_id',true),'')::uuid);
grant select,insert,update,delete on app.account_keys to app_api,app_worker,app_system;

alter table app.workspaces drop constraint users_owner_id_fkey;
alter table app.workspaces add constraint workspaces_legacy_creator_fk
  foreign key(owner_id) references app.accounts(id) on delete set null;
alter table app.workspaces add column created_by_account_id uuid references app.accounts(id)
  on delete set null;
alter table app.workspaces add column kind text not null default 'personal'
  check(kind in ('personal','career'));
alter table app.workspaces add column status text not null default 'active'
  check(status in ('active','archived','deleting'));
alter table app.workspaces add column authorization_revision bigint not null default 1;
update app.workspaces set created_by_account_id=coalesce(owner_id,id),
  kind=case when owner_id is null then 'personal' else 'career' end;

create table app.workspace_memberships (
  workspace_id uuid not null references app.workspaces(id) on delete cascade,
  account_id uuid not null references app.accounts(id) on delete cascade,
  role text not null check(role in ('owner','editor','viewer')),
  status text not null default 'active' check(status in ('active','revoked')),
  authorization_revision bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(workspace_id,account_id)
);
create index workspace_memberships_account on app.workspace_memberships(account_id,workspace_id)
  where status='active';
insert into app.workspace_memberships(workspace_id,account_id,role)
  select id,created_by_account_id,'owner' from app.workspaces;
alter table app.profiles no force row level security;
insert into app.workspace_memberships(workspace_id,account_id,role)
  select id,user_id,'editor' from app.profiles on conflict do nothing;
alter table app.integrations no force row level security;
insert into app.workspace_memberships(workspace_id,account_id,role)
  select c.user_id,i.user_id,'editor' from app.integrations i
  join app.career_os_people c on c.career_os_user_id=i.external_id
  where i.provider='career_os' and i.status='active' and i.external_kind='user'
  on conflict do nothing;

-- Reparent account-owned references. All remaining former-users FKs explicitly
-- reference workspaces after the table rename, preserving same-workspace FKs.
do $$ declare r record; begin
  for r in select c.conrelid::regclass as tab,c.conname,a.attname
    from pg_constraint c join pg_attribute a on a.attrelid=c.conrelid and a.attnum=c.conkey[1]
    where c.contype='f' and c.confrelid='app.workspaces'::regclass
      and array_length(c.conkey,1)=1 and (
        (c.conrelid::regclass::text in ('app.identities','app.refresh_tokens','app.integrations',
          'app.profiles','app.ask_conversations','app.practice_sessions',
          'app.calendar_connections','app.calendar_meeting_context','app.companion_attachments')
          and a.attname='user_id')
        or a.attname in ('account_id','host_account_id','guest_id','private_context_account_id'))
  loop
    execute format('alter table %s drop constraint %I',r.tab,r.conname);
    execute format('alter table %s add constraint %I foreign key(%I) references app.accounts(id) '
                   'on delete cascade',r.tab,r.conname,r.attname);
  end loop;
end $$;

alter table app.identities rename to account_identities;
alter table app.account_identities rename column user_id to account_id;
alter table app.account_identities add column realm text not null default 'default'
  check(length(realm) between 1 and 200);
alter table app.account_identities drop constraint identities_pkey;
alter table app.account_identities add primary key(provider,realm,subject);
alter policy tenant_isolation on app.account_identities
  using(account_id=nullif(current_setting('app.account_id',true),'')::uuid)
  with check(account_id=nullif(current_setting('app.account_id',true),'')::uuid);
alter policy sign_in on app.account_identities using(
  realm=coalesce(nullif(current_setting('app.sign_in_realm',true),''),'default')
  and (provider||':'||subject=current_setting('app.sign_in',true)
       or 'email:'||email=current_setting('app.sign_in',true)));
create view app.identities with(security_invoker=true) as
  select provider,subject,account_id as user_id,email,created_at
  from app.account_identities where realm='default' with local check option;
grant select,insert,update,delete on app.identities to app_api,app_system;
grant select on app.identities to app_gateway;

alter table app.career_os_people rename to external_workspace_links;
alter table app.external_workspace_links rename column user_id to workspace_id;
alter table app.external_workspace_links rename column career_os_user_id to external_id;
alter table app.external_workspace_links add column provider text not null default 'career_os';
alter table app.external_workspace_links add column realm text not null default 'default';
alter table app.external_workspace_links drop constraint career_os_people_pkey;
alter table app.external_workspace_links add primary key(provider,realm,external_id);
create view app.career_os_people with(security_invoker=true) as
  select external_id as career_os_user_id,workspace_id as user_id,created_at
  from app.external_workspace_links where provider='career_os' and realm='default'
  with local check option;
grant select,insert on app.career_os_people to app_api,app_worker;
grant select on app.career_os_people to app_gateway;
grant select,insert,update,delete on app.career_os_people to app_system;

alter table app.accounts enable row level security;
alter table app.accounts force row level security;
create policy account_scope on app.accounts using(id=nullif(current_setting('app.account_id',true),'')::uuid)
  with check(id=nullif(current_setting('app.account_id',true),'')::uuid);
create policy migration_owner on app.accounts to app_owner using(true) with check(true);
create policy account_invite_lookup on app.accounts for select to app_api
  using(lower(email)=current_setting('app.seat_lookup',true) and status='active');
grant select,insert,update,delete on app.accounts to app_api,app_system;
grant select on app.accounts to app_worker;
grant select(id,settings) on app.accounts to app_gateway;

alter table app.workspace_memberships enable row level security;
alter table app.workspace_memberships force row level security;
create policy membership_self on app.workspace_memberships for select
  using(account_id=nullif(current_setting('app.account_id',true),'')::uuid);
create policy migration_owner on app.workspace_memberships to app_owner using(true) with check(true);
create policy migration_owner on app.workspaces to app_owner using(true) with check(true);
grant select on app.workspace_memberships to app_api,app_gateway,app_worker,app_system;
grant insert,update,delete on app.workspace_memberships to app_system;

create function app.has_workspace_access(wid uuid) returns boolean language sql stable
security definer set search_path=pg_catalog,app as $$
  select exists(select 1 from app.workspace_memberships m join app.accounts a on a.id=m.account_id
    join app.workspaces w on w.id=m.workspace_id where m.workspace_id=wid
    and m.account_id=nullif(current_setting('app.account_id',true),'')::uuid
    and m.status='active' and a.status='active' and w.status='active')
$$;
create function app.has_workspace_write(wid uuid) returns boolean language sql stable
security definer set search_path=pg_catalog,app as $$
  select app.has_workspace_access(wid) and exists(select 1 from app.workspace_memberships
    where workspace_id=wid and account_id=nullif(current_setting('app.account_id',true),'')::uuid
    and status='active' and role in ('owner','editor'))
$$;
revoke all on function app.has_workspace_access(uuid),app.has_workspace_write(uuid) from public;
grant execute on function app.has_workspace_access(uuid),app.has_workspace_write(uuid)
  to app_api,app_gateway,app_worker,app_system;

-- Billing follows the authenticated payer even when a personal workspace UUID
-- also happens to equal a different member's account UUID.
do $$ declare t text; begin
  foreach t in array array['live_usage_sessions','live_usage_events','live_usage_intervals',
                          'usage_adjustments'] loop
    execute format('alter policy payer_reads on app.%I '
      'using(payer_account_id=nullif(current_setting(''app.account_id'',true),'''')::uuid)',t);
  end loop;
end $$;
create policy legacy_membership on app.legacy_usage_estimates as restrictive for select to app_api
  using(app.has_workspace_access(workspace_id));

-- Explicit canonical ownership columns during the SQL compatibility transition.
-- user_id is an input alias only: it cannot point to an account or another workspace.
do $$ declare t text; begin
  foreach t in array array['user_keys','applications','interviews','memory_nodes','memory_edges',
    'memory_proposals','transcript_segments','questions','answers','session_outputs','documents',
    'employers','work_logs','company_documents','knowledge_chunks','resume_versions',
    'data_revisions','data_changes','usage_daily'] loop
    execute format('alter table app.%I add column workspace_id uuid generated always as '
                   '(user_id) stored',t);
    execute format('create policy membership_boundary on app.%I as restrictive '
      'to app_api,app_gateway using(app.has_workspace_access(user_id)) '
      'with check(app.has_workspace_write(user_id))',t);
    execute format('create policy membership_delete on app.%I as restrictive for delete '
      'to app_api using(app.has_workspace_write(user_id))',t);
  end loop;
  foreach t in array array['refresh_tokens','integrations','profiles','ask_conversations',
    'ask_messages','practice_sessions','practice_operations','calendar_connections',
    'calendar_meeting_context','companion_attachments'] loop
    execute format('alter table app.%I add column account_id uuid generated always as '
                   '(user_id) stored',t);
    execute format('alter policy tenant_isolation on app.%I '
      'using(account_id=nullif(current_setting(''app.account_id'',true),'''')::uuid) '
      'with check(account_id=nullif(current_setting(''app.account_id'',true),'''')::uuid)',t);
  end loop;
  foreach t in array array['context_requests','context_brief_versions'] loop
    execute format('alter policy private_account on app.%I '
      'using(account_id=nullif(current_setting(''app.account_id'',true),'''')::uuid) '
      'with check(account_id=nullif(current_setting(''app.account_id'',true),'''')::uuid)',t);
  end loop;
  foreach t in array array['ask_conversations','practice_sessions','calendar_connections',
    'companion_attachments','context_requests'] loop
    execute format('alter table app.%I add column workspace_id uuid generated always as '
                   '(profile_id) stored',t);
  end loop;
end $$;
alter table app.profiles add column workspace_id uuid generated always as (id) stored;
alter table app.session_seats add column workspace_id uuid generated always as (user_id) stored;
alter table app.session_seats add column guest_account_id uuid generated always as (guest_id) stored;
alter table app.profiles add constraint profiles_workspace_fk foreign key(id)
  references app.workspaces(id) on delete cascade;
alter table app.context_brief_versions add constraint context_version_request_account
  unique(id,request_id,account_id);
alter table app.context_requests drop constraint context_active_version_fk;
alter table app.context_requests add constraint context_active_version_fk
  foreign key(active_version_id,id,account_id)
  references app.context_brief_versions(id,request_id,account_id) deferrable initially deferred;
-- Guest seats retain their explicit guest-read policy; their ordinary host scope
-- needs membership without excluding a legitimately invited non-member guest.
create policy membership_boundary on app.workspaces as restrictive to app_api,app_gateway
  using(app.has_workspace_access(id)) with check(app.has_workspace_write(id));
create policy membership_delete on app.workspaces as restrictive for delete to app_api
  using(app.has_workspace_write(id));
create policy membership_boundary on app.session_seats as restrictive to app_api,app_gateway
  using(app.has_workspace_access(user_id) or
    (guest_id=nullif(current_setting('app.account_id',true),'')::uuid
      and status in ('invited','accepted','admitted') and expires_at>now()))
  with check(app.has_workspace_write(user_id));
create policy membership_delete on app.session_seats as restrictive for delete to app_api
  using(app.has_workspace_write(user_id));

create function app.bootstrap_account_workspace() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
begin
  -- Only the nested legacy-import trigger may attach its newly inserted workspace.
  -- A normal new account must never acquire an independently retained workspace.
  if pg_trigger_depth()=1 and exists(select 1 from app.workspaces where id=new.id) then
    raise exception 'account ID already belongs to a workspace' using errcode='23514';
  end if;
  insert into app.workspaces(id,email,display_name,plan,settings,external_ref,created_at,
                             created_by_account_id,kind)
    values(new.id,new.email,new.display_name,new.plan,new.settings,new.external_ref,new.created_at,
           new.id,'personal') on conflict(id) do nothing;
  insert into app.workspace_memberships(workspace_id,account_id,role)
    values(new.id,new.id,'owner') on conflict do nothing;
  return null;
end $$;
create trigger bootstrap_account_workspace after insert on app.accounts
  for each row execute function app.bootstrap_account_workspace();

-- Old test/import clients can still create users. A personal workspace created
-- through that legacy path obtains a real account; new runtime auth writes accounts.
create function app.bootstrap_legacy_workspace() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
declare creator uuid;
begin
  creator:=coalesce(new.created_by_account_id,new.owner_id);
  if creator is null and new.kind='personal' then
    insert into app.accounts(id,email,display_name,plan,settings,external_ref,created_at)
      values(new.id,new.email,new.display_name,new.plan,new.settings,new.external_ref,new.created_at)
      on conflict(id) do nothing;
    creator:=new.id;
  end if;
  if creator is not null then
    update app.workspaces set created_by_account_id=creator,
      kind=case when new.owner_id is null then new.kind else 'career' end where id=new.id;
    insert into app.workspace_memberships(workspace_id,account_id,role)
      values(new.id,creator,'owner') on conflict do nothing;
  end if;
  return null;
end $$;
create trigger bootstrap_legacy_workspace after insert on app.workspaces
  for each row execute function app.bootstrap_legacy_workspace();

create function app.sync_account_workspace_metadata() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
begin
  update app.workspaces set email=new.email,display_name=new.display_name,plan=new.plan,
    settings=new.settings,external_ref=new.external_ref where id=new.id and kind='personal'
    and exists(select 1 from app.workspace_memberships where workspace_id=new.id
      and account_id=new.id and status='active' and role='owner');
  return null;
end $$;
create trigger sync_account_workspace_metadata after update of email,display_name,plan,settings,
  external_ref on app.accounts for each row execute function app.sync_account_workspace_metadata();

-- Trusted API calls this only after validating the remote Career-OS grant. The
-- actor is transaction context; no client-supplied account can receive membership.
create function app.provision_external_workspace(external_person text,proposed uuid,label text)
returns uuid language plpgsql security definer set search_path=pg_catalog,app as $$
declare actor uuid:=nullif(current_setting('app.account_id',true),'')::uuid; found uuid;
begin
  if not exists(select 1 from app.accounts where id=actor and status='active') then
    raise exception 'active account required' using errcode='42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('career_os:default:'||external_person,0));
  select workspace_id into found from app.external_workspace_links
    where provider='career_os' and realm='default' and external_id=external_person;
  if found is null then
    if proposed=actor then found:=actor;
    else
      insert into app.workspaces(id,email,display_name,owner_id,created_by_account_id,kind)
        values(proposed,'workspace-'||proposed||'@profiles.invalid',label,actor,actor,'career');
      found:=proposed;
    end if;
    insert into app.external_workspace_links(provider,realm,external_id,workspace_id)
      values('career_os','default',external_person,found);
  end if;
  insert into app.workspace_memberships(workspace_id,account_id,role)
    values(found,actor,'editor') on conflict(workspace_id,account_id) do nothing;
  return found;
end $$;
revoke all on function app.provision_external_workspace(text,uuid,text) from public;
grant execute on function app.provision_external_workspace(text,uuid,text) to app_api;

create function app.account_workspaces()
returns table(id uuid,name text,career_os_user_id text) language sql stable
security definer set search_path=pg_catalog,app as $$
  select w.id,w.display_name,l.external_id from app.workspace_memberships m
  join app.workspaces w on w.id=m.workspace_id
  left join app.external_workspace_links l on l.workspace_id=w.id
    and l.provider='career_os' and l.realm='default'
  where m.account_id=nullif(current_setting('app.account_id',true),'')::uuid
    and m.status='active' and w.status='active' and app.has_workspace_access(w.id)
  order by w.display_name,w.created_at,w.id
$$;
revoke all on function app.account_workspaces() from public;
grant execute on function app.account_workspaces() to app_api,app_gateway;

-- Membership changes have one narrow owner-authorized entry point. Direct API
-- writes to the membership table are intentionally not granted.
create function app.set_workspace_membership(wid uuid,member uuid,member_role text,
                                             member_status text default 'active')
returns void language plpgsql security definer set search_path=pg_catalog,app as $$
declare actor uuid:=nullif(current_setting('app.account_id',true),'')::uuid;
begin
  perform 1 from app.workspaces where id=wid for update;
  if not app.has_workspace_access(wid) or not exists(
    select 1 from app.workspace_memberships where workspace_id=wid and account_id=actor
      and status='active' and role='owner') then
    raise exception 'active workspace owner required' using errcode='42501';
  end if;
  if not exists(select 1 from app.accounts where id=member and status='active') then
    raise exception 'active member account required' using errcode='23503';
  end if;
  insert into app.workspace_memberships(workspace_id,account_id,role,status)
    values(wid,member,member_role,member_status)
    on conflict(workspace_id,account_id) do update set role=excluded.role,status=excluded.status;
end $$;
revoke all on function app.set_workspace_membership(uuid,uuid,text,text) from public;
grant execute on function app.set_workspace_membership(uuid,uuid,text,text) to app_api;

create function app.lock_workspace_membership_change() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
declare wid uuid;
begin
  wid:=case when tg_op='DELETE' then old.workspace_id else new.workspace_id end;
  -- A real row update fences simultaneous removals even under repeatable-read.
  update app.workspaces set authorization_revision=authorization_revision+1 where id=wid;
  if tg_op='UPDATE' then
    if old.workspace_id<>new.workspace_id or old.account_id<>new.account_id then
      raise exception 'membership scope is immutable' using errcode='23514';
    end if;
    new.authorization_revision:=old.authorization_revision+1;
    new.updated_at:=clock_timestamp();
  end if;
  return case when tg_op='DELETE' then old else new end;
end $$;
create trigger lock_workspace_membership_change before insert or update or delete
  on app.workspace_memberships for each row execute function app.lock_workspace_membership_change();
create function app.require_workspace_owner() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
declare wid uuid;
begin
  if tg_table_name='workspaces' then wid:=new.id;
  else wid:=case when tg_op='DELETE' then old.workspace_id else new.workspace_id end; end if;
  if exists(select 1 from app.workspaces where id=wid and status='active')
     and not exists(select 1 from app.workspace_memberships m join app.accounts a on a.id=m.account_id
                    where m.workspace_id=wid and m.status='active' and m.role='owner'
                    and a.status='active') then
    raise exception 'active workspace requires an active owner' using errcode='23514';
  end if;
  return null;
end $$;
create constraint trigger workspace_owner_required after insert or update on app.workspaces
  deferrable initially deferred for each row execute function app.require_workspace_owner();
create constraint trigger membership_owner_required after insert or update or delete
  on app.workspace_memberships deferrable initially deferred
  for each row execute function app.require_workspace_owner();
create function app.require_active_account_owner() returns trigger language plpgsql
security definer set search_path=pg_catalog,app as $$
declare wid uuid;
begin
  if new.status=old.status or new.status='active' then return null; end if;
  for wid in select w.id from app.workspaces w join app.workspace_memberships m
    on m.workspace_id=w.id where m.account_id=new.id and m.role='owner' and w.status='active'
    order by w.id for update of w loop
    if not exists(select 1 from app.workspace_memberships m join app.accounts a on a.id=m.account_id
      where m.workspace_id=wid and m.role='owner' and m.status='active' and a.status='active') then
      raise exception 'active workspace requires an active owner' using errcode='23514';
    end if;
  end loop;
  return null;
end $$;
create constraint trigger account_owner_required after update on app.accounts
  deferrable initially deferred for each row execute function app.require_active_account_owner();

create view app.users with(security_invoker=true) as
  select id,email,display_name,plan,settings,external_ref,created_at,owner_id,call_language
  from app.workspaces;
grant select,insert,update,delete on app.users to app_api,app_worker,app_system;
grant select on app.users to app_gateway;
alter table app.workspaces force row level security;
do $$ declare r record; begin
  for r in select oid::regclass as tab from pg_class
    where relnamespace='app'::regnamespace and relkind='r' and relrowsecurity loop
    execute format('alter table %s force row level security',r.tab);
  end loop;
end $$;
revoke all on function app.bootstrap_account_workspace(),app.bootstrap_legacy_workspace(),
  app.sync_account_workspace_metadata(),
  app.lock_workspace_membership_change(),app.require_workspace_owner(),
  app.require_active_account_owner(),app.guard_account_id_lifetime() from public;
