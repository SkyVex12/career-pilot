import React, { useEffect, useRef, useState } from "react";
import { Alert, AppState, ScrollView, Switch, View } from "react-native";
import { AudioManager, AudioRecorder } from "react-native-audio-api";
import { Api } from "./api";
import { MediaEpoch } from "./mediaEpoch";
import { pcmFrame, sessionURL } from "./pcm";
import { emptyLive, event, type Event } from "./liveState";
import { Button, Card, Input, Row, Txt, page } from "./ui";
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
    [recording, setRecording] = useState(false),
    [channel, setChannel] = useState<0 | 1>(0),
    [retain, setRetain] = useState(false),
    [title, setTitle] = useState("In-person meeting"),
    [draft, setDraft] = useState(""),
    [language, setLanguage] = useState("en"),
    [languages, setLanguages] = useState<{ id: string; name: string }[]>([]);
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
    media = useRef(new MediaEpoch());
  function send(value: object) {
    if (ws.current?.readyState === 1) ws.current.send(JSON.stringify(value));
  }
  async function stopMic() {
    media.current.stop();
    wantMic.current = false;
    const r = recorder.current;
    recorder.current = null;
    if (r) {
      r.clearOnAudioReady();
      try {
        await r.stop();
      } catch (e) {
        if (alive.current) setError(`Microphone interrupted: ${String(e)}`);
      } finally {
        r.clearOnError();
        await AudioManager.setAudioSessionActivity(false).catch(() => {});
      }
      if (ws.current?.readyState === 1)
        ws.current.send(
          pcmFrame(
            new Float32Array(),
            micChannel.current,
            2,
            seq.current[micChannel.current]++,
            Date.now() - started.current,
          ),
        );
    }
    if (alive.current) {
      setRecording(false);
      setStatus(
        sid.current ? "Session open · microphone off" : "Microphone off",
      );
    }
  }
  async function end(summarize = true) {
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
      .then((d) => setLanguages(d.items))
      .catch(() => {});
    const sub = AppState.addEventListener("change", (state) => {
      if (state !== "active") {
        void stopMic();
        setError(
          "Microphone stopped when the app left the foreground. Tap record to resume explicitly.",
        );
      }
    });
    return () => {
      alive.current = false;
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
      AppState.currentState !== "active" ||
      !sid.current ||
      ws.current?.readyState !== 1
    )
      return;
    const ticket = media.current.begin();
    try {
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
        setError(`Microphone interrupted: ${e.message}`);
        void stopMic();
      });
      const result = r.onAudioReady(
        { sampleRate: 16000, bufferLength: 1600, channelCount: 1 },
        ({ buffer }) => {
          if (!wantMic.current || ws.current?.readyState !== 1) return;
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
        r.clearOnAudioReady();
        await r.stop();
        r.clearOnError();
        await AudioManager.setAudioSessionActivity(false);
        await stopMic();
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
      setError(String(e));
      await stopMic();
    }
  }
  async function connect(resume = false) {
    if (starting.current) return;
    starting.current = true;
    setError("");
    const profile = api.profile;
    try {
      const token = await api.token();
      if (
        !alive.current ||
        profile !== api.profile ||
        AppState.currentState !== "active"
      ) {
        starting.current = false;
        return;
      }
      const socket = new NativeWebSocket(sessionURL(url, profile), undefined, {
        headers: { Authorization: `Bearer ${token}` },
      });
      ws.current = socket;
      socket.onopen = () => {
        if (!alive.current) return socket.close();
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
      };
      socket.onmessage = (e) => {
        if (ws.current !== socket || !alive.current) return;
        try {
          const m = JSON.parse(String(e.data)) as Event;
          if (m.type === "session.ready") {
            sid.current = String(m.session_id);
            if (!resume) {
              started.current = Date.now();
              seq.current = [0, 0];
              last.current = 0;
              setLive(emptyLive());
            }
            meAudio.current = m.me_audio === true;
            setActive(true);
            setStatus("In-person session ready · microphone off");
            starting.current = false;
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
              .catch((e) => setError(String(e)));
            return;
          }
          if (m.type === "session.ended") {
            void stopMic();
            sid.current = null;
            setActive(false);
            setStatus("Session ended");
            starting.current = false;
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
              ws.current = null;
              socket.close();
            }
            return;
          }
          last.current = Math.max(last.current, m.seq ?? 0);
          setLive((s) => event(s, m));
        } catch {
          setError("Invalid gateway event");
        }
      };
      socket.onerror = () =>
        setError("Connection interrupted. Audio stops until you reconnect.");
      socket.onclose = () => {
        if (ws.current !== socket) return;
        ws.current = null;
        starting.current = false;
        void stopMic();
        setStatus(
          sid.current ? "Disconnected · microphone off" : "Microphone off",
        );
      };
    } catch (e) {
      starting.current = false;
      setError(String(e));
    }
  }
  return (
    <ScrollView
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={page}
    >
      <Txt title>In-person microphone</Txt>
      <Txt muted>
        This is a separate phone session. It cannot hear system audio from Zoom,
        Teams, Meet or a phone call. Use Companion for computer calls. Inform
        participants before recording. Leaving this screen ends the phone
        session without a summary.
      </Txt>
      <Txt>{status}</Txt>
      {live.memory ? <Txt muted>{live.memory}</Txt> : null}
      {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
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
          <Row>
            {languages.map((l) => (
              <Button
                key={l.id}
                label={`${language === l.id ? "✓ " : ""}${l.name}`}
                secondary
                onPress={() => setLanguage(l.id)}
              />
            ))}
          </Row>
          <Button
            label="Start separate in-person session"
            onPress={() => void connect()}
          />
        </>
      ) : (
        <>
          <Row>
            <Button
              label={`${channel === 0 ? "✓ " : ""}Other speaker`}
              secondary
              disabled={recording}
              onPress={() => setChannel(0)}
            />
            <Button
              label={`${channel === 1 ? "✓ " : ""}I'm speaking`}
              secondary
              disabled={recording}
              onPress={() => setChannel(1)}
            />
          </Row>
          <Button
            label={
              recording ? "Finish this spoken turn" : "Record this speaker"
            }
            disabled={ws.current?.readyState !== 1}
            onPress={() => void (recording ? stopMic() : capture())}
          />
          {ws.current?.readyState !== 1 ? (
            <Button
              label="Reconnect same session"
              onPress={() => void connect(true)}
            />
          ) : null}
          <Input
            placeholder="Type a question instead"
            multiline
            value={draft}
            onChangeText={setDraft}
          />
          <Button
            label="Ask"
            disabled={!draft.trim() || ws.current?.readyState !== 1}
            onPress={() => {
              send({ type: "question.force", text: draft.trim() });
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
