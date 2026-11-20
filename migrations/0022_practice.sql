-- Practice is private account data selected within an authorized profile, separate from calls.
set local role app_owner;
create table app.practice_sessions (
  id uuid primary key,
  user_id uuid not null references app.users(id) on delete cascade,
  profile_id uuid not null references app.users(id) on delete cascade,
  request_id uuid not null,
  request_hash text not null,
  status text not null check (status in ('preparing','answering','processing','paused','finished','failed')),
  state_enc bytea not null,
  key_version int not null,
  revision int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id,request_id),
  unique (id,user_id)
);
create table app.practice_operations (
  id uuid not null,
  user_id uuid not null,
  session_id uuid not null,
  kind text not null,
  request_hash text not null,
  status text not null check(status in ('pending','done','failed','cancelled')),
  result_enc bytea,
  key_version int,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(session_id,id),
  foreign key(session_id,user_id) references app.practice_sessions(id,user_id) on delete cascade
);
create index practice_sessions_profile on app.practice_sessions(user_id,profile_id,updated_at desc);
alter table app.practice_sessions enable row level security;
alter table app.practice_sessions force row level security;
create policy tenant_isolation on app.practice_sessions
  using(user_id=current_setting('app.user_id')::uuid)
  with check(user_id=current_setting('app.user_id')::uuid);
alter table app.practice_operations enable row level security;
alter table app.practice_operations force row level security;
create policy tenant_isolation on app.practice_operations
  using(user_id=current_setting('app.user_id')::uuid)
  with check(user_id=current_setting('app.user_id')::uuid);
grant select,insert,update,delete on app.practice_sessions,app.practice_operations
  to app_api,app_worker,app_system;
