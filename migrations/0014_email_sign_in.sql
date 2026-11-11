-- 0014_email_sign_in: sign-in with an emailed one-time code, alongside Google sign-in.
-- Source: ARCHITECTURE.md, "Connection and auth" (Email sign-in).
--
-- `app.identities` maps a sign-in (provider, subject) to the account it signs in to, so one
-- account can have several: Google (`subject` is Google's `sub`) and email (`subject` is the
-- normalized address). `email` is the address that provider verified.
--
-- `app.refresh_tokens` holds the email sign-in's refresh tokens, as SHA-256 hashes. Every
-- refresh replaces the token (`replaced_at`); a replaced token shown again means it leaked, and
-- revokes every token of its sign-in (`family_id`).
--
-- Both are user tables under the usual row-level security. Sign-in runs before the user is
-- known, so each also has a read-only `sign_in` policy: a transaction sees the one row whose
-- key it presents in `app.sign_in` (`google:<sub>`, `email:<address>`, `refresh:<hash>`), and
-- nothing else (copilot_core.db.sign_in_tx).
--
-- Expand only: two new tables. Existing accounts keep their ids.

set local role app_owner;

create table app.identities (
  provider   text not null check (provider in ('google', 'email')),
  subject    text not null check (length(subject) between 1 and 320),
  user_id    uuid not null references app.users(id) on delete cascade,
  email      text not null check (email = lower(btrim(email)) and length(email) between 3 and 320),
  created_at timestamptz not null default now(),
  primary key (provider, subject)
);
create index identities_user on app.identities (user_id);
create index identities_email on app.identities (email);

-- Existing accounts signed in with Google, and keep their ids (a UUIDv5 of Google's `sub`).
-- The `sub` cannot be read back from the id, so each gets a row with its own id as the
-- subject, which no real `sub` equals (they are decimal numbers). It carries the account's
-- verified email, which is what an email sign-in is matched against, and the API replaces it
-- with the real `sub` the next time the account signs in with Google
-- (copilot_core.identities.adopt_google). Profiles' rows (owner_id) are not accounts.
alter table app.users no force row level security;
insert into app.identities (provider, subject, user_id, email)
select 'google', u.id::text, u.id, lower(btrim(u.email))
  from app.users u
 where u.owner_id is null and length(btrim(u.email)) >= 3;
alter table app.users force row level security;

create table app.refresh_tokens (
  token_hash  text primary key check (length(token_hash) = 64),  -- SHA-256 of the token, hex
  user_id     uuid not null references app.users(id) on delete cascade,
  family_id   uuid not null,                   -- one per sign-in; every rotation keeps it
  email       text not null,                   -- the address signed in with
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null,
  replaced_at timestamptz,                     -- rotated; shown again, the family is revoked
  revoked_at  timestamptz
);
create index refresh_tokens_family on app.refresh_tokens (user_id, family_id);

do $$
declare
  t text;
begin
  foreach t in array array['identities', 'refresh_tokens'] loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);
    execute format(
      'create policy tenant_isolation on app.%I'
      ' using (user_id = current_setting(''app.user_id'')::uuid)'
      ' with check (user_id = current_setting(''app.user_id'')::uuid)', t);
  end loop;
end
$$;

-- Before the user is known. `app.sign_in` is unset (null) or empty outside a sign-in, and no
-- key is empty, so these match nothing then.
create policy sign_in on app.identities for select
  using (provider || ':' || subject = current_setting('app.sign_in', true)
         or 'email:' || email = current_setting('app.sign_in', true));
create policy sign_in on app.refresh_tokens for select
  using ('refresh:' || token_hash = current_setting('app.sign_in', true));

grant select, insert, update, delete on app.identities, app.refresh_tokens to app_api;
grant select, insert, update, delete on app.identities, app.refresh_tokens to app_system;
-- The gateway resolves a Google sign-in to its account.
grant select on app.identities to app_gateway;
