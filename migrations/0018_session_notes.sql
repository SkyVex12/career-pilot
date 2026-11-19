-- Personal call-review notes share the session's owner, RLS policy and deletion lifecycle.
-- Notes are independent of transcript retention; only explicitly saved text is retained.
alter table app.sessions
  add column notes_enc bytea,
  add column notes_key_version integer,
  add constraint session_notes_key_pair check
    ((notes_enc is null) = (notes_key_version is null));
