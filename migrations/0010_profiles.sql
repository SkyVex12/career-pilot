-- 0010_profiles: one account works for several people. Besides the account itself ("Me"),
-- each Career-OS user a Career-OS admin link reads gets a profile, and the desktop app switches
-- between them; everything the app keeps (applications, resumes, interviews, sessions, work,
-- memory, packs) is kept per profile.
-- Source: ARCHITECTURE.md, "Profiles".
--
-- A profile is an `app.users` row of its own (owner_id names the account), so every table,
-- row-level security policy, data key and Valkey key works per profile unchanged: the API and
-- the gateway act as the profile's user id once they have checked that it belongs to the
-- account (`app.profiles`, which the account reads under its own row-level security).
--
-- Expand only: a nullable column and a new table. Existing rows are the accounts' own ("Me").

set local role app_owner;

-- The account a profile belongs to; null for an account. Deleting the account deletes its
-- profiles' users rows, and with them every row of theirs.
alter table app.users
  add column owner_id uuid references app.users(id) on delete cascade;
create index users_owner on app.users (owner_id) where owner_id is not null;

-- The account's profiles, under the account's row-level security. `id` is the profile's
-- users row; `career_os_user_id` the Career-OS user it stands for.
create table app.profiles (
  id                uuid primary key,
  user_id           uuid not null references app.users(id) on delete cascade,
  career_os_user_id text not null check (length(career_os_user_id) between 1 and 128),
  name              text not null check (length(name) <= 200),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (user_id, career_os_user_id)
);

alter table app.profiles enable row level security;
alter table app.profiles force row level security;
create policy tenant_isolation on app.profiles
  using (user_id = current_setting('app.user_id')::uuid)
  with check (user_id = current_setting('app.user_id')::uuid);

grant select, insert, update, delete on app.profiles to app_api, app_worker, app_system;
-- The gateway checks a session's profile against the account.
grant select on app.profiles to app_gateway;
