-- Private conversations use account RLS plus an explicit active-profile boundary.
set local role app_owner;
create table app.ask_conversations (
  id uuid primary key,
  user_id uuid not null references app.users(id) on delete cascade,
  profile_id uuid not null references app.users(id) on delete cascade,
  title text not null check (length(title) between 1 and 200),
  scope jsonb not null default '{"kind":"auto"}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);
create table app.ask_messages (
  id uuid primary key,
  user_id uuid not null,
  conversation_id uuid not null,
  request_id uuid not null,
  scope jsonb not null,
  question_enc bytea not null,
  answer_enc bytea,
  sources_enc bytea,
  key_version int not null,
  answer_key_version int,
  sources_key_version int,
  status text not null default 'pending' check (status in ('pending','done','failed','cancelled')),
  error text,
  attempt int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (conversation_id, request_id),
  foreign key (conversation_id,user_id) references app.ask_conversations(id,user_id)
    on delete cascade
);
create index ask_conversations_updated on app.ask_conversations(user_id,profile_id,updated_at desc);
create index ask_messages_conversation on app.ask_messages(conversation_id,created_at);
create unique index ask_one_pending on app.ask_messages(conversation_id) where status='pending';
alter table app.ask_conversations enable row level security;
alter table app.ask_conversations force row level security;
create policy tenant_isolation on app.ask_conversations
  using (user_id=current_setting('app.user_id')::uuid)
  with check (user_id=current_setting('app.user_id')::uuid);
alter table app.ask_messages enable row level security;
alter table app.ask_messages force row level security;
create policy tenant_isolation on app.ask_messages
  using (user_id=current_setting('app.user_id')::uuid)
  with check (user_id=current_setting('app.user_id')::uuid);
grant select,insert,update,delete on app.ask_conversations,app.ask_messages
  to app_api,app_worker,app_system;
