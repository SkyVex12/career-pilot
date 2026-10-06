-- 0002_question_text_optional: question text is stored only with transcript consent.
-- Source: ARCHITECTURE.md, "Session logging" (consent) and "Schema DDL".
--
-- A question is the other person's words, so when a session's store_transcript is off the
-- log keeps only its metadata (type, timings, tokens, cost, latency, memory node ids,
-- feedback). answers.text_enc and key_version are already nullable. Text and key version
-- are both present or both absent.

set local role app_owner;

alter table app.questions
  alter column text_enc drop not null,
  alter column key_version drop not null,
  add constraint questions_text_and_key_together
    check ((text_enc is null) = (key_version is null));

alter table app.answers
  add constraint answers_text_and_key_together
    check ((text_enc is null) = (key_version is null));
