-- 0013_shared_people: one workspace per Career-OS person, shared by every account linked to
-- them. Before, each account made its own profile for a Career-OS user, so applications pulled
-- by one account were invisible to another account linked to the same person (2026-10-29).
-- Source: ARCHITECTURE.md, "Profiles".
--
-- `app.career_os_people` names each Career-OS user's one `app.users` row: an admin's profile
-- row, or the user's own account row (a user link). Accounts list it in `app.profiles` (an
-- admin link) or reach it as "Me" (a user link). It is a lookup without row-level security,
-- like job_runs: it holds ids only, and acting as a row still needs the account's own
-- `app.profiles` row or link, both under the account's row-level security.
--
-- Data stays where it is. For each Career-OS user the row holding the most applications (then
-- the oldest) becomes the workspace, and every account's profile for that user points to it.
-- A profile row no account lists any more keeps its data (owner's, unreachable from others).

set local role app_owner;

alter table app.users no force row level security;
alter table app.profiles no force row level security;
alter table app.applications no force row level security;
alter table app.integrations no force row level security;

create table app.career_os_people (
  career_os_user_id text primary key check (length(career_os_user_id) between 1 and 128),
  user_id           uuid not null unique references app.users(id) on delete cascade,
  created_at        timestamptz not null default now()
);

-- Several accounts may list one person.
alter table app.profiles drop constraint profiles_pkey;
alter table app.profiles add primary key (user_id, id);
create index profiles_person on app.profiles (id);

with candidates as (
  select p.career_os_user_id as cos, p.id as row_id from app.profiles p
  union
  select i.external_id, i.user_id from app.integrations i
   where i.provider = 'career_os' and i.status = 'active' and i.external_kind = 'user'
     and i.external_id is not null and length(i.external_id) between 1 and 128
), scored as (
  select c.cos, c.row_id, u.created_at,
         (select count(*) from app.applications a where a.user_id = c.row_id) as apps
    from candidates c join app.users u on u.id = c.row_id
)
insert into app.career_os_people (career_os_user_id, user_id)
select distinct on (cos) cos, row_id from scored order by cos, apps desc, created_at, row_id;

-- An account whose own row is the workspace reaches it as "Me", not as a profile.
delete from app.profiles p using app.career_os_people c
 where c.career_os_user_id = p.career_os_user_id and c.user_id = p.user_id;
update app.profiles p set id = c.user_id
  from app.career_os_people c
 where c.career_os_user_id = p.career_os_user_id and p.id <> c.user_id;

alter table app.users force row level security;
alter table app.profiles force row level security;
alter table app.applications force row level security;
alter table app.integrations force row level security;

grant select, insert on app.career_os_people to app_api, app_worker;
grant select, insert, update, delete on app.career_os_people to app_system;
-- The gateway resolves "Me" for a user link: the person's workspace, from the link's user.
grant select on app.career_os_people to app_gateway;
grant select (external_id, external_kind) on app.integrations to app_gateway;
