import React, { useContext, useEffect, useRef, useState } from "react";
import {
  Alert,
  AppState,
  Pressable,
  Switch,
  View,
  useWindowDimensions,
} from "react-native";
import {
  AudioModule,
  RecordingPresets,
  setAudioModeAsync,
  useAudioPlayer,
  useAudioPlayerStatus,
  useAudioRecorder,
  useAudioRecorderState,
} from "expo-audio";
import * as FS from "expo-file-system/legacy";
import { randomUUID } from "expo-crypto";
import { Api } from "./api";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Icon,
  IconButton,
  Input,
  Loading,
  Notice,
  Row,
  Screen,
  ScreenTitle,
  Segments,
  Select,
  Theme,
  Txt,
  type Choice,
} from "./ui";
import {
  cleanupPracticeMedia,
  parsePracticeChoices,
  parsePracticeContext,
  parsePracticeHistory,
  parsePracticeSession,
  parsePracticeSpeech,
  parsePracticeTranscript,
  pollPractice,
  PracticeBusy,
  PracticeMediaQueue,
  PracticeScope,
  practiceOperation,
  type PracticeEntry,
  type PracticeOperation,
  type PracticeSession,
} from "./practiceState";

export type ImportedQuestion = {
  session_id: string;
  channel: "them" | "me";
  seg_id: number;
};
type Phase = "idle" | "preparing" | "recording" | "transcribing" | "speaking";
type Capture = { sessionId: string; questionId: string };
type AudioRetry = Capture & {
  body: { request_id: string; content_type: string; audio_base64: string };
};
const activeStates = ["preparing", "processing", "answering"];
const kinds: Choice[] = [
  { value: "behavioral", label: "Behavioral" },
  { value: "recruiter", label: "Recruiter conversation" },
  { value: "technical", label: "Technical" },
  { value: "system_design", label: "System design" },
];
const readable = (value: string) => value.replaceAll("_", " ");
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";

export function Practice({
  api,
  imported,
  visible = true,
}: {
  api: Api;
  imported?: ImportedQuestion;
  visible?: boolean;
}) {
  const colors = useContext(Theme),
    wide = useWindowDimensions().width >= 620;
  const [list, setList] = useState<PracticeEntry[]>([]),
    [session, setSession] = useState<PracticeSession | null>(null);
  const [section, setSection] = useState("new"),
    [historyCount, setHistoryCount] = useState(5),
    [historyLoading, setHistoryLoading] = useState(true),
    [useImported, setUseImported] = useState(!!imported);
  const lastImported = useRef(imported);
  const [topic, setTopic] = useState(""),
    [type, setType] = useState("behavioral"),
    [minutes, setMinutes] = useState("15"),
    [language, setLanguage] = useState(""),
    [application, setApplication] = useState("");
  const [languages, setLanguages] = useState<Choice[]>([]),
    [applications, setApplications] = useState<Choice[]>([]);
  const [context, setContext] = useState<
      { label: string; excerpt: string }[] | null
    >(null),
    [contextOpen, setContextOpen] = useState(false),
    [contextLoading, setContextLoading] = useState(true),
    [contextAttempt, setContextAttempt] = useState(0),
    [consent, setConsent] = useState(false);
  const [draft, setDraft] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState("");
  const [phase, setPhase] = useState<Phase>("idle"),
    [optionsOpen, setOptionsOpen] = useState(false),
    [expanded, setExpanded] = useState<string[]>([]),
    [audioRetry, setAudioRetry] = useState(false);
  const recorder = useAudioRecorder(RecordingPresets.HIGH_QUALITY),
    recording = useAudioRecorderState(recorder),
    player = useAudioPlayer(null),
    playback = useAudioPlayerStatus(player);
  const alive = useRef(true),
    foreground = useRef(visible && AppState.currentState === "active"),
    visibleRef = useRef(visible),
    pauseFlight = useRef<Promise<void>>(Promise.resolve()),
    current = useRef(session),
    phaseRef = useRef(phase);
  current.current = session;
  phaseRef.current = phase;
  visibleRef.current = visible;
  const profile = api.profile;
  const viewScope = useRef(new PracticeScope()),
    mediaScope = useRef(new PracticeScope()),
    resourceScope = useRef(new PracticeScope()),
    contextScope = useRef(new PracticeScope());
  const lock = useRef(new PracticeBusy()),
    files = useRef(new Set<string>()),
    mediaQueue = useRef(new PracticeMediaQueue());
  const operation = useRef<PracticeOperation | null>(null),
    setup = useRef({ fingerprint: "", id: "" }),
    drafts = useRef(new Map<string, string>());
  const capture = useRef<Capture | null>(null),
    pendingAudio = useRef<AudioRetry | null>(null),
    recordingSeen = useRef(false);
  const q = session?.questions.find(
    (item) => item.id === session.current_question_id,
  );
  const draftKey = (s: PracticeSession | null) =>
    s?.current_question_id ? `${s.id}/${s.current_question_id}` : "";
  const request = (
    path: string,
    body?: unknown,
    method = "GET",
    signal?: AbortSignal,
  ) => api.request(path, body, method, true, signal, true, profile);
  function begin(label: string) {
    const ticket = lock.current.begin();
    if (ticket !== null && alive.current) {
      setBusy(label);
      setError("");
      setNotice("");
    }
    return ticket;
  }
  function finish(ticket: number) {
    if (lock.current.finish(ticket) && alive.current) setBusy("");
  }
  function releaseBusy() {
    lock.current.invalidate();
    if (alive.current) setBusy("");
  }
  function setMediaPhase(next: Phase) {
    phaseRef.current = next;
    if (alive.current) setPhase(next);
  }
  function editDraft(value: string) {
    const key = draftKey(current.current);
    if (key) drafts.current.set(key, value);
    setDraft(value);
    operation.current = null;
  }
  const releaseAudio = () =>
    setAudioModeAsync({
      allowsRecording: false,
      playsInSilentMode: true,
      shouldPlayInBackground: false,
    });
  function clearMedia() {
    mediaScope.current.invalidate();
    capture.current = null;
    pendingAudio.current = null;
    recordingSeen.current = false;
    if (alive.current) setAudioRetry(false);
    setMediaPhase("idle");
    return mediaQueue.current.run(() =>
      cleanupPracticeMedia({
        pause: () => player.pause(),
        stop: () => recorder.stop(),
        discard: async () => {
          try {
            if (recorder.uri) files.current.add(recorder.uri);
          } catch {
            /* Native recorder may already be disposed. */
          }
          const pending = [...files.current];
          files.current.clear();
          await Promise.all(
            pending.map((uri) =>
              FS.deleteAsync(uri, { idempotent: true }).catch(() => {}),
            ),
          );
        },
        release: releaseAudio,
      }),
    );
  }
  function publish(next: PracticeSession) {
    if (
      draftKey(current.current) !== draftKey(next) ||
      current.current?.id !== next.id
    ) {
      void clearMedia();
      setDraft(drafts.current.get(draftKey(next)) ?? "");
      setOptionsOpen(false);
      operation.current = null;
    }
    current.current = next;
    setSession(next);
  }
  async function pauseAway(s: PracticeSession | null) {
    if (!s || !activeStates.includes(s.status)) return;
    try {
      await request(
        `/practice/sessions/${s.id}/actions`,
        { request_id: randomUUID(), action: "pause" },
        "POST",
      );
    } catch {
      /* Reopening refreshes the server state. */
    }
  }
  async function refresh() {
    const ticket = resourceScope.current.begin();
    try {
      const data = parsePracticeHistory(
        await request("/practice/sessions", undefined, "GET", ticket.signal),
      );
      if (alive.current && resourceScope.current.current(ticket)) setList(data);
    } catch (e) {
      if (alive.current && resourceScope.current.current(ticket))
        setError(message(e));
    } finally {
      if (alive.current && resourceScope.current.current(ticket))
        setHistoryLoading(false);
      resourceScope.current.finish(ticket);
    }
  }
  async function reload(id: string) {
    const ticket = viewScope.current.begin();
    try {
      const next = parsePracticeSession(
        await request(
          `/practice/sessions/${id}`,
          undefined,
          "GET",
          ticket.signal,
        ),
      );
      if (
        alive.current &&
        foreground.current &&
        viewScope.current.current(ticket) &&
        current.current?.id === id
      )
        publish(next);
    } catch (e) {
      if (alive.current && viewScope.current.current(ticket))
        setError(message(e));
    } finally {
      viewScope.current.finish(ticket);
    }
  }
  function updateVisibility(active: boolean) {
    if (foreground.current === active) return;
    foreground.current = active;
    if (!active) {
      viewScope.current.invalidate();
      void clearMedia();
      releaseBusy();
      pauseFlight.current = pauseAway(current.current);
      if (alive.current && current.current)
        setNotice(
          "Audio stopped while you were away. Practice will refresh when you return.",
        );
    } else {
      const id = current.current?.id;
      // Wait for the offscreen pause before showing resumable server state.
      void pauseFlight.current.then(() => {
        if (
          id &&
          alive.current &&
          foreground.current &&
          current.current?.id === id
        )
          void reload(id);
      });
    }
  }
  useEffect(() => {
    updateVisibility(visible && AppState.currentState === "active");
  }, [visible]);
  useEffect(() => {
    alive.current = true;
    foreground.current =
      visibleRef.current && AppState.currentState === "active";
    void refresh();
    const ticket = resourceScope.current.begin();
    void Promise.allSettled([
      request("/call-languages", undefined, "GET", ticket.signal),
      request(
        "/ask/context-options?kind=application",
        undefined,
        "GET",
        ticket.signal,
      ),
    ])
      .then((results) => {
        if (!alive.current || !resourceScope.current.current(ticket)) return;
        try {
          if (results[0].status === "fulfilled")
            setLanguages(parsePracticeChoices(results[0].value, "languages"));
          if (results[1].status === "fulfilled")
            setApplications(
              parsePracticeChoices(results[1].value, "applications"),
            );
          if (results.some((r) => r.status === "rejected"))
            setNotice(
              "Some setup choices could not load. Reopen Interview prep to try again; profile defaults are still available.",
            );
        } catch (e) {
          setError(message(e));
        }
      })
      .finally(() => resourceScope.current.finish(ticket));
    const subscription = AppState.addEventListener("change", (state) => {
      updateVisibility(state === "active" && visibleRef.current);
    });
    return () => {
      alive.current = false;
      foreground.current = false;
      resourceScope.current.invalidate();
      contextScope.current.invalidate();
      viewScope.current.invalidate();
      releaseBusy();
      subscription.remove();
      void clearMedia();
      void pauseAway(current.current);
    };
  }, [api, profile]);
  useEffect(() => {
    if (!imported || imported === lastImported.current) return;
    lastImported.current = imported;
    setUseImported(true);
    void choose().then(() => {
      if (!alive.current) return;
      setSection("new");
      setTopic("");
      setApplication("");
      setConsent(false);
    });
  }, [imported]);
  useEffect(() => {
    if (session || !visible) return;
    contextScope.current.invalidate();
    const ticket = contextScope.current.begin();
    setContext(null);
    setContextLoading(true);
    setConsent(false);
    void request(
      `/practice/context${application ? `?application_id=${encodeURIComponent(application)}` : ""}`,
      undefined,
      "GET",
      ticket.signal,
    )
      .then((value) => {
        if (alive.current && contextScope.current.current(ticket))
          setContext(parsePracticeContext(value));
      })
      .catch((e) => {
        if (alive.current && contextScope.current.current(ticket))
          setError(message(e));
      })
      .finally(() => {
        if (alive.current && contextScope.current.current(ticket))
          setContextLoading(false);
        contextScope.current.finish(ticket);
      });
    return () => contextScope.current.invalidate();
  }, [application, session?.id, api, profile, contextAttempt, visible]);
  useEffect(() => {
    if (!session || !["preparing", "processing"].includes(session.status))
      return;
    const id = session.id;
    return pollPractice(async () => {
      if (foreground.current && !lock.current.active) await reload(id);
    });
  }, [session?.id, session?.status, visible]);
  useEffect(() => {
    if (phaseRef.current !== "recording") return;
    if (recording.isRecording) recordingSeen.current = true;
    if (
      recording.durationMillis >= 240000 ||
      recording.mediaServicesDidReset ||
      (recordingSeen.current && !recording.isRecording)
    ) {
      void clearMedia();
      setError(
        recording.durationMillis >= 240000
          ? "Recording reached four minutes. Use a shorter response or type your answer."
          : "Recording was interrupted. Please record again or type your answer.",
      );
    }
  }, [
    recording.durationMillis,
    recording.isRecording,
    recording.mediaServicesDidReset,
  ]);
  useEffect(() => {
    if (phaseRef.current === "speaking" && playback.didJustFinish)
      void clearMedia();
  }, [playback.didJustFinish]);
  async function choose(id?: string) {
    const owner = begin("Opening practice…");
    if (owner === null) return;
    viewScope.current.invalidate();
    const ticket = viewScope.current.begin();
    try {
      await clearMedia();
      const previous = current.current;
      if (previous && previous.id !== id) await pauseAway(previous);
      if (!alive.current || !viewScope.current.current(ticket)) return;
      if (id) {
        const next = parsePracticeSession(
          await request(
            `/practice/sessions/${id}`,
            undefined,
            "GET",
            ticket.signal,
          ),
        );
        if (
          alive.current &&
          foreground.current &&
          viewScope.current.current(ticket)
        )
          publish(next);
      } else {
        current.current = null;
        setSession(null);
        setSection("history");
        setDraft("");
        operation.current = null;
        void refresh();
      }
    } catch (e) {
      if (alive.current && viewScope.current.current(ticket))
        setError(message(e));
    } finally {
      viewScope.current.finish(ticket);
      finish(owner);
    }
  }
  async function start() {
    const owner = begin("Preparing your practice…");
    if (owner === null) return;
    viewScope.current.invalidate();
    const ticket = viewScope.current.begin();
    try {
      const duration = Number(minutes);
      if (!Number.isInteger(duration) || duration < 3 || duration > 60)
        throw new Error("Choose a duration from 3 to 60 minutes.");
      if (!consent || context === null)
        throw new Error(
          "Review the context and allow saving responses before starting.",
        );
      const body = {
        topic: topic.trim() || "General interview preparation",
        interview_type: type,
        duration_minutes: duration,
        ...(application ? { application_id: application } : {}),
        ...(language ? { spoken_language: language } : {}),
        ...(useImported && imported ? { source_question: imported } : {}),
        retain_audio: false,
      };
      const fingerprint = JSON.stringify(body);
      if (setup.current.fingerprint !== fingerprint)
        setup.current = { fingerprint, id: randomUUID() };
      // Keep the response so a session created after leaving can be paused.
      const next = parsePracticeSession(
        await request(
          "/practice/sessions",
          { ...body, request_id: setup.current.id },
          "POST",
        ),
      );
      if (
        !alive.current ||
        !foreground.current ||
        !viewScope.current.current(ticket)
      ) {
        void pauseAway(next);
        return;
      }
      publish(next);
      setUseImported(false);
      setup.current = { fingerprint: "", id: "" };
      setExpanded([]);
    } catch (e) {
      if (alive.current && viewScope.current.current(ticket))
        setError(message(e));
    } finally {
      viewScope.current.finish(ticket);
      finish(owner);
    }
  }
  async function act(
    action: string,
    questionId = current.current?.current_question_id ?? undefined,
  ) {
    const s = current.current;
    if (!s) return;
    const owner = begin(
      action === "answer" ? "Saving your answer…" : "Updating practice…",
    );
    if (owner === null) return;
    viewScope.current.invalidate();
    const ticket = viewScope.current.begin();
    const input = {
      sessionId: s.id,
      action,
      ...(questionId ? { question_id: questionId } : {}),
      ...(action === "answer" ? { answer: draft } : {}),
    };
    const op = practiceOperation(operation.current, input, randomUUID);
    operation.current = op;
    try {
      await clearMedia();
      const next = parsePracticeSession(
        await request(
          `/practice/sessions/${s.id}/actions`,
          op.body,
          "POST",
          ticket.signal,
        ),
      );
      if (
        !alive.current ||
        !viewScope.current.current(ticket) ||
        current.current?.id !== s.id
      )
        return;
      operation.current = null;
      if (action === "answer") {
        drafts.current.delete(`${s.id}/${questionId}`);
        setDraft("");
      }
      publish(next);
    } catch (e) {
      if (alive.current && viewScope.current.current(ticket))
        setError(message(e));
    } finally {
      viewScope.current.finish(ticket);
      finish(owner);
    }
  }
  function mediaReady(
    ticket: ReturnType<PracticeScope["begin"]>,
    target: Capture,
  ) {
    return (
      alive.current &&
      foreground.current &&
      mediaScope.current.current(ticket) &&
      current.current?.id === target.sessionId &&
      current.current.current_question_id === target.questionId
    );
  }
  async function record() {
    const s = current.current;
    if (!s?.current_question_id || phaseRef.current === "recording") return;
    const owner = begin("Starting microphone…");
    if (owner === null) return;
    await clearMedia();
    const ticket = mediaScope.current.begin(),
      target = { sessionId: s.id, questionId: s.current_question_id };
    setMediaPhase("preparing");
    try {
      if (!mediaReady(ticket, target)) return;
      const permission = await AudioModule.requestRecordingPermissionsAsync();
      if (!permission.granted)
        throw new Error(
          "Microphone permission is unavailable. Type your answer or enable permission in device settings.",
        );
      if (!mediaReady(ticket, target)) return;
      await mediaQueue.current.run(async () => {
        if (!mediaReady(ticket, target)) return;
        await setAudioModeAsync({
          allowsRecording: true,
          playsInSilentMode: true,
          shouldPlayInBackground: false,
          interruptionMode: "doNotMix",
        });
        if (!mediaReady(ticket, target)) return;
        await recorder.prepareToRecordAsync();
        if (!mediaReady(ticket, target)) return;
        capture.current = target;
        recorder.record();
        setMediaPhase("recording");
      });
    } catch (e) {
      if (mediaReady(ticket, target)) {
        setError(message(e));
        await clearMedia();
      }
    } finally {
      mediaScope.current.finish(ticket);
      finish(owner);
    }
  }
  async function transcribe(retry = false) {
    const target = retry ? pendingAudio.current : capture.current;
    if (
      !target ||
      current.current?.id !== target.sessionId ||
      current.current.current_question_id !== target.questionId
    )
      return;
    const owner = begin("Transcribing your answer…");
    if (owner === null) return;
    const ticket = mediaScope.current.begin();
    setMediaPhase("transcribing");
    try {
      let payload = retry ? pendingAudio.current : null;
      if (!payload) {
        const uri = await mediaQueue.current.run(async () => {
          await recorder.stop();
          return recorder.uri;
        });
        if (!mediaReady(ticket, target)) return;
        if (!uri)
          throw new Error("No recording was captured. Please try again.");
        files.current.add(uri);
        const info = await FS.getInfoAsync(uri);
        if (!info.exists || info.size > 8 * 1024 * 1024)
          throw new Error(
            "Recording is unavailable or too large. Use a shorter answer.",
          );
        const audio = await FS.readAsStringAsync(uri, {
          encoding: FS.EncodingType.Base64,
        });
        await FS.deleteAsync(uri, { idempotent: true });
        files.current.delete(uri);
        await mediaQueue.current.run(async () => {
          if (mediaReady(ticket, target)) await releaseAudio();
        });
        if (!mediaReady(ticket, target)) return;
        payload = {
          ...target,
          body: {
            request_id: randomUUID(),
            content_type: "audio/mp4",
            audio_base64: audio,
          },
        };
        pendingAudio.current = payload;
      }
      const result = parsePracticeTranscript(
        await request(
          `/practice/sessions/${target.sessionId}/transcribe-json`,
          payload.body,
          "POST",
          ticket.signal,
        ),
      );
      if (!mediaReady(ticket, target)) return;
      pendingAudio.current = null;
      capture.current = null;
      setAudioRetry(false);
      if (result.text.trim()) {
        const key = `${target.sessionId}/${target.questionId}`,
          previous = drafts.current.get(key) ?? "";
        const combined = [previous.trim(), result.text.trim()]
          .filter(Boolean)
          .join("\n\n")
          .slice(0, 16000);
        drafts.current.set(key, combined);
        setDraft(combined);
        operation.current = null;
      }
      setNotice(
        result.silence
          ? "No speech was detected. Record again or type your answer."
          : "Your transcript is ready. Review it, then submit your answer.",
      );
    } catch (e) {
      if (mediaReady(ticket, target)) {
        setError(message(e));
        setAudioRetry(!!pendingAudio.current);
        if (!pendingAudio.current) await clearMedia();
      }
    } finally {
      if (mediaReady(ticket, target)) {
        setMediaPhase("idle");
        await mediaQueue.current
          .run(async () => {
            if (mediaReady(ticket, target)) await releaseAudio();
          })
          .catch(() => {});
      }
      mediaScope.current.finish(ticket);
      finish(owner);
    }
  }
  async function speak() {
    const s = current.current;
    if (!s?.current_question_id) return;
    const owner = begin("Preparing question audio…");
    if (owner === null) return;
    await clearMedia();
    const ticket = mediaScope.current.begin(),
      target = { sessionId: s.id, questionId: s.current_question_id };
    setMediaPhase("preparing");
    try {
      if (!mediaReady(ticket, target)) return;
      const encoded = parsePracticeSpeech(
        await request(
          `/practice/sessions/${s.id}/questions/${target.questionId}/speech-json`,
          undefined,
          "GET",
          ticket.signal,
        ),
      );
      if (!mediaReady(ticket, target)) return;
      if (!FS.cacheDirectory)
        throw new Error(
          "Audio cache is unavailable. You can read the question on screen.",
        );
      const uri = `${FS.cacheDirectory}practice-${randomUUID()}.mp3`;
      files.current.add(uri);
      await FS.writeAsStringAsync(uri, encoded, {
        encoding: FS.EncodingType.Base64,
      });
      if (!mediaReady(ticket, target)) {
        await FS.deleteAsync(uri, { idempotent: true }).catch(() => {});
        files.current.delete(uri);
        return;
      }
      await mediaQueue.current.run(async () => {
        if (mediaReady(ticket, target)) {
          player.replace({ uri });
          player.play();
          setMediaPhase("speaking");
        }
      });
    } catch (e) {
      if (mediaReady(ticket, target)) {
        setError(message(e));
        await clearMedia();
      }
    } finally {
      mediaScope.current.finish(ticket);
      finish(owner);
    }
  }
  async function remove() {
    const s = current.current;
    if (!s) return;
    const owner = begin("Deleting practice…");
    if (owner === null) return;
    viewScope.current.invalidate();
    const ticket = viewScope.current.begin();
    try {
      await clearMedia();
      await request(
        `/practice/sessions/${s.id}`,
        undefined,
        "DELETE",
        ticket.signal,
      );
      if (!alive.current || !viewScope.current.current(ticket)) return;
      for (const key of drafts.current.keys())
        if (key.startsWith(`${s.id}/`)) drafts.current.delete(key);
      current.current = null;
      setSession(null);
      setSection("history");
      setDraft("");
      operation.current = null;
      void refresh();
    } catch (e) {
      if (alive.current && viewScope.current.current(ticket))
        setError(message(e));
    } finally {
      viewScope.current.finish(ticket);
      finish(owner);
    }
  }
  const reviewedQuestions =
    session?.questions.filter(
      (item) =>
        item.attempts.length ||
        item.saved_for_later ||
        item.id !== session.current_question_id ||
        session.status === "finished",
    ) ?? [];
  const practiceStatus =
    session?.status === "finished"
      ? "Complete"
      : session
        ? readable(session.status)
        : "";
  const toggleFeedback = (id: string) =>
    setExpanded((previous) =>
      previous.includes(id)
        ? previous.filter((value) => value !== id)
        : [...previous, id],
    );
  return (
    <View style={{ flex: 1 }}>
      <Screen
        key={
          session
            ? `${session.id}:${session.current_question_id ?? ""}:${session.status}`
            : `${section}:${useImported && imported ? `${imported.session_id}:${imported.channel}:${imported.seg_id}` : "new"}`
        }
      >
        <ScreenTitle
          eyebrow="Interview prep"
          title={session ? "Your practice" : "Practice with purpose"}
          subtitle={
            session
              ? session.setup.topic
              : "Build confidence, one answer at a time."
          }
          action={
            session ? (
              <IconButton
                icon="history"
                label="Practice history"
                disabled={!!busy}
                onPress={() => void choose()}
              />
            ) : undefined
          }
        />
        {error ? <Notice tone="error">{error}</Notice> : null}
        {notice ? <Notice>{notice}</Notice> : null}
        {!session ? (
          <>
            <Segments
              value={section}
              options={[
                { value: "new", label: "New practice" },
                { value: "history", label: "Your sessions" },
              ]}
              onChange={setSection}
            />
            {section === "new" ? (
              <>
                {useImported && imported ? (
                  <Notice>
                    Start with the question you selected from your saved call,
                    then practice follow-ups.
                  </Notice>
                ) : null}
                <Card>
                  <View style={{ gap: 4 }}>
                    <Txt style={{ fontWeight: "700", fontSize: 19 }}>
                      Make it relevant
                    </Txt>
                    <Txt muted>Choose a focus or keep it open.</Txt>
                  </View>
                  <Field label="What would you like to practice?">
                    <Input
                      value={topic}
                      onChangeText={setTopic}
                      placeholder="e.g. Leadership stories or a product interview"
                      maxLength={500}
                      editable={!busy}
                    />
                  </Field>
                  <View
                    style={{ flexDirection: wide ? "row" : "column", gap: 16 }}
                  >
                    <View style={{ flex: 1 }}>
                      <Select
                        label="Interview style"
                        value={type}
                        options={kinds}
                        onChange={setType}
                        disabled={!!busy}
                      />
                    </View>
                    <View style={{ width: wide ? 155 : "100%" }}>
                      <Field label="Duration · minutes">
                        <Input
                          value={minutes}
                          onChangeText={setMinutes}
                          keyboardType="number-pad"
                          accessibilityLabel="Practice duration in minutes"
                          maxLength={2}
                          editable={!busy}
                        />
                      </Field>
                    </View>
                  </View>
                  <Select
                    label="Spoken language"
                    value={language}
                    options={[
                      { value: "", label: "Use profile language" },
                      ...languages,
                    ]}
                    onChange={setLanguage}
                    disabled={!!busy}
                  />
                  <Select
                    label="Application context"
                    value={application}
                    options={[
                      { value: "", label: "General profile context" },
                      ...applications,
                    ]}
                    onChange={setApplication}
                    disabled={!!busy}
                  />
                </Card>
                <Card subtle>
                  <Pressable
                    accessibilityRole="button"
                    accessibilityState={{ expanded: contextOpen }}
                    onPress={() => setContextOpen((value) => !value)}
                    style={{
                      minHeight: 48,
                      flexDirection: "row",
                      gap: 12,
                      alignItems: "center",
                    }}
                  >
                    <Icon name="file" />
                    <View style={{ flex: 1 }}>
                      <Txt style={{ fontWeight: "600" }}>
                        Context for your interview
                      </Txt>
                      <Txt muted style={{ fontSize: 12 }}>
                        {context
                          ? `${context.length} relevant source${context.length === 1 ? "" : "s"} · tap to review`
                          : contextLoading
                            ? "Finding relevant context…"
                            : "Context could not be loaded"}
                      </Txt>
                    </View>
                    <Icon name="chevron" />
                  </Pressable>
                  {!context && !contextLoading ? (
                    <Button
                      label="Retry loading context"
                      secondary
                      icon="refresh"
                      onPress={() => setContextAttempt((value) => value + 1)}
                    />
                  ) : null}
                  {contextOpen && context ? (
                    <>
                      {context.length ? (
                        context.map((source, index) => (
                          <View key={index} style={{ gap: 4 }}>
                            <Txt style={{ fontWeight: "600" }}>
                              {source.label}
                            </Txt>
                            <Txt muted>{source.excerpt}</Txt>
                          </View>
                        ))
                      ) : (
                        <Txt muted>
                          No saved sources yet. Practice can start from your
                          topic.
                        </Txt>
                      )}
                      <Txt muted style={{ fontSize: 12 }}>
                        Correct source records in Interview prep or Work before
                        starting.
                      </Txt>
                    </>
                  ) : null}
                  <View
                    style={{
                      flexDirection: "row",
                      alignItems: "center",
                      gap: 12,
                    }}
                  >
                    <Switch
                      accessibilityLabel="I reviewed the context and agree to save my practice responses and feedback"
                      value={consent}
                      onValueChange={setConsent}
                      disabled={!!busy}
                      trackColor={{ false: colors.line, true: colors.ruby }}
                    />
                    <View style={{ flex: 1 }}>
                      <Txt style={{ fontSize: 13 }}>
                        I reviewed the context. Save my responses and feedback.
                      </Txt>
                      <Txt muted style={{ fontSize: 12, lineHeight: 18 }}>
                        You can delete practice anytime. Raw audio is discarded
                        after transcription.
                      </Txt>
                    </View>
                  </View>
                </Card>
                <Button
                  label="Start practice"
                  icon="arrow"
                  onPress={() => void start()}
                  disabled={!consent || context === null}
                  loading={!!busy}
                />
              </>
            ) : (
              <>
                <Row style={{ justifyContent: "space-between" }}>
                  <Txt muted>
                    {list.length} saved session{list.length === 1 ? "" : "s"}
                  </Txt>
                  <IconButton
                    label="Refresh practice history"
                    icon="refresh"
                    disabled={!!busy}
                    onPress={() => void refresh()}
                  />
                </Row>
                {historyLoading ? (
                  <Loading label="Loading your sessions…" />
                ) : list.length ? (
                  <>
                    {list.slice(0, historyCount).map((item) => (
                      <Pressable
                        key={item.id}
                        accessibilityRole="button"
                        accessibilityLabel={`${item.setup.topic}, ${readable(item.status)}`}
                        disabled={!!busy}
                        onPress={() => void choose(item.id)}
                      >
                        <Card>
                          <Row style={{ flexWrap: "nowrap" }}>
                            <View style={{ flex: 1, gap: 8 }}>
                              <Txt style={{ fontWeight: "600", fontSize: 17 }}>
                                {item.setup.topic}
                              </Txt>
                              <Txt muted style={{ fontSize: 12 }}>
                                {item.setup.duration_minutes} minutes ·{" "}
                                {readable(item.status)}
                              </Txt>
                            </View>
                            <Icon name="arrow" />
                          </Row>
                        </Card>
                      </Pressable>
                    ))}
                    {historyCount < list.length ? (
                      <Button
                        label="Show more sessions"
                        secondary
                        onPress={() => setHistoryCount((value) => value + 5)}
                      />
                    ) : null}
                  </>
                ) : (
                  <EmptyState
                    icon="practice"
                    title="Your progress starts here"
                    description="Completed answers and feedback will appear here after your first practice."
                    action={
                      <Button
                        label="Start a new practice"
                        icon="plus"
                        onPress={() => setSection("new")}
                      />
                    }
                  />
                )}
              </>
            )}
          </>
        ) : (
          <>
            <Row style={{ justifyContent: "space-between" }}>
              <Badge
                label={practiceStatus}
                tone={session.status === "finished" ? "success" : "ruby"}
              />
              <Txt muted style={{ fontSize: 12 }}>
                {Math.floor(session.elapsed_seconds / 60)} /{" "}
                {session.setup.duration_minutes} min
              </Txt>
            </Row>
            {q ? (
              <Card>
                <Row style={{ justifyContent: "space-between" }}>
                  <Txt
                    style={{
                      color: colors.ruby,
                      fontSize: 11,
                      letterSpacing: 1.1,
                      fontWeight: "700",
                    }}
                  >
                    QUESTION{" "}
                    {session.questions.findIndex((item) => item.id === q.id) +
                      1}
                  </Txt>
                  <Badge
                    label={
                      q.kind === "hypothetical"
                        ? "Scenario"
                        : q.kind === "original"
                          ? "From your call"
                          : "Experience"
                    }
                  />
                </Row>
                <Txt
                  style={{ fontSize: 23, lineHeight: 32, fontWeight: "600" }}
                  selectable
                >
                  {q.text}
                </Txt>
                {q.hint ? <Notice>Hint: {q.hint}</Notice> : null}
                {q.sample ? <Notice>Coached example: {q.sample}</Notice> : null}
                {session.status === "answering" ? (
                  phase === "speaking" ? (
                    <Button
                      label="Stop reading"
                      secondary
                      icon="stop"
                      onPress={() => void clearMedia()}
                    />
                  ) : session.speech_available ? (
                    <Button
                      label="Read question aloud"
                      secondary
                      icon="headphones"
                      disabled={!!busy || phase === "recording"}
                      onPress={() => void speak()}
                    />
                  ) : (
                    <Txt muted style={{ fontSize: 12 }}>
                      Read the question on screen. Voice playback is unavailable
                      for this language.
                    </Txt>
                  )
                ) : null}
              </Card>
            ) : null}
            {session.status === "answering" ? (
              <>
                {phase === "recording" ? (
                  <Card
                    style={{
                      borderColor: colors.ruby,
                      backgroundColor: colors.tint,
                    }}
                  >
                    <Row>
                      <Icon name="mic" color={colors.ruby} />
                      <Txt style={{ color: colors.ruby, fontWeight: "700" }}>
                        Listening ·{" "}
                        {Math.floor(recording.durationMillis / 60000)}:
                        {String(
                          Math.floor(recording.durationMillis / 1000) % 60,
                        ).padStart(2, "0")}
                      </Txt>
                    </Row>
                    <Txt muted>
                      Take your time. We will turn your answer into text for you
                      to review.
                    </Txt>
                  </Card>
                ) : (
                  <Card>
                    <Field
                      label="Your answer"
                      hint="Use your own words. Review the text before submitting."
                    >
                      <Input
                        multiline
                        value={draft}
                        onChangeText={editDraft}
                        placeholder="Start with the situation, your action and the result…"
                        maxLength={16000}
                        style={{ minHeight: 160, lineHeight: 24 }}
                        editable={!busy}
                      />
                    </Field>
                    {phase === "transcribing" || phase === "preparing" ? (
                      <Loading label={busy || "Preparing audio…"} />
                    ) : null}
                    {!session.voice_available ? (
                      <Txt muted style={{ fontSize: 12 }}>
                        Microphone transcription is unavailable on this server
                        for this language. You can type your answer.
                      </Txt>
                    ) : null}
                    {audioRetry ? (
                      <Button
                        label="Retry transcription"
                        secondary
                        icon="refresh"
                        disabled={!!busy}
                        onPress={() => void transcribe(true)}
                      />
                    ) : null}
                    <Pressable
                      accessibilityRole="button"
                      accessibilityState={{ expanded: optionsOpen }}
                      onPress={() => setOptionsOpen((value) => !value)}
                      style={{ minHeight: 48, justifyContent: "center" }}
                    >
                      <Row>
                        <Icon name="more" />
                        <Txt muted>Question options</Txt>
                      </Row>
                    </Pressable>
                    {optionsOpen ? (
                      <Row>
                        {[
                          ["hint", "Give me a hint"],
                          ["sample", "Show an example"],
                          [
                            "save",
                            q?.saved_for_later
                              ? "Saved for later"
                              : "Save for later",
                          ],
                          ["skip", "Skip question"],
                        ].map(([action, label]) => (
                          <Button
                            key={action}
                            label={label}
                            secondary
                            compact
                            disabled={!!busy}
                            onPress={() => void act(action)}
                          />
                        ))}
                      </Row>
                    ) : null}
                  </Card>
                )}
                <Row>
                  <Button
                    label="Pause"
                    secondary
                    disabled={!!busy}
                    onPress={() => void act("pause")}
                  />
                  <Button
                    label="Finish practice"
                    secondary
                    disabled={!!busy}
                    onPress={() => void act("finish")}
                  />
                </Row>
              </>
            ) : session.status === "paused" ? (
              <Card>
                <Txt style={{ fontWeight: "600", fontSize: 18 }}>
                  Take a breath
                </Txt>
                <Txt muted>
                  Your completed turns are saved. Any draft stays here while
                  this screen is open.
                </Txt>
                <Button
                  label="Resume practice"
                  icon="arrow"
                  loading={!!busy}
                  onPress={() => void act("resume")}
                />
                <Button
                  label="Finish practice"
                  secondary
                  disabled={!!busy}
                  onPress={() => void act("finish")}
                />
              </Card>
            ) : ["preparing", "processing"].includes(session.status) ? (
              <Card>
                <Loading
                  label={
                    session.status === "preparing"
                      ? "Preparing your first question…"
                      : "Reflecting on your answer…"
                  }
                />
                <Txt muted>
                  We'll bring your feedback and next question here.
                </Txt>
                <Button
                  label="Pause"
                  secondary
                  disabled={!!busy}
                  onPress={() => void act("pause")}
                />
              </Card>
            ) : session.status === "failed" ? (
              <Notice
                tone="error"
                action={
                  <Button
                    label="Retry generation"
                    icon="refresh"
                    loading={!!busy}
                    onPress={() => void act("retry_generation")}
                  />
                }
              >
                {session.error ||
                  "Your question could not be prepared. You can retry without losing your answers."}
              </Notice>
            ) : (
              <Card>
                <Row>
                  <Icon name="check" color={colors.success} />
                  <Txt style={{ fontSize: 19, fontWeight: "600" }}>
                    Practice complete
                  </Txt>
                </Row>
                <Txt muted>
                  Review what worked, then try a question again to build on your
                  feedback.
                </Txt>
                <Button
                  label="New practice"
                  icon="plus"
                  onPress={() => {
                    void choose().then(() => {
                      if (alive.current) setSection("new");
                    });
                  }}
                  disabled={!!busy}
                />
              </Card>
            )}
            {reviewedQuestions.length > 0 ? (
              <View style={{ gap: 12 }}>
                <Txt style={{ fontSize: 18, fontWeight: "600" }}>
                  Your answers & feedback
                </Txt>
                {reviewedQuestions.map((item) => (
                  <Card key={item.id}>
                    <Pressable
                      accessibilityRole="button"
                      accessibilityState={{
                        expanded: expanded.includes(item.id),
                      }}
                      onPress={() => toggleFeedback(item.id)}
                      style={{
                        minHeight: 48,
                        flexDirection: "row",
                        gap: 12,
                        alignItems: "center",
                      }}
                    >
                      <View style={{ flex: 1, gap: 5 }}>
                        <Txt
                          style={{ fontWeight: "600" }}
                          numberOfLines={
                            expanded.includes(item.id) ? undefined : 2
                          }
                        >
                          {item.text}
                        </Txt>
                        <Txt muted style={{ fontSize: 12 }}>
                          {item.attempts.length} attempt
                          {item.attempts.length === 1 ? "" : "s"}
                          {item.saved_for_later ? " · Saved for later" : ""}
                        </Txt>
                      </View>
                      <Icon name="chevron" />
                    </Pressable>
                    {expanded.includes(item.id) ? (
                      <>
                        <Row>
                          <Button
                            label={
                              item.saved_for_later
                                ? "Saved for later"
                                : "Save for later"
                            }
                            secondary
                            compact
                            disabled={!!busy}
                            onPress={() => void act("save", item.id)}
                          />
                          <Button
                            label="Try again"
                            secondary
                            compact
                            icon="refresh"
                            disabled={
                              !!busy ||
                              ["preparing", "processing"].includes(
                                session.status,
                              )
                            }
                            onPress={() => void act("retry", item.id)}
                          />
                        </Row>
                        {item.attempts.map((attempt, index) => (
                          <View
                            key={attempt.id}
                            style={{
                              gap: 10,
                              paddingTop: 14,
                              borderTopWidth: 1,
                              borderColor: colors.line,
                            }}
                          >
                            <Txt style={{ fontWeight: "600" }}>
                              Attempt {index + 1} ·{" "}
                              {attempt.assistance.length
                                ? attempt.assistance.join(", ")
                                : "Independent"}
                            </Txt>
                            <Txt selectable>{attempt.answer}</Txt>
                            {attempt.feedback ? (
                              <>
                                <Row>
                                  {Object.entries(
                                    attempt.feedback.dimensions,
                                  ).map(([key, value]) => (
                                    <Badge
                                      key={key}
                                      label={`${readable(key)} ${value}/4`}
                                    />
                                  ))}
                                </Row>
                                <Txt muted style={{ fontSize: 12 }}>
                                  Rubric {attempt.feedback.rubric_version}: 0
                                  not demonstrated · 4 strong.
                                </Txt>
                                {[
                                  ["What worked", attempt.feedback.strengths],
                                  ["Try next", attempt.feedback.improvements],
                                  ["Evidence", attempt.feedback.excerpts],
                                  ["Limits", attempt.feedback.uncertainty],
                                ].map(([label, values]) =>
                                  (values as string[]).length ? (
                                    <View
                                      key={label as string}
                                      style={{ gap: 4 }}
                                    >
                                      <Txt style={{ fontWeight: "600" }}>
                                        {label as string}
                                      </Txt>
                                      {(values as string[]).map((value, i) => (
                                        <Txt key={i} muted>
                                          {value}
                                        </Txt>
                                      ))}
                                    </View>
                                  ) : null,
                                )}
                                {attempt.feedback.suggested_wording ? (
                                  <Notice>
                                    Suggested wording:{" "}
                                    {attempt.feedback.suggested_wording}
                                  </Notice>
                                ) : null}
                              </>
                            ) : (
                              <Txt muted>
                                Feedback is not available for this attempt yet.
                              </Txt>
                            )}
                            {attempt.comparison ? (
                              <View style={{ gap: 7 }}>
                                <Txt style={{ fontWeight: "600" }}>
                                  Compared with your previous attempt
                                </Txt>
                                <Txt muted>
                                  {attempt.comparison.note}{" "}
                                  {attempt.comparison.comparable
                                    ? "Comparable coaching conditions."
                                    : "Coaching differs."}
                                </Txt>
                                {attempt.comparison.summary ? (
                                  <Txt>{attempt.comparison.summary}</Txt>
                                ) : null}
                                {[
                                  ["Improved", attempt.comparison.improvements],
                                  ["Next", attempt.comparison.remaining_gaps],
                                  [
                                    "Recurring",
                                    attempt.comparison.recurring_gaps,
                                  ],
                                  [
                                    "Current evidence",
                                    attempt.comparison.evidence.current,
                                  ],
                                  [
                                    "Previous evidence",
                                    attempt.comparison.evidence.previous,
                                  ],
                                ].map(([label, values]) =>
                                  (values as string[]).map((value, i) => (
                                    <Txt key={`${label}-${i}`} muted>
                                      {label as string}: {value}
                                    </Txt>
                                  )),
                                )}
                                {Object.entries(
                                  attempt.comparison.dimensions,
                                ).map(([key, value]) => (
                                  <Txt key={key} muted>
                                    {readable(key)}: {value.previous} →{" "}
                                    {value.current}
                                  </Txt>
                                ))}
                              </View>
                            ) : null}
                          </View>
                        ))}
                      </>
                    ) : null}
                  </Card>
                ))}
              </View>
            ) : null}
            {session.error && session.status !== "failed" ? (
              <Notice tone="error">{session.error}</Notice>
            ) : null}
            <Txt muted style={{ fontSize: 12, lineHeight: 18 }}>
              {session.retention_notice}
            </Txt>
            <Button
              label="Delete this practice"
              secondary
              danger
              disabled={!!busy}
              onPress={() =>
                Alert.alert(
                  "Delete practice?",
                  "This removes the saved responses, feedback and generated questions.",
                  [
                    { text: "Cancel", style: "cancel" },
                    {
                      text: "Delete",
                      style: "destructive",
                      onPress: () => void remove(),
                    },
                  ],
                )
              }
            />
          </>
        )}
      </Screen>
      {session?.status === "answering" ? (
        <View
          style={{
            borderTopWidth: 1,
            borderColor: colors.line,
            backgroundColor: colors.card,
            paddingHorizontal: 22,
            paddingVertical: 12,
          }}
        >
          <View
            style={{
              flexDirection: "row",
              gap: 10,
              width: "100%",
              maxWidth: 816,
              alignSelf: "center",
            }}
          >
            {phase === "recording" ? (
              <>
                <Button
                  label="I'm done · transcribe"
                  icon="check"
                  style={{ flex: 1 }}
                  loading={!!busy}
                  onPress={() => void transcribe()}
                />
                <IconButton
                  label="Discard recording"
                  icon="close"
                  disabled={!!busy}
                  onPress={() => void clearMedia()}
                />
              </>
            ) : (
              <>
                <Button
                  label="Submit answer"
                  icon="arrow"
                  style={{ flex: 1 }}
                  disabled={!draft.trim() || !!busy}
                  loading={busy === "Saving your answer…"}
                  onPress={() => void act("answer")}
                />
                {session.voice_available ? (
                  <Button
                    label="Record"
                    icon="mic"
                    secondary
                    disabled={!!busy}
                    onPress={() => void record()}
                  />
                ) : null}
              </>
            )}
          </View>
        </View>
      ) : null}
    </View>
  );
}
