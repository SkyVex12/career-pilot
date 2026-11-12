-- 0015_career_os_sign_in: "Sign in with Career-OS", a third way in beside Google and email.
-- Source: ARCHITECTURE.md, "Connection and auth" (Career-OS sign-in).
--
-- A Career-OS sign-in is an identity like the others: `('career_os', '<kind>:<id>')`, where
-- kind is `user` or `admin` (a super admin is an admin; its role can change, its id cannot).
-- Career-OS does not verify email addresses, so such a row carries none: `email` is null for
-- it and stays required for Google and email. An account has at most one, as it has at most
-- one Career-OS link.
--
-- The API's refresh tokens (0014) now belong to either sign-in: `provider` says which, and
-- `subject` names the Career-OS account for a Career-OS sign-in, which has no email.
--
-- Sign-in runs before the user is known. To find the accounts already linked to the Career-OS
-- account signing in, `app.integrations` gets a read-only `sign_in` policy like 0014's: a
-- transaction sees the Career-OS links whose account it names in `app.sign_in`
-- (`career_os_link:<kind>:<id>`), and nothing else.
--
-- Expand only: nothing existing changes meaning, no row is moved or rewritten.

set local role app_owner;

alter table app.identities drop constraint identities_provider_check;
alter table app.identities
  add constraint identities_provider_check check (provider in ('google', 'email', 'career_os'));
alter table app.identities alter column email drop not null;
alter table app.identities
  add constraint identities_email_by_provider check ((provider = 'career_os') = (email is null));
create unique index identities_one_career_os on app.identities (user_id)
  where provider = 'career_os';

alter table app.refresh_tokens
  add column provider text not null default 'email' check (provider in ('email', 'career_os')),
  add column subject text;
alter table app.refresh_tokens alter column email drop not null;
alter table app.refresh_tokens
  add constraint refresh_tokens_who check (
    (provider = 'email' and email is not null) or (provider = 'career_os' and subject is not null)
  );

-- A link made before 0009 has no kind and counts as a user link, as everywhere else.
create policy sign_in on app.integrations for select
  using (provider = 'career_os' and external_id is not null
         and 'career_os_link:'
             || case when external_kind in ('admin', 'super_admin') then 'admin' else 'user' end
             || ':' || external_id = current_setting('app.sign_in', true));
