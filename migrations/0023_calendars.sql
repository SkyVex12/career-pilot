-- Read-only calendar connections are owned by the signed-in account and scoped to a profile.
set local role app_owner;
create table app.calendar_connections (
  id uuid primary key,
  user_id uuid not null references app.users(id) on delete cascade,
  profile_id uuid not null references app.users(id) on delete cascade,
  provider text not null check (provider in ('google', 'outlook')),
  status text not null default 'pending' check (status in ('pending','active','reauth_required','error')),
  oauth_nonce text,
  token_enc bytea,
  token_key_version int,
  cache_enc bytea,
  cache_key_version int,
  selected_calendars text[] not null default '{}',
  last_sync_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, profile_id, provider)
);
alter table app.calendar_connections enable row level security;
alter table app.calendar_connections force row level security;
create policy tenant_isolation on app.calendar_connections
  using (user_id = current_setting('app.user_id')::uuid)
  with check (user_id = current_setting('app.user_id')::uuid);
grant select,insert,update,delete on app.calendar_connections to app_api,app_system;
create index calendar_connections_profile on app.calendar_connections(user_id,profile_id);
alter table app.calendar_connections add constraint calendar_connection_owner unique(id,user_id);
create table app.calendar_meeting_context (
  id uuid primary key,
  user_id uuid not null references app.users(id) on delete cascade,
  connection_id uuid not null,
  calendar_id text not null,
  instance_id text not null,
  series_id text,
  context_enc bytea not null,
  key_version int not null,
  updated_at timestamptz not null default now(),
  unique(connection_id,calendar_id,instance_id),
  foreign key(connection_id,user_id) references app.calendar_connections(id,user_id) on delete cascade
);
alter table app.calendar_meeting_context enable row level security;
alter table app.calendar_meeting_context force row level security;
create policy tenant_isolation on app.calendar_meeting_context
  using(user_id=current_setting('app.user_id')::uuid)
  with check(user_id=current_setting('app.user_id')::uuid);
grant select,insert,update,delete on app.calendar_meeting_context to app_api,app_system;
