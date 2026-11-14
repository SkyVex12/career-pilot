-- 0016_engineering_context: a company's engineering context. Every uploaded company document
-- is kept as searchable sections (`app.knowledge_chunks`) next to the facts extracted from
-- it, and workplace memory gains three kinds for what an architecture document holds.
-- Source: ARCHITECTURE.md, "Memory system" (engineering context) and "Schema DDL".
--
-- Why: until now a document became only one-line facts, and 6% of the words uploaded
-- survived (docs/analysis/2026-11-14-standup-technical.md). The text itself was never
-- searched.
--
-- A section's text, its heading path and its file's name are the company's confidential
-- words: sealed with the user's data key like a work log (`body_enc`, associated data: this
-- table and the row id). The database can read nothing of a section but its vector. Keyword
-- search runs in the gateway's memory, over the copy a worker opens into Valkey for live
-- calls (`kb:{uid}:{employer}`, like a session pack); the vector half runs here and returns
-- ids only.
--
-- Expand only: a new table, new nullable or defaulted columns, a wider kind check. Code from
-- before this migration never reads the table or writes the new kinds.

set local role app_owner;

create table app.knowledge_chunks (
  id              uuid primary key,
  user_id         uuid not null references app.users(id) on delete cascade,
  employer_id     uuid not null,
  document_id     uuid not null,
  ord             int  not null,                 -- its place in the document
  body_enc        bytea not null,                -- sealed JSON: {"doc", "path", "text"}
  key_version     int  not null,
  embedding       vector(1024),
  embedding_model text,
  created_at      timestamptz not null default now(),
  unique (document_id, ord),
  check ((embedding is null) = (embedding_model is null)),
  foreign key (employer_id, user_id) references app.employers(id, user_id) on delete cascade,
  foreign key (document_id, user_id)
    references app.company_documents(id, user_id) on delete cascade
);
create index knowledge_chunks_employer on app.knowledge_chunks (user_id, employer_id);

alter table app.knowledge_chunks enable row level security;
alter table app.knowledge_chunks force row level security;
create policy tenant_isolation on app.knowledge_chunks
  using (user_id = current_setting('app.user_id')::uuid)
  with check (user_id = current_setting('app.user_id')::uuid);

grant select, insert, update, delete on app.knowledge_chunks to app_api, app_worker, app_system;
-- The gateway reads ids and vectors for live search (it cannot open `body_enc`: it has no
-- grant on the data keys).
grant select on app.knowledge_chunks to app_gateway;

-- A document's index: the content it was built from (so indexing runs once per content),
-- how many sections it has, why it failed, and the earlier upload of the same name it
-- replaces once it is indexed.
alter table app.company_documents add column indexed_sha256 text;
alter table app.company_documents add column chunk_count int not null default 0;
alter table app.company_documents add column error text;
alter table app.company_documents add column replaces uuid;

-- Three more kinds of workplace memory: a data flow (its steps in the body), a constraint
-- (a limit the design must keep) and an open risk. A component stays a `system`, a glossary
-- entry a `term`, and a decision gains its reason, rejected alternatives and tradeoff in its
-- body. Dropping and adding the check validates existing rows as app_owner under forced
-- row-level security; lift `force` for this transaction only, as 0006 does.
alter table app.memory_nodes no force row level security;
alter table app.memory_nodes drop constraint memory_nodes_kind_check;
alter table app.memory_nodes
  add constraint memory_nodes_kind_check check (kind in
    ('profile','role','project','story','skill','work_item','commitment','company','person',
     'decision','system','term','flow','constraint','risk'));
alter table app.memory_nodes force row level security;
