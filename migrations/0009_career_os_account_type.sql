-- 0009_career_os_account_type: a Career-OS link can belong to a Career-OS admin or super admin,
-- whose token reads the applications of the users linked to them, not only to one user.
-- Source: ARCHITECTURE.md, "Career-OS link".
--
-- Expand only: one nullable column. Links from before are user links (null reads as 'user').

set local role app_owner;

alter table app.integrations
  add column external_kind text;  -- the Career-OS account type: user, admin or super_admin
