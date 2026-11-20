import React, { useEffect, useRef, useState, useContext } from "react";
import {
  Alert,
  AppState,
  ScrollView,
  Switch,
  View,
  Platform,
} from "react-native";
import { activateKeepAwakeAsync, deactivateKeepAwake } from "expo-keep-awake";
import { randomUUID } from "expo-crypto";
import { Api } from "./api";
import { event, emptyLive, command, gatewayURL, type Event } from "./liveState";
import { Button, Card, Input, Row, Txt, page, Theme } from "./ui";
type Session = {
  session_id: string;
  title: string;
  mode: string;
  platform: string;
  capture_connected: boolean;
  profile_id: string;
};
type Attachment = { attachment_id: string; session_id: string };
export function Companion({ api, url }: { api: Api; url: string }) {
  const [sessions, setSessions] = useState<Session[]>([]),
    [attachment, setAttachment] = useState<Attachment | null>(null),
    [live, setLive] = useState(emptyLive),
    [error, setError] = useState(""),
    [draft, setDraft] = useState(""),
    [keep, setKeep] = useState(false),
    [size, setSize] = useState(18),
    [latest, setLatest] = useState(true),
    [commandStatus, setCommandStatus] = useState("");
  const socket = useRef<WebSocket | null>(null),
    sequence = useRef(0),
    scroll = useRef<React.ComponentRef<typeof ScrollView>>(null),
    pending = useRef<ReturnType<typeof command> | null>(null);
  const mounted = useRef(true),
    attaching = useRef(false);
  const [attachBusy, setAttachBusy] = useState(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const c = useContext(Theme);
  async function refresh() {
    try {
      const data = await api.request<{ items: Session[] }>(
        "/companion/sessions",
      );
      setSessions(data.items);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    void refresh();
    const t = setInterval(() => {
      if (AppState.currentState === "active" && !attachment) void refresh();
    }, 15000);
    return () => clearInterval(t);
  }, [api, attachment]);
  useEffect(() => {
    if (!attachment) return;
    let active = true,
      timer: ReturnType<typeof setTimeout> | undefined,
      attempt = 0;
    async function connect() {
      if (
        !active ||
        AppState.currentState !== "active" ||
        socket.current?.readyState === 0 ||
        socket.current?.readyState === 1
      )
        return;
      try {
        const token = await api.token();
        if (!active) return;
        const ws = new WebSocket(gatewayURL(url));
        socket.current = ws;
        ws.onopen = () => {
          if (!active) return ws.close();
          ws.send(
            JSON.stringify({
              type: "companion.join",
              session_id: attachment!.session_id,
              attachment_id: attachment!.attachment_id,
              last_seq: sequence.current,
              client_version: "0.1.0",
              access_token: token,
            }),
          );
        };
        ws.onmessage = (e) => {
          if (!active || socket.current !== ws) return;
          try {
            const m = JSON.parse(String(e.data)) as Event;
            if (m.type === "auth.expiring") {
              void api
                .token(true)
                .then((t) => {
                  if (active && socket.current === ws && ws.readyState === 1)
                    ws.send(
                      JSON.stringify({ type: "auth.refresh", access_token: t }),
                    );
                })
                .catch((e) => setError(String(e)));
              return;
            }
            if (m.type === "companion.ready") {
              attempt = 0;
              setError("");
              setCommandStatus(
                pending.current
                  ? "Waiting for acknowledgment after reconnect"
                  : "",
              );
              return;
            }
            if (m.type === "companion.command.result") {
              const status = String(m.result);
              setCommandStatus(
                status === "queued"
                  ? "Command queued — awaiting desktop execution"
                  : `Desktop: ${status}`,
              );
              if (
                m.command_id === pending.current?.command_id &&
                status !== "queued"
              )
                pending.current = null;
              return;
            }
            if (m.type === "companion.error" || m.type === "error") {
              setError(String(m.message ?? m.code ?? "Connection unavailable"));
              return;
            }
            setLive((old) => {
              const next = event(old, m);
              sequence.current = next.seq;
              return next;
            });
          } catch {
            setError(
              "Invalid session event; reconnect to reload current state.",
            );
          }
        };
        ws.onerror = () => setError("Connection interrupted. Reconnecting…");
        ws.onclose = (e) => {
          if (!active || socket.current !== ws) return;
          socket.current = null;
          if (AppState.currentState !== "active") return;
          setLive((s) => ({
            ...s,
            connected: false,
            status: "Reconnecting — previous text may be stale",
          }));
          if ([4401, 4403, 4410, 4411, 4412, 4426].includes(e.code)) {
            setError(
              "Attachment unavailable. Return to the active-session list and reconnect.",
            );
            return;
          }
          timer = setTimeout(
            () => void connect(),
            Math.min(30000, 1000 * 2 ** attempt++),
          );
        };
      } catch (e) {
        setError(String(e));
        timer = setTimeout(() => void connect(), 5000);
      }
    }
    void connect();
    const sub = AppState.addEventListener("change", (state) => {
      if (state !== "active") {
        if (timer) clearTimeout(timer);
        socket.current?.close();
      } else {
        if (timer) clearTimeout(timer);
        void connect();
      }
    });
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
      sub.remove();
      socket.current?.close();
      socket.current = null;
    };
  }, [attachment, api, url]);
  useEffect(() => {
    let active = true;
    async function update() {
      if (
        active &&
        keep &&
        attachment &&
        !live.ended &&
        AppState.currentState === "active"
      ) {
        await activateKeepAwakeAsync("companion").catch(() => {});
        if (!active || AppState.currentState !== "active")
          deactivateKeepAwake("companion");
      } else deactivateKeepAwake("companion");
    }
    void update();
    const sub = AppState.addEventListener("change", () => void update());
    return () => {
      active = false;
      sub.remove();
      deactivateKeepAwake("companion");
    };
  }, [keep, attachment, live.ended]);
  async function attach(s: Session) {
    if (attaching.current) return;
    attaching.current = true;
    setAttachBusy(true);
    const profile = api.profile;
    try {
      const token = await api.token();
      if (!mounted.current || profile !== api.profile) return;
      const headers = {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "x-copilot-profile": profile,
      };
      const response = await fetch(
        `${api.base}/v1/companion/sessions/${s.session_id}/attach`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ device_name: `${Platform.OS} phone` }),
        },
      );
      if (!response.ok)
        throw new Error(`Attachment unavailable (${response.status})`);
      const a = (await response.json()) as Attachment;
      if (!mounted.current || profile !== api.profile) {
        await fetch(`${api.base}/v1/companion/attachments/${a.attachment_id}`, {
          method: "DELETE",
          headers,
        }).catch(() => {});
        return;
      }
      api.attachments.add(a.attachment_id);
      sequence.current = 0;
      setLive(emptyLive());
      setAttachment(a);
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      attaching.current = false;
      if (mounted.current) setAttachBusy(false);
    }
  }
  async function detach() {
    const a = attachment;
    setAttachment(null);
    setLive(emptyLive());
    sequence.current = 0;
    pending.current = null;
    if (a)
      try {
        api.attachments.delete(a.attachment_id);
        await api.request(
          `/companion/attachments/${a.attachment_id}`,
          undefined,
          "DELETE",
        );
      } catch (e) {
        setError(
          `Disconnected locally; revoke this device after reconnecting. ${e}`,
        );
      }
  }
  function send(
    kind: "answer" | "followup" | "end",
    confirmed = false,
    summarize = true,
  ) {
    if (pending.current)
      return setError(
        "Wait for the previous command or retry it using the same identifier.",
      );
    if (socket.current?.readyState !== 1 || !live.connected)
      return setError("Desktop audio must reconnect before sending a command.");
    try {
      const cmd = command(randomUUID(), kind, draft, confirmed, summarize);
      pending.current = cmd;
      socket.current.send(JSON.stringify(cmd));
      setCommandStatus("Waiting for acknowledgment");
      if (kind === "followup") setDraft("");
    } catch (e) {
      setError(String(e));
    }
  }
  if (!attachment)
    return (
      <ScrollView contentContainerStyle={page}>
        <Txt title>Keep your headphones on</Txt>
        <Txt muted>
          Your computer supplies the audio. Your phone displays guidance without
          microphone permission.
        </Txt>
        {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
        <Button
          label="Refresh active sessions"
          onPress={() => void refresh()}
          secondary
        />
        {sessions.map((s) => (
          <Card key={s.session_id}>
            <Txt title>{s.title || "Desktop call"}</Txt>
            <Txt>
              {s.platform || "Computer"} · {s.mode}
            </Txt>
            <Txt muted>
              {s.capture_connected
                ? "Desktop capture connected"
                : "Desktop capture reconnecting"}
            </Txt>
            <Button
              label="Connect this phone"
              disabled={attachBusy}
              onPress={() => void attach(s)}
            />
          </Card>
        ))}
        {sessions.length === 0 ? (
          <Txt muted>
            Start a call in Career Pilot on your computer, then refresh.
            Connecting this display never starts another audio pipeline.
          </Txt>
        ) : null}
      </ScrollView>
    );
  return (
    <View style={{ flex: 1 }}>
      <View
        style={{
          padding: 12,
          gap: 8,
          borderBottomWidth: 1,
          borderColor: c.line,
        }}
      >
        <Txt>{live.status}</Txt>
        {live.memory ? <Txt muted>{live.memory}</Txt> : null}
        <Row>
          <Button
            label="Disconnect display"
            onPress={() => void detach()}
            secondary
          />
          <Button
            label="A−"
            onPress={() => setSize((n) => Math.max(16, n - 2))}
            secondary
          />
          <Button
            label="A+"
            onPress={() => setSize((n) => Math.min(30, n + 2))}
            secondary
          />
        </Row>
        <Row>
          <Txt muted>Keep screen awake</Txt>
          <Switch
            accessibilityLabel="Keep screen awake"
            value={keep}
            onValueChange={setKeep}
          />
        </Row>
        {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
      </View>
      <ScrollView
        ref={scroll}
        contentContainerStyle={page}
        onScrollBeginDrag={() => setLatest(false)}
        onContentSizeChange={() => {
          if (latest) scroll.current?.scrollToEnd({ animated: false });
        }}
      >
        {live.items.map((q) => (
          <Card key={q.id}>
            <Txt style={{ fontSize: size, fontWeight: "700" }}>
              {q.question}
            </Txt>
            <Txt selectable style={{ fontSize: size, lineHeight: size * 1.5 }}>
              {q.deep || q.fast || "Preparing guidance…"}
            </Txt>
          </Card>
        ))}
      </ScrollView>
      {!latest ? (
        <Button
          label="Return to latest"
          onPress={() => {
            setLatest(true);
            scroll.current?.scrollToEnd({ animated: true });
          }}
        />
      ) : null}
      <View style={{ padding: 12, gap: 8 }}>
        <Txt muted>{commandStatus}</Txt>
        {pending.current ? (
          <Button
            label="Retry same command"
            onPress={() => {
              if (socket.current?.readyState === 1 && pending.current)
                socket.current.send(JSON.stringify(pending.current));
            }}
            secondary
          />
        ) : null}
        <Input
          placeholder="Ask a follow-up"
          value={draft}
          onChangeText={setDraft}
        />
        <Row>
          <Button
            label="Answer"
            disabled={!live.connected || live.ended}
            onPress={() => send("answer")}
          />
          <Button
            label="Follow-up"
            disabled={!draft.trim() || !live.connected || live.ended}
            onPress={() => send("followup")}
          />
          <Button
            label="End call"
            disabled={!live.connected || live.ended}
            secondary
            onPress={() =>
              Alert.alert(
                "End the computer call?",
                "This stops the same capture session on your computer.",
                [
                  { text: "Cancel", style: "cancel" },
                  {
                    text: "End without summary",
                    style: "destructive",
                    onPress: () => send("end", true, false),
                  },
                  {
                    text: "End and summarize",
                    onPress: () => send("end", true, true),
                  },
                ],
              )
            }
          />
        </Row>
      </View>
    </View>
  );
}
