import { assistantAudio } from "./assistantAudio";
import React, { useEffect, useRef, useState } from "react";
import { Alert, AppState, Switch, View } from "react-native";
import type { AudioRecorder } from "react-native-audio-api";
import { randomUUID } from "expo-crypto";
import { withDeadline } from "./requestScope";
import { Api } from "./api";
import { HostAssistants } from "./HostAssistants";
import { MediaEpoch } from "./mediaEpoch";
import { pcmFrame, sessionURL } from "./pcm";
import {
  emptyLive,
  event,
  languageAcknowledgement,
  type Event,
} from "./liveState";
import { parsePracticeChoices } from "./practiceState";
import { loadCallLanguages } from "./callLanguages";
import type { Choice } from "./choiceState";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Notice,
  Row,
  Screen,
  ScreenTitle,
  Select,
  Segments,
  Txt,
} from "./ui";
const NativeWebSocket = WebSocket as unknown as {
  new (
    url: string,
    protocols?: string | string[],
    options?: { headers: Record<string, string> },
  ): WebSocket;
};
/** Foreground, user-started in-person mic only; never joins a desktop capture session. */
export function InPerson({
  api,
  url,
  initialInterviewId,
}: {
  api: Api;
  url: string;
  initialInterviewId?: string;
}) {
  const [status, setStatus] = useState("Microphone off"),
    [error, setError] = useState(""),
    [live, setLive] = useState(emptyLive),
    [active, setActive] = useState(false),
    [connecting, setConnecting] = useState(false),
    [micBusy, setMicBusy] = useState(false),
    [recording, setRecording] = useState(false),
    [channel, setChannel] = useState<0 | 1>(0),
    [retain, setRetain] = useState(false),
    [title, setTitle] = useState("In-person meeting"),
    [mode, setMode] = useState<"interview" | "meeting">("meeting"),
    [application, setApplication] = useState(""),
    [interviewId, setInterviewId] = useState(""),
    [interviewLoading, setInterviewLoading] = useState(false),
    [interviewError, setInterviewError] = useState(""),
    [interviewRevision, setInterviewRevision] = useState(0),
    [company, setCompany] = useState(""),
    [agenda, setAgenda] = useState(""),
    [answerStyle, setAnswerStyle] = useState("bullets"),
    [applications, setApplications] = useState<Choice[]>([]),
    [companies, setCompanies] = useState<Choice[]>([]),
    [contextError, setContextError] = useState(""),
    [contextRevision, setContextRevision] = useState(0),
    [draft, setDraft] = useState(""),
    [language, setLanguage] = useState(""),
    [languages, setLanguages] = useState<Choice[]>([]),
    [languagesLoading, setLanguagesLoading] = useState(true),
    [languageChanging, setLanguageChanging] = useState(false),
    [languageError, setLanguageError] = useState(""),
    [languageRevision, setLanguageRevision] = useState(0);
  const liveRef = useRef(emptyLive());
  const ws = useRef<WebSocket | null>(null),
    recorder = useRef<AudioRecorder | null>(null),
    seq = useRef([0, 0]),
    started = useRef(0),
    micChannel = useRef<0 | 1>(0),
    meAudio = useRef(false),
    alive = useRef(true),
    starting = useRef(false),
    sid = useRef<string | null>(null),
    last = useRef(0),
    wantMic = useRef(false),
    media = useRef(new MediaEpoch()),
    audioModule = useRef<typeof import("react-native-audio-api") | null>(null),
    stopping = useRef<Promise<void> | null>(null),
    capturing = useRef(false),
    connectionEpoch = useRef(0),
    handshake = useRef<ReturnType<typeof setTimeout> | null>(null);
  const languageRequest = useRef<{
    id: string;
    timer: ReturnType<typeof setTimeout>;
  } | null>(null);
  const lastLanguageRequestId = useRef<string | null>(null);
  const scheduledApplication = useRef<Choice | null>(null);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setContextError("");
    void Promise.allSettled(
      ["application", "company"].map((kind) =>
        api
          .request(
            `/ask/context-options?kind=${kind}`,
            undefined,
            "GET",
            true,
            controller.signal,
          )
          .then((value) => parsePracticeChoices(value, "applications")),
      ),
    ).then((results) => {
      if (!active) return;
      if (results[0].status === "fulfilled") {
        const choices = results[0].value,
          selected = scheduledApplication.current;
        setApplications(
          selected && !choices.some((choice) => choice.value === selected.value)
            ? [...choices, selected]
            : choices,
        );
      }
      if (results[1].status === "fulfilled") setCompanies(results[1].value);
      if (results.some((result) => result.status === "rejected"))
        setContextError(
          "Some call context could not be loaded. You can still start with your profile context.",
        );
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [api, contextRevision]);
  useEffect(() => {
    if (!initialInterviewId || sid.current || starting.current) return;
    let current = true;
    const controller = new AbortController();
    setInterviewLoading(true);
    setInterviewError("");
    setMode("interview");
    setInterviewId("");
    scheduledApplication.current = null;
    void withDeadline(
      (signal) =>
        api.request<{ interviews: unknown[] }>(
          "/interviews",
          undefined,
          "GET",
          true,
          signal,
        ),
      15000,
      controller.signal,
    )
      .then((value) => {
        if (!current) return;
        const selected = Array.isArray(value.interviews)
          ? (value.interviews.find((i: any) => i?.id === initialInterviewId) as
              Record<string, unknown> | undefined)
          : undefined;
        if (!selected || typeof selected.job_id !== "string")
          throw new Error(
            "This scheduled interview is no longer available. Refresh Applications or choose a general interview.",
          );
        setInterviewId(initialInterviewId);
        setApplication(selected.job_id);
        const label = [selected.company, selected.title]
          .filter((v): v is string => typeof v === "string" && !!v.trim())
          .join(" · ");
        setTitle(label || "Scheduled interview");
        scheduledApplication.current = {
          value: selected.job_id,
          label: label || "Scheduled interview application",
        };
        setApplications((choices) =>
          choices.some((c) => c.value === selected.job_id)
            ? choices
            : [
                ...choices,
                scheduledApplication.current!,
              ],
        );
      })
      .catch((e) => {
        if (current)
          setInterviewError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (current) setInterviewLoading(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [api, initialInterviewId, interviewRevision]);
  function finishLanguageChange() {
    if (languageRequest.current) clearTimeout(languageRequest.current.timer);
    languageRequest.current = null;
    if (alive.current) setLanguageChanging(false);
  }
  function changeLiveLanguage(value: string) {
    if (
      !liveRef.current.languageSwitching ||
      recording ||
      micBusy ||
      languageRequest.current ||
      !languages.some((choice) => choice.value === value) ||
      !sid.current ||
      value === language
    )
      return;
    const id = randomUUID();
    if (
      !send({
        type: "session.set_language",
        spoken_language: value,
        request_id: id,
      })
    ) {
      setError("Reconnect this session before changing its language.");
      return;
    }
    setLanguageChanging(true);
    lastLanguageRequestId.current = id;
    setError("");
    languageRequest.current = {
      id,
      timer: setTimeout(() => {
        if (languageRequest.current?.id !== id) return;
        finishLanguageChange();
        if (alive.current)
          setError(
            "The language change was not confirmed. Check the current language and retry when connected.",
          );
      }, 15000),
    };
  }
  function send(value: object): boolean {
    try {
      if (ws.current?.readyState === 1) {
        ws.current.send(JSON.stringify(value));
        return true;
      }
    } catch (e) {
      if (alive.current) setError(String(e));
    }
    return false;
  }
  function stopMic(): Promise<void> {
    media.current.stop();
    wantMic.current = false;
    if (stopping.current) return stopping.current;
    const r = recorder.current;
    recorder.current = null;
    const socket = ws.current,
      ch = micChannel.current;
    const operation = (async () => {
      if (r) {
        try {
          r.clearOnAudioReady();
          await withDeadline(() => r.stop(), 5000);
        } catch (e) {
          if (alive.current) setError(`Microphone interrupted: ${String(e)}`);
        } finally {
          try {
            r.clearOnError();
          } catch {
            /* Native recorder may already be released. */
          }
          await audioModule.current?.AudioManager.setAudioSessionActivity(
            false,
          ).catch(() => {});
        }
        try {
          if (socket && socket === ws.current && socket.readyState === 1)
            socket.send(
              pcmFrame(
                new Float32Array(),
                ch,
                2,
                seq.current[ch]++,
                Date.now() - started.current,
              ),
            );
        } catch (e) {
          if (alive.current) setError(`Connection interrupted: ${String(e)}`);
        }
      }
      if (alive.current) {
        setRecording(false);
        setStatus(
          sid.current ? "Session open - microphone off" : "Microphone off",
        );
      }
    })().catch((e) => {
      if (alive.current) setError(`Microphone cleanup failed: ${String(e)}`);
    });
    stopping.current = operation;
    void operation.finally(() => {
      if (stopping.current === operation) stopping.current = null;
    });
    return operation;
  }
  async function end(summarize = true) {
    finishLanguageChange();
    connectionEpoch.current++;
    starting.current = false;
    if (alive.current) setConnecting(false);
    if (handshake.current) clearTimeout(handshake.current);
    await stopMic();
    send({ type: "session.end", summarize });
    sid.current = null;
    setActive(false);
    setStatus("Session ended");
    const socket = ws.current;
    ws.current = null;
    socket?.close();
  }
  useEffect(() => {
    let current = true;
    const profile = api.profile;
    setLanguagesLoading(true);
    setLanguageError("");
    void loadCallLanguages((path) => api.request(path))
      .then((result) => {
        if (!current || profile !== api.profile) return;
        setLanguages(result.choices);
        setLanguage(result.selected);
        setLanguageError(result.warning);
      })
      .catch((e) => {
        if (current && profile === api.profile) setLanguageError(String(e));
      })
      .finally(() => {
        if (current && profile === api.profile) setLanguagesLoading(false);
      });
    return () => {
      current = false;
    };
  }, [api, languageRevision]);
  useEffect(() => {
    alive.current = true;
    const sub = AppState.addEventListener("change", (state) => {
      if (state !== "active") {
        finishLanguageChange();
        connectionEpoch.current++;
        starting.current = false;
        if (alive.current) setConnecting(false);
        if (handshake.current) clearTimeout(handshake.current);
        const previous = ws.current;
        ws.current = null;
        previous?.close();
        void stopMic();
        setError(
          "Microphone stopped when the app left the foreground. Tap record to resume explicitly.",
        );
      }
    });
    return () => {
      alive.current = false;
      finishLanguageChange();
      connectionEpoch.current++;
      starting.current = false;
      if (alive.current) setConnecting(false);
      if (handshake.current) clearTimeout(handshake.current);
      sub.remove();
      void stopMic().finally(() => {
        send({ type: "session.end", summarize: false });
        ws.current?.close();
        ws.current = null;
      });
    };
  }, [api]);
  async function capture() {
    if (
      recorder.current ||
      capturing.current ||
      stopping.current ||
      AppState.currentState !== "active" ||
      !sid.current ||
      ws.current?.readyState !== 1
    )
      return;
    capturing.current = true;
    setMicBusy(true);
    const ticket = media.current.begin();
    const captureSocket = ws.current;
    try {
      const native =
        audioModule.current ?? (await import("react-native-audio-api"));
      audioModule.current = native;
      const { AudioManager, AudioRecorder } = native;
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        captureSocket !== ws.current ||
        AppState.currentState !== "active"
      )
        return;
      if ((await AudioManager.requestRecordingPermissions()) !== "Granted")
        throw new Error(
          "Microphone permission denied. You can still type questions.",
        );
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      )
        return;
      // Finish any assistant playback teardown before taking ownership for capture.
      await assistantAudio.quiesce();
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      )
        return;
      AudioManager.setSystemOptions({
        ios: { category: "record", mode: "default", options: [] },
      });
      await AudioManager.setAudioSessionActivity(true);
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      ) {
        await AudioManager.setAudioSessionActivity(false);
        return;
      }
      const r = new AudioRecorder();
      recorder.current = r;
      micChannel.current = channel;
      let first = true;
      wantMic.current = true;
      r.onError((e) => {
        if (!alive.current || recorder.current !== r) return;
        setError(`Microphone interrupted: ${e.message}`);
        void stopMic();
      });
      const result = r.onAudioReady(
        { sampleRate: 16000, bufferLength: 1600, channelCount: 1 },
        ({ buffer }) => {
          if (
            !wantMic.current ||
            captureSocket !== ws.current ||
            ws.current?.readyState !== 1
          )
            return;
          try {
            if (buffer.sampleRate !== 16000) {
              setError(
                "This audio route did not provide 16 kHz audio. Microphone stopped; use text.",
              );
              void stopMic();
              return;
            }
            if (ws.current.bufferedAmount > 128000) {
              setError(
                "Network is too slow. Microphone stopped to avoid delayed audio.",
              );
              void stopMic();
              return;
            }
            const samples = buffer.getChannelData(0);
            const ch = micChannel.current;
            if (ch === 1 && !meAudio.current) {
              setError(
                "This server cannot transcribe your microphone channel. Select Other speaker or use text.",
              );
              void stopMic();
              return;
            }
            for (let offset = 0; offset < samples.length; offset += 1600) {
              ws.current.send(
                pcmFrame(
                  samples.subarray(offset, offset + 1600),
                  ch,
                  first ? 1 : 0,
                  seq.current[ch]++,
                  Date.now() - started.current,
                ),
              );
              first = false;
            }
          } catch (e) {
            if (alive.current)
              setError(`Audio transmission interrupted: ${String(e)}`);
            void stopMic();
          }
        },
      );
      if (result.status === "error") throw new Error(result.message);
      const resultStart = await r.start();
      if (resultStart.status === "error") throw new Error(resultStart.message);
      if (
        !media.current.current(ticket) ||
        !wantMic.current ||
        !alive.current ||
        AppState.currentState !== "active"
      ) {
        // A permission/start continuation can finish after lifecycle stop cleared the ref.
        // Release this exact recorder too; stopping only the current ref would leak capture.
        if (recorder.current !== r) {
          r.clearOnAudioReady();
          await withDeadline(() => r.stop(), 5000).catch(() => {});
          r.clearOnError();
          await AudioManager.setAudioSessionActivity(false).catch(() => {});
        } else await stopMic();
        return;
      }
      setRecording(true);
      setStatus(
        channel === 0
          ? "Recording other speaker · phone microphone"
          : "Recording you · phone microphone",
      );
      setError("");
    } catch (e) {
      if (alive.current) setError(String(e));
      await stopMic();
    } finally {
      capturing.current = false;
      if (alive.current) setMicBusy(false);
    }
  }
  async function connect(resume = false) {
    if (starting.current) return;
    if (
      !resume &&
      (interviewLoading ||
        languagesLoading ||
        !languages.some((choice) => choice.value === language))
    ) {
      setLanguageError("Choose a supported spoken language before starting.");
      return;
    }
    starting.current = true;
    setConnecting(true);
    setError("");
    const profile = api.profile,
      epoch = ++connectionEpoch.current;
    try {
      const token = await withDeadline(() => api.token(), 15000);
      if (
        !alive.current ||
        epoch !== connectionEpoch.current ||
        profile !== api.profile ||
        AppState.currentState !== "active"
      ) {
        if (epoch === connectionEpoch.current) {
          starting.current = false;
          if (alive.current) setConnecting(false);
        }
        return;
      }
      const socket = new NativeWebSocket(sessionURL(url, profile), undefined, {
        headers: { Authorization: `Bearer ${token}` },
      });
      ws.current = socket;
      handshake.current = setTimeout(() => {
        if (ws.current === socket && alive.current) {
          setError("The session gateway did not respond. Try reconnecting.");
          starting.current = false;
          if (alive.current) setConnecting(false);
          ws.current = null;
          socket.close();
        }
      }, 15000);
      socket.onopen = () => {
        if (
          !alive.current ||
          epoch !== connectionEpoch.current ||
          AppState.currentState !== "active" ||
          ws.current !== socket
        )
          return socket.close();
        try {
          if (resume && sid.current)
            socket.send(
              JSON.stringify({
                type: "session.resume",
                session_id: sid.current,
                last_seq: last.current,
              }),
            );
          else
            socket.send(
              JSON.stringify({
                type: "session.start",
                mode,
                meeting_title: title,
                ...(mode === "interview" && interviewId
                  ? { interview_id: interviewId }
                  : {}),
                ...(mode === "interview" && application
                  ? { job_id: application }
                  : {}),
                ...(mode === "meeting" && company
                  ? { employer_id: company }
                  : {}),
                ...(mode === "meeting" && agenda.trim()
                  ? { agenda: agenda.trim() }
                  : {}),
                answer_style: answerStyle,
                platform: "mobile_in_person",
                client_version: "mobile-0.1.0",
                spoken_language: language,
                consent: { store_transcript: retain },
                audio: { sample_rate: 16000, encoding: "pcm16" },
              }),
            );
        } catch (e) {
          setError(String(e));
          socket.close();
        }
      };
      socket.onmessage = (e) => {
        if (ws.current !== socket || !alive.current) return;
        try {
          const m = JSON.parse(String(e.data)) as Event;
          if (m.type === "session.ready") {
            if (handshake.current) clearTimeout(handshake.current);
            if (typeof m.session_id !== "string" || !m.session_id)
              throw new Error("Invalid session identity");
            sid.current = m.session_id;
            if (!resume) {
              started.current = Date.now();
              seq.current = [0, 0];
              last.current = 0;
              liveRef.current = emptyLive();
            }
            liveRef.current = event(liveRef.current, m);
            last.current = liveRef.current.seq;
            if (liveRef.current.spokenLanguage)
              setLanguage(liveRef.current.spokenLanguage);
            setLive(liveRef.current);
            meAudio.current = m.me_audio === true;
            setActive(true);
            setStatus("In-person session ready · microphone off");
            starting.current = false;
            if (alive.current) setConnecting(false);
            return;
          }
          if (m.type === "auth.expiring") {
            void api
              .token(true)
              .then((t) => {
                if (
                  alive.current &&
                  ws.current === socket &&
                  socket.readyState === 1
                )
                  socket.send(
                    JSON.stringify({ type: "auth.refresh", access_token: t }),
                  );
              })
              .catch((e) => {
                if (alive.current && ws.current === socket) {
                  setError(String(e));
                  socket.close();
                }
              });
            return;
          }
          if (m.type === "session.ended") {
            finishLanguageChange();
            void stopMic();
            sid.current = null;
            setActive(false);
            setStatus("Session ended");
            starting.current = false;
            if (alive.current) setConnecting(false);
            ws.current = null;
            socket.close();
            return;
          }
          if (m.type === "error") {
            finishLanguageChange();
            setError(String(m.message));
            if (m.fatal) {
              void stopMic();
              setActive(false);
              starting.current = false;
              if (alive.current) setConnecting(false);
              ws.current = null;
              socket.close();
            }
            return;
          }
          const next = event(liveRef.current, m);
          if (m.type === "session.language") {
            if (next.spokenLanguage) setLanguage(next.spokenLanguage);
            const languageResult = languageAcknowledgement(
              m,
              lastLanguageRequestId.current,
            );
            if (languageResult) {
              finishLanguageChange();
              lastLanguageRequestId.current = null;
              setError(languageResult.error ?? "");
            }
          }
          last.current = next.seq;
          liveRef.current = next;
          setLive(next);
        } catch {
          setError("Invalid gateway event");
        }
      };
      socket.onerror = () => {
        if (alive.current && ws.current === socket)
          setError("Connection interrupted. Audio stops until you reconnect.");
      };
      socket.onclose = () => {
        if (ws.current !== socket) return;
        finishLanguageChange();
        ws.current = null;
        if (handshake.current) clearTimeout(handshake.current);
        starting.current = false;
        if (alive.current) setConnecting(false);
        void stopMic();
        setStatus(
          sid.current ? "Disconnected · microphone off" : "Microphone off",
        );
      };
    } catch (e) {
      if (alive.current && epoch === connectionEpoch.current) {
        starting.current = false;
        if (alive.current) setConnecting(false);
        setError(String(e));
      }
    }
  }
  return (
    <Screen>
      <ScreenTitle
        eyebrow="Phone microphone"
        title="In-person assistance"
        subtitle="For an in-person conversation. Choose who is speaking, then record one turn at a time."
      />
      <Notice>
        Inform everyone before recording. This mode hears the room through your
        phone; it cannot capture calls running inside other apps. Leaving this
        screen ends this phone session.
      </Notice>
      <Badge label={status} tone={recording ? "ruby" : "neutral"} />
      {live.memory ? <Txt muted>{live.memory}</Txt> : null}
      {error ? <Notice tone="error">{error}</Notice> : null}
      {!active && !sid.current ? (
        <Card>
          <Txt style={{ fontSize: 18, fontWeight: "700" }}>
            Set up this conversation
          </Txt>
          <Segments
            value={mode}
            options={[
              { value: "interview", label: "For interviews" },
              { value: "meeting", label: "For work" },
            ]}
            disabled={connecting}
            onChange={(value) => {
              setMode(value as "interview" | "meeting");
              setInterviewId("");
            }}
          />
          <Select
            label={mode === "interview" ? "Application" : "Company"}
            value={mode === "interview" ? application : company}
            options={[
              {
                value: "",
                label:
                  mode === "interview"
                    ? "General interview · profile context"
                    : "General meeting",
              },
              ...(mode === "interview" ? applications : companies),
            ]}
            disabled={connecting}
            onChange={(value) => {
              if (mode === "interview") {
                setApplication(value);
                setInterviewId("");
              } else setCompany(value);
            }}
          />
          {interviewLoading ? (
            <Txt muted>Loading scheduled interview·</Txt>
          ) : null}
          {interviewId ? (
            <Notice>
              Scheduled interview selected. Review the settings, then start when
              ready.
            </Notice>
          ) : null}
          {interviewError ? (
            <Notice
              tone="error"
              action={
                <Button
                  label="Retry interview"
                  secondary
                  onPress={() => setInterviewRevision((value) => value + 1)}
                />
              }
            >
              {interviewError}
            </Notice>
          ) : null}
          {contextError ? (
            <Notice
              action={
                <Button
                  label="Retry context"
                  secondary
                  onPress={() => setContextRevision((value) => value + 1)}
                />
              }
            >
              {contextError}
            </Notice>
          ) : null}
          {mode === "meeting" ? (
            <Field label="Agenda (optional)">
              <Input
                multiline
                maxLength={2000}
                value={agenda}
                onChangeText={setAgenda}
                placeholder="What will you discuss?"
              />
            </Field>
          ) : null}
          <Segments
            value={answerStyle}
            options={[
              { value: "bullets", label: "Bullet points" },
              { value: "paragraph", label: "Paragraphs" },
            ]}
            onChange={setAnswerStyle}
            disabled={connecting}
          />
          <Field
            label={mode === "interview" ? "Interview title" : "Meeting title"}
          >
            <Input
              placeholder="Meeting title"
              value={title}
              onChangeText={setTitle}
            />
          </Field>
          <Row style={{ flexWrap: "nowrap", justifyContent: "space-between" }}>
            <View style={{ flex: 1, gap: 3 }}>
              <Txt style={{ fontSize: 14, fontWeight: "600" }}>
                Save transcript
              </Txt>
              <Txt muted style={{ fontSize: 12 }}>
                Review this conversation in History.
              </Txt>
            </View>
            <Switch
              value={retain}
              onValueChange={setRetain}
              accessibilityLabel="Save transcript after this session"
            />
          </Row>
          <Select
            label="Spoken language"
            value={language}
            options={languages}
            placeholder={
              languagesLoading ? "Loading languages…" : "Choose a language"
            }
            onChange={(value) => {
              setLanguage(value);
              setLanguageError("");
            }}
            disabled={connecting || languagesLoading}
          />
          <Txt muted style={{ fontSize: 12 }}>
            Choose the spoken language for this conversation.
          </Txt>
          {languageError ? (
            <Notice
              tone="error"
              action={
                <Button
                  secondary
                  compact
                  label="Retry language settings"
                  disabled={connecting || languagesLoading}
                  onPress={() => setLanguageRevision((value) => value + 1)}
                />
              }
            >
              {languageError}
            </Notice>
          ) : null}
          <Button
            label="Start in-person session"
            loading={connecting}
            disabled={
              interviewLoading ||
              languagesLoading ||
              !languages.some((choice) => choice.value === language)
            }
            icon="mic"
            onPress={() => void connect()}
          />
        </Card>
      ) : (
        <>
          <Select
            label="Spoken language"
            value={language}
            options={languages}
            disabled={
              !live.languageSwitching ||
              !active ||
              recording ||
              micBusy ||
              connecting ||
              languageChanging ||
              ws.current?.readyState !== 1
            }
            onChange={changeLiveLanguage}
          />
          <Txt muted accessibilityLiveRegion="polite" style={{ fontSize: 12 }}>
            {languageChanging
              ? "Changing the call language…"
              : !live.languageSwitching
                ? "This server does not support changing the language during a call."
                : "Finish the recorded turn before switching languages. New speech and answers use the new language."}
          </Txt>
          <Field label="Who is speaking?">
            <Segments
              value={String(channel)}
              disabled={recording || micBusy}
              options={[
                { value: "0", label: "Other speaker" },
                { value: "1", label: "Me" },
              ]}
              onChange={(value) => {
                if (!recording) setChannel(value === "1" ? 1 : 0);
              }}
            />
          </Field>
          <Button
            label={
              recording ? "Finish this spoken turn" : "Record this speaker"
            }
            disabled={languageChanging || ws.current?.readyState !== 1}
            loading={micBusy}
            icon="mic"
            onPress={() => void (recording ? stopMic() : capture())}
          />
          {ws.current?.readyState !== 1 ? (
            <Button
              label="Reconnect same session"
              loading={connecting}
              onPress={() => void connect(true)}
            />
          ) : null}
          <Input
            placeholder="Type a question instead"
            multiline
            value={draft}
            maxLength={4000}
            onChangeText={setDraft}
          />
          <Button
            label="Ask"
            disabled={!draft.trim() || ws.current?.readyState !== 1}
            onPress={() => {
              if (send({ type: "question.force", text: draft.trim() }))
                setDraft("");
            }}
          />
          <Button
            label="End in-person session"
            secondary
            danger
            onPress={() =>
              Alert.alert(
                "End this phone session?",
                "Microphone capture stops immediately.",
                [
                  { text: "Cancel" },
                  {
                    text: "End without summary",
                    onPress: () => void end(false),
                  },
                  { text: "End and summarize", onPress: () => void end(true) },
                ],
              )
            }
          />
        </>
      )}
      {active && sid.current ? (
        <HostAssistants
          api={api}
          sessionId={sid.current}
          connected={ws.current?.readyState === 1}
          sharing={live.assistantSharing}
          canShare
          recording={recording || micBusy}
          onSharing={(on) => {
            if (!send({ type: "sharing.set", on }))
              setError("Reconnect before changing sharing.");
          }}
        />
      ) : null}
      {live.items.map((q) => (
        <Card key={q.id}>
          <Txt style={{ fontWeight: "700" }}>{q.question}</Txt>
          <Txt selectable>{q.deep || q.fast || "Preparing guidance…"}</Txt>
        </Card>
      ))}
    </Screen>
  );
}
