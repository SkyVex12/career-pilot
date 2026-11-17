-- 0017_guest_seats: guest seats on a live call. The host (the person running the call) invites
-- another Copilot account by email; the guest accepts in their own app, the host admits them,
-- and the guest then follows the call's overlay live and, if allowed, sends notes that only
-- the host sees. Source: ARCHITECTURE.md, "Guest seats" and "Schema DDL".
--
-- A seat belongs to one live session. `user_id` is the session's owner (the account or the
-- profile the call runs for), so the host reads and writes seats under the usual tenant policy.
-- The guest is always an account (`guest_id`), never a profile, and reads only their own seats,
-- and only while the seat is open. Answering an invite goes through `app.seat_answer`, which
-- may change nothing but the status; everything else is the host's.
--
-- `capabilities` lists what the guest may do. Phase 1 grants `view` (always) and `note`; the
-- other values are accepted here so later phases need no migration to widen a check. `rev`
-- goes up on every change, so the gateway can tell a grant it checked from a newer one.
--
-- Expand only: a new table, a new policy on users for the invite lookup, and two functions.
-- Code from before this migration never reads any of them.

set local role app_owner;

create table app.session_seats (
  id              uuid primary key,
  user_id         uuid not null references app.users(id) on delete cascade,  -- the session's owner
  session_id      uuid not null,             -- no foreign key: the row comes at the first flush
  host_account_id uuid not null references app.users(id) on delete cascade,
  guest_id        uuid not null references app.users(id) on delete cascade,
  host_name       text not null check (length(host_name) <= 200),
  guest_name      text not null check (length(guest_name) <= 200),
  guest_email     text not null check (length(guest_email) <= 320),
  mode            text not null check (mode in ('interview', 'standup', 'meeting')),
  title           text check (length(title) <= 300),
  status          text not null check (status in
                    ('invited', 'accepted', 'admitted', 'declined', 'removed', 'expired', 'ended')),
  capabilities    text[] not null default '{view}',
  rev             int  not null default 1,
  created_at      timestamptz not null default now(),
  expires_at      timestamptz not null,      -- an open seat is gone after this, whatever its status
  accepted_at     timestamptz,
  admitted_at     timestamptz,
  ended_at        timestamptz,
  check (guest_id <> host_account_id),
  check (guest_id <> user_id),
  check ('view' = any (capabilities)),
  check (capabilities <@ array['view', 'note', 'voice', 'control.answer', 'control.session',
                               'device.view', 'device.control']::text[])
);
-- One open seat per guest and session.
create unique index session_seats_open on app.session_seats (session_id, guest_id)
  where status in ('invited', 'accepted', 'admitted');
create index session_seats_session on app.session_seats (user_id, session_id);
create index session_seats_guest on app.session_seats (guest_id)
  where status in ('invited', 'accepted', 'admitted');

alter table app.session_seats enable row level security;
alter table app.session_seats force row level security;
-- The host: every seat of their sessions.
create policy tenant_isolation on app.session_seats
  using (user_id = current_setting('app.user_id')::uuid)
  with check (user_id = current_setting('app.user_id')::uuid);
-- The guest: their own open seats, read only.
create policy guest_reads on app.session_seats for select
  using (guest_id = current_setting('app.user_id')::uuid
         and status in ('invited', 'accepted', 'admitted')
         and expires_at > now());

-- The worker closes a session's seats when it ends (and deletes them with the account).
grant select, insert, update, delete on app.session_seats to app_api, app_worker, app_system;
-- The gateway checks a guest's seat when they join.
grant select on app.session_seats to app_gateway;

-- Finding the guest's account by email, for an invite. The API sets `app.seat_lookup` to the
-- normalized address in the one transaction that looks it up; it is unset (null) everywhere
-- else, so this policy matches nothing then. Profiles (owner_id set) are never found.
create policy seat_lookup on app.users for select
  using (owner_id is null and lower(email) = current_setting('app.seat_lookup', true));

-- The guest answers an invite: accepted or declined, nothing else. Runs as its owner to update
-- a row the guest may only read, and checks the guest itself.
-- Returns the seat's session, or null when it is not the caller's open invite. No RETURNING:
-- a declined row is no longer the guest's to read.
create function app.seat_answer(seat uuid, accept boolean)
  returns uuid
  language plpgsql security definer set search_path = app, pg_temp as $$
declare
  me  uuid := current_setting('app.user_id')::uuid;
  sid uuid;
begin
  select s.session_id into sid from app.session_seats s
   where s.id = seat and s.guest_id = me and s.status = 'invited' and s.expires_at > now();
  if sid is null then
    return null;
  end if;
  update app.session_seats s
     set status = case when accept then 'accepted' else 'declined' end,
         accepted_at = case when accept then now() else s.accepted_at end,
         ended_at = case when accept then s.ended_at else now() end,
         rev = s.rev + 1
   where s.id = seat and s.guest_id = me and s.status = 'invited';
  return sid;
end
$$;
-- A security definer function owned by app_owner bypasses nothing by itself: the table forces
-- row-level security, so the update needs a policy the owner matches. Only app_owner gets it,
-- and app_owner logs in for migrations alone, so in practice it is this function's.
create policy seat_answer on app.session_seats for update to app_owner
  using (guest_id = current_setting('app.user_id')::uuid and status = 'invited')
  with check (guest_id = current_setting('app.user_id')::uuid
              and status in ('accepted', 'declined'));
-- An update that reads the row (its where clause) must also see the new row: a declined seat
-- is no longer the guest's to read under `guest_reads`, so the function's owner gets this.
create policy seat_answer_read on app.session_seats for select to app_owner
  using (guest_id = current_setting('app.user_id')::uuid);
revoke all on function app.seat_answer(uuid, boolean) from public;
grant execute on function app.seat_answer(uuid, boolean) to app_api;
