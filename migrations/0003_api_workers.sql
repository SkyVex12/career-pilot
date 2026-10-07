-- 0003_api_workers: what the Copilot API and the workers need (build step 4a).
-- Source: ARCHITECTURE.md, "2. Data model" and "3. Services and prompts" (REST API, Workers).
--
-- Expand only: new nullable columns and a new constraint, so code from
-- step 3 keeps working against this schema.

set local role app_owner;

-- A proposal's session is a same-user reference like every other one (composite on
-- (id, user_id)). Deleting a session deletes its proposals with the rest of its log;
-- approved facts are memory nodes by then and stay.
-- Validating the new key scans both tables as app_owner, and forced row-level security
-- would apply to that scan too (and fail without app.user_id, or check nothing with a dummy
-- one). Lift `force` for the length of this transaction only, so every existing row is
-- checked; the transaction restores it before anything else can run.
alter table app.memory_proposals no force row level security;
alter table app.sessions no force row level security;
alter table app.memory_proposals
  add constraint memory_proposals_session_fk
    foreign key (session_id, user_id) references app.sessions(id, user_id) on delete cascade;
alter table app.memory_proposals force row level security;
alter table app.sessions force row level security;
create index memory_proposals_session on app.memory_proposals (session_id)
  where session_id is not null;

-- Set by session.postcall when the outputs are written; GET /sessions/{id} reports the
-- session as ready from then on.
alter table app.sessions add column outputs_ready_at timestamptz;

-- What the client said it uploads (application/pdf, text/plain, text/markdown), so the
-- worker knows how to hand the document to Claude without guessing from bytes.
alter table app.documents add column content_type text;

