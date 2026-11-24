-- Child lookups used by history, transcript flushes and FK/cascade checks.
-- Keep the existing sessions_user_page index from 0028.
set local role app_owner;

create index questions_session_lookup
  on app.questions(session_id, user_id, detected_at, id);
create index answers_question_lookup
  on app.answers(question_id, user_id, created_at, id);
create index session_outputs_session_lookup
  on app.session_outputs(session_id, user_id, kind);
