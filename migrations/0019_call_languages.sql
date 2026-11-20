-- Profile preferences must survive account-settings synchronization.
set local role app_owner;
alter table app.users add column call_language text not null default 'en';
-- Historical calls have unknown language; never backfill a guess.
alter table app.sessions add column spoken_language text;
