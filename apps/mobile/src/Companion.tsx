import React, { useEffect, useRef, useState, useContext } from "react";
import {
  Alert,
  AppState,
  ScrollView,
  Switch,
  View,
  Platform,
  useWindowDimensions,
} from "react-native";
import { activateKeepAwakeAsync, deactivateKeepAwake } from "expo-keep-awake";
import { randomUUID } from "expo-crypto";
import { Api, ApiError } from "./api";
import { companionDeviceName } from "./companionDevice";
import { findReusableCompanionAttachment } from "./companionAttachment";
import { RequestScope, ConnectionGate, withDeadline } from "./requestScope";
import {
  event,
  emptyLive,
  command,
  gatewayURL,
  languageAcknowledgement,
  type Event,
} from "./liveState";
import { parseCallLanguages } from "./callLanguages";
import type { Choice } from "./choiceState";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icon,
  IconButton,
  Input,
  Loading,
  Notice,
  Row,
  ScreenTitle,
  Select,
  Txt,
  page,
  Theme,
} from "./ui";
type Session = {
  session_id: string;
  title: string;
  mode: string;
  platform: string;
  capture_connected: boolean;
  profile_id: string;
  spoken_language?: string | null;
};
type Attachment = { attachment_id: string; session_id: string };
export function Companion({
  api,
  url,
  visible = true,
}: {
  api: Api;
  url: string;
  visible?: boolean;
}) {
  const [sessions, setSessions] = useState<Session[]>([]),
    [attachment, setAttachment] = useState<Attachment | null>(null),
    [live, setLive] = useState(emptyLive),
    [error, setError] = useState(""),
    [draft, setDraft] = useState(""),
    [keep, setKeep] = useState(false),
    [size, setSize] = useState(18),
    [latest, setLatest] = useState(true),
    [commandStatus, setCommandStatus] = useState("");
  const [languages, setLanguages] = useState<Choice[]>([]),
    [languageError, setLanguageError] = useState(""),
    [languageAttempt, setLanguageAttempt] = useState(0);
  const languageTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastLanguageCommandId = useRef<string | null>(null);
  function clearLanguageTimer() {
    if (languageTimer.current) clearTimeout(languageTimer.current);
    languageTimer.current = null;
  }
  const socket = useRef<WebSocket | null>(null),
    sequence = useRef(0),
    scroll = useRef<React.ComponentRef<typeof ScrollView>>(null),
    pending = useRef<ReturnType<typeof command> | null>(null);
  const liveRef = useRef(emptyLive());
  const attachedRef = useRef(attachment);
  attachedRef.current = attachment;
  const refreshScope = useRef(new RequestScope());
  const attachAbort = useRef<AbortController | null>(null);
  // Retain the account's scope through profile remounts and late old-account cleanup.
  const retiringAttachments = useRef(api.companionRetirements).current;
  const [reconnect, setReconnect] = useState(0);
  const mounted = useRef(true),
    attaching = useRef(false);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachingSession, setAttachingSession] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true),
    [displaySettings, setDisplaySettings] = useState(false);
  useEffect(() => {
    let current = true;
    const profile = api.profile;
    const controller = new AbortController();
    setLanguageError("");
    void api
      .request("/call-languages", undefined, "GET", true, controller.signal)
      .then(parseCallLanguages)
      .then((choices) => {
        if (current && api.profile === profile) setLanguages(choices);
      })
      .catch(() => {
        if (current && api.profile === profile)
          setLanguageError("Could not load available languages.");
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [api, languageAttempt]);
  useEffect(() => {
    mounted.current = true;
    const originalProfile = api.profile;
    return () => {
      mounted.current = false;
      clearLanguageTimer();
      refreshScope.current.invalidate();
      attachAbort.current?.abort();
      const attached = attachedRef.current;
      if (attached) {
        retiringAttachments.retire(attached.attachment_id);
        void withDeadline(
          (signal) =>
            api.request(
              `/companion/attachments/${attached.attachment_id}`,
              undefined,
              "DELETE",
              true,
              signal,
              true,
              originalProfile,
            ),
          8000,
        )
          .then(() => api.attachments.delete(attached.attachment_id))
          .catch((e) => {
            if (e instanceof ApiError && e.status === 404)
              api.attachments.delete(attached.attachment_id);
          });
      }
    };
  }, []);
  const c = useContext(Theme);
  const { width, height, fontScale } = useWindowDimensions();
  const compact = width >= 600 && height < 500 && fontScale <= 1.3;
  async function refresh() {
    if (mounted.current) setRefreshing(true);
    try {
      const data = await refreshScope.current.run((signal) =>
        withDeadline(
          (bounded) =>
            api.request<{ items: Session[] }>(
              "/companion/sessions",
              undefined,
              "GET",
              true,
              bounded,
            ),
          15000,
          signal,
        ),
      );
      if (!mounted.current || !data) return;
      if (
        !Array.isArray(data.items) ||
        data.items.some(
          (x) =>
            !x ||
            typeof x.session_id !== "string" ||
            typeof x.profile_id !== "string" ||
            typeof x.capture_connected !== "boolean" ||
            typeof x.title !== "string" ||
            typeof x.mode !== "string" ||
            typeof x.platform !== "string" ||
            (x.spoken_language != null &&
              typeof x.spoken_language !== "string"),
        )
      )
        throw new Error(
          "This server returned an unsupported session list. Update the server and refresh.",
        );
      setSessions(data.items);
      setError("");
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      if (mounted.current) setRefreshing(false);
    }
  }
  useEffect(() => {
    if (!visible) {
      attachAbort.current?.abort();
      liveRef.current = {
        ...liveRef.current,
        connected: false,
        status: "Display paused",
      };
      setLive(liveRef.current);
      return;
    }
    void refresh();
    const t = setInterval(() => {
      if (
        AppState.currentState === "active" &&
        !attachment &&
        !attaching.current
      )
        void refresh();
    }, 15000);
    return () => clearInterval(t);
  }, [api, attachment, visible]);
  useEffect(() => {
    if (!attachment || !visible) return;
    let active = true,
      terminal = false,
      attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let handshake: ReturnType<typeof setTimeout> | undefined;
    let renewal: ReturnType<typeof setInterval> | undefined;
    const gate = new ConnectionGate();
    const profile = api.profile;
    const updateConnection = (status: string) => {
      liveRef.current = { ...liveRef.current, connected: false, status };
      setLive(liveRef.current);
    };
    const stopTimers = () => {
      if (timer) clearTimeout(timer);
      if (handshake) clearTimeout(handshake);
      if (renewal) clearInterval(renewal);
    };
    function retry() {
      if (!active || terminal || AppState.currentState !== "active") return;
      if (attempt >= 5) {
        updateConnection("Connection unavailable; previous text may be stale");
        setError(
          "Could not reach the companion gateway. Check the server address or retry the connection.",
        );
        return;
      }
      timer = setTimeout(
        () => void connect(),
        Math.min(15000, 1000 * 2 ** attempt++),
      );
    }
    async function connect() {
      if (
        !active ||
        terminal ||
        AppState.currentState !== "active" ||
        socket.current?.readyState === 0 ||
        socket.current?.readyState === 1
      )
        return;
      const ticket = gate.begin();
      if (ticket === null) return;
      try {
        const token = await withDeadline(() => api.token(), 15000);
        if (
          !active ||
          !gate.current(ticket) ||
          profile !== api.profile ||
          AppState.currentState !== "active"
        )
          return;
        const ws = new WebSocket(gatewayURL(url));
        socket.current = ws;
        const current = () =>
          active && socket.current === ws && profile === api.profile;
        const transmitToken = async (force = false) => {
          try {
            const fresh = await withDeadline(() => api.token(force), 15000);
            if (
              current() &&
              ws.readyState === 1 &&
              AppState.currentState === "active"
            )
              ws.send(
                JSON.stringify({ type: "auth.refresh", access_token: fresh }),
              );
          } catch (e) {
            if (current()) {
              setError(String(e));
              ws.close();
            }
          }
        };
        handshake = setTimeout(() => {
          if (current()) {
            setError(
              "The companion gateway did not respond. Retrying connection...",
            );
            socket.current = null;
            ws.close();
            updateConnection("Disconnected - previous text may be stale");
            retry();
          }
        }, 15000);
        ws.onopen = () => {
          if (!current() || AppState.currentState !== "active")
            return ws.close();
          try {
            ws.send(
              JSON.stringify({
                type: "companion.join",
                session_id: attachment!.session_id,
                attachment_id: attachment!.attachment_id,
                last_seq: sequence.current,
                client_version: "mobile-0.1.0",
                access_token: token,
              }),
            );
          } catch (e) {
            setError(String(e));
            ws.close();
          }
        };
        ws.onmessage = (e) => {
          if (!current() || terminal) return;
          try {
            const m = JSON.parse(String(e.data)) as Event;
            if (m.type === "auth.expiring") {
              void transmitToken(true);
              return;
            }
            if (m.type === "companion.ready") {
              if (handshake) clearTimeout(handshake);
              if (renewal) clearInterval(renewal);
              // Companion relay checks expiry but does not send auth.expiring.
              renewal = setInterval(() => void transmitToken(), 30000);
              attempt = 0;
              liveRef.current = event(liveRef.current, m);
              setLive(liveRef.current);
              setError("");
              setCommandStatus(
                pending.current
                  ? "Retry the pending command to check its result"
                  : "",
              );
              return;
            }
            if (m.type === "companion.command.result") {
              if (m.command_id !== pending.current?.command_id) return;
              const status = String(m.result);
              setCommandStatus(
                status === "queued"
                  ? "Command queued - awaiting desktop execution"
                  : `Desktop: ${status}`,
              );
              if (
                status !== "queued" &&
                !(
                  pending.current?.kind === "set_language" &&
                  status === "executed"
                )
              ) {
                pending.current = null;
                clearLanguageTimer();
              }
              return;
            }
            if (m.type === "companion.error" || m.type === "error") {
              if (pending.current?.kind === "set_language") {
                pending.current = null;
                clearLanguageTimer();
              }
              setError(String(m.message ?? m.code ?? "Connection unavailable"));
              return;
            }
            const next = event(liveRef.current, m);
            const languageResult = languageAcknowledgement(
              m,
              lastLanguageCommandId.current,
            );
            if (languageResult) {
              if (pending.current?.command_id === m.request_id)
                pending.current = null;
              lastLanguageCommandId.current = null;
              clearLanguageTimer();
              setCommandStatus(
                languageResult.error !== null
                  ? "Language unchanged"
                  : "Call language changed",
              );
              setError(languageResult.error ?? "");
            }
            liveRef.current = next;
            sequence.current = next.seq;
            setLive(next);
            if (m.type === "companion.ended") {
              terminal = true;
              stopTimers();
              pending.current = null;
              clearLanguageTimer();
              ws.close();
            }
          } catch {
            terminal = true;
            stopTimers();
            updateConnection("Display needs to reconnect");
            setError(
              "Invalid session event. Retry connection to reload the current state.",
            );
            ws.close();
          }
        };
        ws.onerror = () => {
          if (current()) setError("Connection interrupted. Retrying...");
        };
        ws.onclose = (e) => {
          if (!current()) return;
          socket.current = null;
          if (handshake) clearTimeout(handshake);
          if (renewal) clearInterval(renewal);
          if (terminal) return;
          updateConnection("Disconnected - previous text may be stale");
          if ([4401, 4403, 4410, 4411, 4412, 4426].includes(e.code)) {
            terminal = true;
            setError(
              "Attachment unavailable. Disconnect this display and attach again from the session list.",
            );
            return;
          }
          retry();
        };
      } catch (e) {
        if (active && gate.current(ticket)) {
          setError(String(e));
          retry();
        }
      } finally {
        gate.finish(ticket);
      }
    }
    updateConnection("Connecting to desktop guidance…");
    void connect();
    const sub = AppState.addEventListener("change", (state) => {
      gate.invalidate();
      stopTimers();
      if (state !== "active") {
        const previous = socket.current;
        socket.current = null;
        previous?.close();
        updateConnection("Display paused while app is in background");
      } else {
        attempt = 0;
        void connect();
      }
    });
    return () => {
      active = false;
      gate.invalidate();
      stopTimers();
      sub.remove();
      const previous = socket.current;
      socket.current = null;
      previous?.close();
    };
  }, [attachment, api, url, reconnect, visible]);
  useEffect(() => {
    let active = true;
    async function update() {
      if (
        active &&
        visible &&
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
  }, [keep, attachment, live.ended, visible]);
  async function attach(s: Session) {
    if (attaching.current) return;
    attaching.current = true;
    setAttachBusy(true);
    setAttachingSession(s.session_id);
    const profile = api.profile;
    const controller = new AbortController();
    attachAbort.current = controller;
    try {
      await withDeadline(
        async (signal) => {
          const deviceName = await companionDeviceName(Platform.OS);
          signal.throwIfAborted();
          const list = await api.request<unknown>(
            "/companion/attachments",
            undefined,
            "GET",
            true,
            signal,
          );
          signal.throwIfAborted();
          if (!mounted.current || profile !== api.profile) return;
          const existing = findReusableCompanionAttachment(list, {
            deviceName,
            profileId: s.profile_id,
            sessionId: s.session_id,
            excludedIds: retiringAttachments,
          });
          const token = await api.token();
          signal.throwIfAborted();
          if (!mounted.current || profile !== api.profile) return;
          const headers: Record<string, string> = {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          };
          if (profile) headers["x-copilot-profile"] = profile;
          let a: Attachment;
          if (existing) a = existing;
          else {
            const response = await fetch(
              `${api.base}/v1/companion/sessions/${s.session_id}/attach`,
              {
                method: "POST",
                headers,
                signal,
                body: JSON.stringify({ device_name: deviceName }),
              },
            );
            if (!response.ok)
              throw new Error(`Attachment unavailable (${response.status})`);
            a = (await response.json()) as Attachment;
          }
          if (
            !a ||
            typeof a.attachment_id !== "string" ||
            a.session_id !== s.session_id
          )
            throw new Error(
              "Invalid attachment response. Refresh the session list.",
            );
          if (!mounted.current || signal.aborted || profile !== api.profile) {
            // A timed-out POST body can settle after a newer attempt has adopted its grant.
            if (
              retiringAttachments === api.companionRetirements &&
              api.attachments.has(a.attachment_id)
            )
              return;
            retiringAttachments.retire(a.attachment_id);
            await withDeadline(
              (bounded) =>
                fetch(
                  `${api.base}/v1/companion/attachments/${a.attachment_id}`,
                  { method: "DELETE", headers, signal: bounded },
                ),
              8000,
            ).catch(() => {});
            return;
          }
          if (existing && retiringAttachments.has(a.attachment_id))
            throw new Error(
              "This display was disconnected while connecting. Try connecting again.",
            );
          api.attachments.add(a.attachment_id);
          sequence.current = 0;
          liveRef.current = emptyLive();
          setLive(liveRef.current);
          pending.current = null;
          clearLanguageTimer();
          setCommandStatus("");
          setError("");
          setAttachment(a);
        },
        15000,
        controller.signal,
      );
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      attaching.current = false;
      if (attachAbort.current === controller) attachAbort.current = null;
      if (mounted.current) {
        setAttachBusy(false);
        setAttachingSession(null);
      }
    }
  }
  async function detach() {
    const a = attachment;
    if (a) retiringAttachments.retire(a.attachment_id);
    setAttachment(null);
    liveRef.current = emptyLive();
    setLive(liveRef.current);
    sequence.current = 0;
    pending.current = null;
    clearLanguageTimer();
    if (a)
      try {
        await withDeadline(
          (signal) =>
            api.request(
              `/companion/attachments/${a.attachment_id}`,
              undefined,
              "DELETE",
              true,
              signal,
            ),
          8000,
        );
        api.attachments.delete(a.attachment_id);
      } catch (e) {
        if (mounted.current && !attachedRef.current)
          setError(
            `Disconnected locally; revoke this device after reconnecting. ${e}`,
          );
      }
  }
  function send(
    kind: "answer" | "followup" | "end" | "set_language",
    confirmed = false,
    summarize = true,
    spokenLanguage = "",
  ) {
    if (pending.current)
      return setError(
        "Wait for the previous command or retry it using the same identifier.",
      );
    if (socket.current?.readyState !== 1 || !live.connected)
      return setError("Desktop audio must reconnect before sending a command.");
    if (
      kind === "set_language" &&
      (!live.languageSwitching ||
        !languages.some((choice) => choice.value === spokenLanguage))
    )
      return setError(
        "Choose an available language on a server that supports live language changes.",
      );
    try {
      const cmd = command(
        randomUUID(),
        kind,
        kind === "set_language" ? spokenLanguage : draft,
        confirmed,
        summarize,
      );
      pending.current = cmd;
      socket.current.send(JSON.stringify(cmd));
      setCommandStatus("Waiting for acknowledgment");
      if (kind === "set_language") {
        lastLanguageCommandId.current = cmd.command_id;
        clearLanguageTimer();
        languageTimer.current = setTimeout(() => {
          if (mounted.current && pending.current?.command_id === cmd.command_id)
            setCommandStatus(
              "Language change not confirmed. Retry the pending command when connected.",
            );
        }, 15000);
      }
      if (kind === "followup") setDraft("");
    } catch (e) {
      setError(String(e));
    }
  }
  if (!attachment)
    return (
      <ScrollView contentContainerStyle={page}>
        <ScreenTitle
          eyebrow="Desktop companion"
          title="Follow your live call"
          subtitle="Read guidance on this phone while your computer handles the call audio."
          action={
            <IconButton
              label="Refresh active sessions"
              icon="refresh"
              disabled={refreshing}
              onPress={() => void refresh()}
            />
          }
        />
        {sessions.length ? (
          <Row style={{ flexWrap: "nowrap", gap: 10 }}>
            <Icon name="headphones" size={20} color={c.ruby} />
            <Txt muted style={{ fontSize: 13, flex: 1 }}>
              Audio on your computer · phone microphone off
            </Txt>
          </Row>
        ) : (
          <View
            style={{
              backgroundColor: c.tint,
              borderRadius: 14,
              padding: 16,
              flexDirection: "row",
              alignItems: "center",
              gap: 16,
            }}
          >
            <Icon name="headphones" size={26} color={c.ruby} />
            <View style={{ flex: 1, gap: 4 }}>
              <Txt style={{ fontWeight: "600" }}>
                Audio stays on your computer
              </Txt>
              <Txt muted style={{ fontSize: 13, lineHeight: 20 }}>
                This phone displays guidance. Its microphone stays off.
              </Txt>
            </View>
          </View>
        )}
        {error ? (
          <Notice
            tone="error"
            action={
              <Button
                label="Try again"
                secondary
                icon="refresh"
                disabled={refreshing}
                onPress={() => void refresh()}
              />
            }
          >
            {error}
          </Notice>
        ) : null}
        <Row style={{ justifyContent: "space-between" }}>
          <Txt style={{ fontSize: 15, fontWeight: "700" }}>Available calls</Txt>
          <Badge
            label={refreshing ? "Updating…" : `${sessions.length} available`}
          />
        </Row>
        {refreshing && !sessions.length ? (
          <Loading label="Finding desktop calls…" />
        ) : null}
        {sessions.map((s) => (
          <Card key={s.session_id}>
            <Row style={{ justifyContent: "space-between" }}>
              <Badge
                label={
                  s.capture_connected
                    ? "Desktop connected"
                    : "Desktop reconnecting"
                }
                tone={s.capture_connected ? "success" : "warning"}
              />
              {s.spoken_language ? (
                <Badge label={s.spoken_language.toUpperCase()} />
              ) : null}
            </Row>
            <Txt style={{ fontSize: 22, fontWeight: "700", lineHeight: 29 }}>
              {s.title || "Desktop call"}
            </Txt>
            <Row>
              <Icon name="screen" size={16} />
              <Txt muted style={{ fontSize: 13 }}>
                {s.platform && s.platform !== "other"
                  ? s.platform
                      .replaceAll("_", " ")
                      .replace(/\b\w/g, (x) => x.toUpperCase())
                  : "Your computer"}{" "}
                · {s.mode === "interview" ? "Interview" : "Meeting"}
              </Txt>
            </Row>
            <Button
              label={
                attachingSession === s.session_id
                  ? "Connecting phone…"
                  : "Connect this phone"
              }
              icon="arrow"
              loading={attachingSession === s.session_id}
              disabled={attachBusy}
              onPress={() => void attach(s)}
            />
          </Card>
        ))}
        {attachBusy ? (
          <Button
            label="Cancel connection"
            secondary
            onPress={() => attachAbort.current?.abort()}
          />
        ) : null}
        {!refreshing && !sessions.length && !error ? (
          <EmptyState
            icon="screen"
            title="Ready when your call starts"
            description="On your computer, open CoRuby → Call and start a session using the same account and profile. Your call will appear here automatically."
            action={
              <Button
                label="Refresh calls"
                secondary
                icon="refresh"
                onPress={() => void refresh()}
              />
            }
          />
        ) : null}
      </ScrollView>
    );
  const session = sessions.find((s) => s.session_id === attachment.session_id);
  const spokenLanguage = live.spokenLanguage ?? session?.spoken_language;
  const usable = live.connected && !live.ended;
  return (
    <View style={{ flex: 1 }}>
      <View
        style={{
          borderBottomWidth: 1,
          borderColor: c.line,
          backgroundColor: c.card,
        }}
      >
        <View
          style={{
            width: "100%",
            maxWidth: 860,
            alignSelf: "center",
            paddingHorizontal: 22,
            paddingVertical: compact ? 4 : 14,
            gap: compact ? 4 : 10,
          }}
        >
          <Row style={{ justifyContent: "space-between" }}>
            <View style={{ flex: 1, gap: 5 }}>
              <Txt
                numberOfLines={1}
                style={{ fontSize: 17, fontWeight: "700" }}
              >
                {session?.title || "Desktop call"}
              </Txt>
              <Row>
                <View
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 3,
                    backgroundColor: usable ? c.success : c.warning,
                  }}
                />
                <Txt
                  muted
                  accessibilityLiveRegion="polite"
                  style={{ fontSize: 12, flexShrink: 1 }}
                >
                  {live.ended
                    ? "Call ended"
                    : usable
                      ? "Connected · audio from your computer"
                      : live.status}
                </Txt>
                {spokenLanguage ? (
                  <Badge label={spokenLanguage.toUpperCase()} />
                ) : null}
              </Row>
            </View>
            <IconButton
              label="Display settings"
              icon="settings"
              onPress={() => setDisplaySettings((v) => !v)}
            />
            <IconButton
              label="Disconnect this display"
              icon="close"
              onPress={() => void detach()}
            />
          </Row>
          {displaySettings ? (
            <Card subtle>
              <Select
                label="Spoken language"
                value={spokenLanguage ?? ""}
                options={languages}
                disabled={
                  !usable ||
                  !live.languageSwitching ||
                  !!pending.current ||
                  !languages.length
                }
                onChange={(value) => {
                  if (value !== spokenLanguage)
                    send("set_language", false, true, value);
                }}
              />
              <Txt muted style={{ fontSize: 12 }}>
                {live.languageSwitching
                  ? "Pause speaking briefly when switching. New speech and answers use the new language."
                  : "This server does not support changing the language during a call."}
              </Txt>
              {languageError ? (
                <Notice
                  tone="error"
                  action={
                    <Button
                      compact
                      secondary
                      label="Retry languages"
                      onPress={() => setLanguageAttempt((value) => value + 1)}
                    />
                  }
                >
                  {languageError}
                </Notice>
              ) : null}
              <Row style={{ justifyContent: "space-between" }}>
                <Txt style={{ fontSize: 13 }}>Reading size</Txt>
                <Row>
                  <Button
                    label="Smaller text"
                    compact
                    secondary
                    onPress={() => setSize((n) => Math.max(16, n - 2))}
                  />
                  <Button
                    label="Larger text"
                    compact
                    secondary
                    onPress={() => setSize((n) => Math.min(30, n + 2))}
                  />
                </Row>
              </Row>
              <Row style={{ justifyContent: "space-between" }}>
                <Txt style={{ fontSize: 13 }}>Keep screen awake</Txt>
                <Switch
                  accessibilityLabel="Keep screen awake"
                  value={keep}
                  onValueChange={setKeep}
                  trackColor={{ true: c.ruby }}
                />
              </Row>
            </Card>
          ) : null}
          {error ? <Notice tone="error">{error}</Notice> : null}
          {!usable && !live.ended ? (
            <Button
              label="Retry connection"
              secondary
              icon="refresh"
              onPress={() => {
                setError("");
                setReconnect((n) => n + 1);
              }}
            />
          ) : null}
        </View>
      </View>
      <ScrollView
        ref={scroll}
        contentContainerStyle={[page, { paddingTop: 22 }]}
        keyboardShouldPersistTaps="handled"
        onScrollBeginDrag={() => setLatest(false)}
        onContentSizeChange={() => {
          if (latest) scroll.current?.scrollToEnd({ animated: false });
        }}
      >
        {live.items.length === 0 ? (
          <EmptyState
            icon={usable ? "call" : "refresh"}
            title={
              live.ended
                ? "This call has ended"
                : usable
                  ? "Ready for your conversation"
                  : "Connecting to live guidance"
            }
            description={
              usable
                ? "Questions and answers will appear here when your computer hears the conversation. Check desktop audio if nothing appears, or type a question below."
                : live.ended
                  ? "Disconnect this display to return to your active calls."
                  : "Your desktop call keeps running. This phone will show guidance when its connection is ready."
            }
          />
        ) : null}
        {live.items.map((q, i) => (
          <Card key={q.id} style={{ padding: 22, gap: 18 }}>
            <Row style={{ justifyContent: "space-between" }}>
              <Badge label={`Question ${i + 1}`} tone="ruby" />
              {!q.done ? (
                <Txt muted style={{ fontSize: 11 }}>
                  Preparing answer…
                </Txt>
              ) : null}
            </Row>
            <Txt
              style={{
                fontSize: size + 1,
                lineHeight: (size + 1) * 1.4,
                fontWeight: "700",
                letterSpacing: -0.2,
              }}
            >
              {q.question}
            </Txt>
            <View style={{ height: 1, backgroundColor: c.line }} />
            <Txt selectable style={{ fontSize: size, lineHeight: size * 1.6 }}>
              {q.deep || q.fast || "Preparing guidance…"}
            </Txt>
          </Card>
        ))}
        {live.memory ? (
          <Txt muted style={{ fontSize: 12 }}>
            {live.memory}
          </Txt>
        ) : null}
      </ScrollView>
      {!latest ? (
        <View style={{ alignSelf: "center", marginBottom: 8 }}>
          <Button
            label="Return to latest"
            icon="arrow"
            compact
            secondary
            onPress={() => {
              setLatest(true);
              scroll.current?.scrollToEnd({ animated: true });
            }}
          />
        </View>
      ) : null}
      <View
        style={{
          borderTopWidth: 1,
          borderColor: c.line,
          backgroundColor: c.card,
        }}
      >
        <View
          style={{
            width: "100%",
            maxWidth: 860,
            alignSelf: "center",
            padding: compact ? 8 : 16,
            gap: compact ? 4 : 9,
          }}
        >
          {commandStatus ? (
            <Txt
              muted
              accessibilityLiveRegion="polite"
              style={{ fontSize: 12 }}
            >
              {commandStatus}
            </Txt>
          ) : null}
          {pending.current ? (
            <Button
              label="Retry pending command"
              secondary
              icon="refresh"
              disabled={!usable}
              onPress={() => {
                try {
                  if (socket.current?.readyState === 1 && pending.current)
                    socket.current.send(JSON.stringify(pending.current));
                } catch (e) {
                  setError(String(e));
                }
              }}
            />
          ) : null}
          <View
            style={{
              flexDirection: compact ? "row" : "column",
              gap: 9,
              alignItems: compact ? "center" : undefined,
            }}
          >
            <Input
              style={compact ? { flex: 1, minWidth: 0 } : undefined}
              placeholder="Ask a follow-up about this conversation…"
              value={draft}
              onChangeText={setDraft}
              maxLength={4000}
              returnKeyType="send"
              onSubmitEditing={() => {
                if (draft.trim() && usable) send("followup");
              }}
            />
            <Row
              style={
                compact ? { flexShrink: 0, flexWrap: "nowrap" } : undefined
              }
            >
              <Button
                label={draft.trim() ? "Ask follow-up" : "Answer now"}
                icon="arrow"
                style={compact ? undefined : { flex: 1 }}
                disabled={!usable || !!pending.current}
                onPress={() => send(draft.trim() ? "followup" : "answer")}
              />
              <Button
                label="End call"
                secondary
                danger
                icon="stop"
                disabled={!usable || !!pending.current}
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
      </View>
    </View>
  );
}
