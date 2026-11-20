import React, { useEffect, useRef, useState } from "react";
import { Alert, AppState, ScrollView, Switch, View } from "react-native";
import type { AudioRecorder } from "react-native-audio-api";
import { withDeadline } from "./requestScope";
import { Api } from "./api";
import { MediaEpoch } from "./mediaEpoch";
import { pcmFrame, sessionURL } from "./pcm";
import { emptyLive, event, type Event } from "./liveState";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Notice,
  Row,
  ScreenTitle,
  Select,
  Segments,
  Txt,
  page,
} from "./ui";
const NativeWebSocket = WebSocket as unknown as {
  new (
    url: string,
    protocols?: string | string[],
    options?: { headers: Record<string, string> },
  ): WebSocket;
};
/** Foreground, user-started in-person mic only; never joins a desktop capture session. */
export function InPerson({ api, url }: { api: Api; url: string }) {
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
    [draft, setDraft] = useState(""),
    [language, setLanguage] = useState("en"),
    [languages, setLanguages] = useState<{ id: string; name: string }[]>([]);
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
    alive.current = true;
    void api
      .request<{ items: { id: string; name: string }[] }>("/call-languages")
      .then((d) => {
        if (!alive.current) return;
        if (
          !Array.isArray(d.items) ||
          d.items.some(
            (l) => !l || typeof l.id !== "string" || typeof l.name !== "string",
          )
        )
          throw new Error(
            "The server returned an invalid language list. Update the server before starting.",
          );
        setLanguages(d.items);
        setLanguage((previous) =>
          d.items.some((l) => l.id === previous)
            ? previous
            : (d.items[0]?.id ?? ""),
        );
      })
      .catch((e) => {
        if (alive.current) setError(String(e));
      });
    const sub = AppState.addEventListener("change", (state) => {
      if (state !== "active") {
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
                mode: "meeting",
                meeting_title: title,
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
              setLive(liveRef.current);
            }
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
    <ScrollView
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={page}
    >
      <ScreenTitle
        eyebrow="Phone microphone"
        title="A little help in the room."
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
        <>
          <Input
            placeholder="Meeting title"
            value={title}
            onChangeText={setTitle}
          />
          <Row>
            <Txt>Save transcript after the session</Txt>
            <Switch
              value={retain}
              onValueChange={setRetain}
              accessibilityLabel="Save transcript after this session"
            />
          </Row>
          <Select
            label="Spoken language"
            value={language}
            options={languages.map((l) => ({ value: l.id, label: l.name }))}
            onChange={setLanguage}
          />
          <Button
            label="Start in-person session"
            loading={connecting}
            disabled={!languages.some((l) => l.id === language)}
            icon="mic"
            onPress={() => void connect()}
          />
        </>
      ) : (
        <>
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
            disabled={ws.current?.readyState !== 1}
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
      {live.items.map((q) => (
        <Card key={q.id}>
          <Txt style={{ fontWeight: "700" }}>{q.question}</Txt>
          <Txt selectable>{q.deep || q.fast || "Preparing guidance…"}</Txt>
        </Card>
      ))}
    </ScrollView>
  );
}
