-- Private imported context makes all retained artifacts private to the authorizing account.
-- The marker is sticky: removing a source brief never makes historical output public.
set local role app_owner;
alter table app.sessions add column private_context_account_id uuid
 references app.users(id) on delete cascade;
create index sessions_private_account on app.sessions(private_context_account_id)
 where private_context_account_id is not null;
