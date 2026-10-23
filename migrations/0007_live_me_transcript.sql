-- 0007_live_me_transcript: the user's own words are transcribed live by the gateway, so a new
-- session no longer waits for an audio upload after the call.
-- Source: ARCHITECTURE.md, "Desktop client" (user's own audio) and "Session logging".
--
-- Expand only: one more allowed value and a new default. Sessions from before keep their
-- status ('pending', 'uploaded', 'transcribed', 'none'); a new one starts as 'live', meaning
-- its "me" segments came from the live stream, and nothing changes it after that.

set local role app_owner;

alter table app.sessions
  drop constraint sessions_me_audio_status_check,
  add constraint sessions_me_audio_status_check
    check (me_audio_status in ('pending','uploaded','transcribed','none','live')),
  alter column me_audio_status set default 'live';
