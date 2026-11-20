set local role app_owner;
-- Live sessions are written asynchronously, so session_id intentionally has no FK.
create table app.companion_attachments (
  id uuid primary key,
  user_id uuid not null references app.users(id) on delete cascade,
  profile_id uuid not null references app.users(id) on delete cascade,
  session_id uuid not null,
  device_name text not null check(length(device_name) between 1 and 100),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);
alter table app.companion_attachments enable row level security;
alter table app.companion_attachments force row level security;
create policy tenant_isolation on app.companion_attachments
  using(user_id=current_setting('app.user_id')::uuid)
  with check(user_id=current_setting('app.user_id')::uuid);
grant select,insert,update,delete on app.companion_attachments to app_api,app_system;
grant select on app.companion_attachments to app_gateway;
create index companion_attachments_session on app.companion_attachments(user_id,session_id);
