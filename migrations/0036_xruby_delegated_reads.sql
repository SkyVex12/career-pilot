-- Bounded XRuby read projections use existing RLS and immutable source revisions.
-- This adds access paths only; no grants, source rewriting or new ownership inference.
set local role app_owner;
create index xruby_interviews_next on app.interviews(user_id,starts_at,id);
create index xruby_work_items_open on app.memory_nodes(user_id,employer_id,id)
 where status='active' and kind in ('work_item','commitment')
 and coalesce(body->>'state','unknown') not in ('done','dropped');
create index xruby_feedback_window on app.session_outputs(user_id,created_at desc,id desc,session_id)
 where kind='feedback';
