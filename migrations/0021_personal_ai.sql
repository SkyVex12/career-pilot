-- Private context belongs to the authorizing account, even on a shared profile.
set local role app_owner;
create table app.context_requests (
 id uuid primary key,
 account_id uuid not null references app.users(id) on delete cascade,
 profile_id uuid not null references app.users(id) on delete cascade,
 employer_id uuid,
 job_id uuid,
 revision integer not null default 1 check (revision > 0),
 body_enc bytea not null,
 key_version integer not null,
 active_version_id uuid,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique (id, account_id),
 foreign key (employer_id, profile_id) references app.employers(id, user_id) on delete cascade,
 foreign key (job_id, profile_id) references app.applications(id, user_id) on delete cascade,
 check (employer_id is null or job_id is null)
);
create index context_requests_scope on app.context_requests
 (account_id, profile_id, employer_id, job_id, updated_at desc);
create table app.context_brief_versions (
 id uuid primary key,
 request_id uuid not null,
 account_id uuid not null,
 request_revision integer not null,
 idempotency_key text not null check (length(idempotency_key) between 1 and 100),
 input_hash text not null,
 origin text not null check (origin in ('mcp','paste','correction')),
 body_enc bytea not null,
 key_version integer not null,
 created_at timestamptz not null default now(),
 foreign key (request_id, account_id) references app.context_requests(id, account_id)
  on delete cascade,
 unique (request_id, idempotency_key)
);
alter table app.context_requests add constraint context_active_version_fk
 foreign key (active_version_id) references app.context_brief_versions(id)
 deferrable initially deferred;
do $$ declare t text; begin
 foreach t in array array['context_requests','context_brief_versions'] loop
  execute format('alter table app.%I enable row level security', t);
  execute format('alter table app.%I force row level security', t);
  execute format('create policy private_account on app.%I using '
   || '(account_id = current_setting(''app.user_id'', true)::uuid) with check '
   || '(account_id = current_setting(''app.user_id'', true)::uuid)', t);
 end loop;
end $$;
grant select, insert, update, delete on app.context_requests, app.context_brief_versions
 to app_api, app_system;
-- No guest/profile-only access, and no gateway grant until account-aware retrieval exists.
