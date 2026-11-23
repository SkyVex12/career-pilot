import React, { useEffect, useRef, useState } from "react";
import { AppState, FlatList, View } from "react-native";
import { randomUUID } from "expo-crypto";
import type { Api } from "./api";
import {
  AssistantConnection,
  emptyAssistant,
  parseSeats,
  seatGateway,
  type AssistantSeat,
  type SeatSocket,
} from "./assistantState";
import { AssistantScreen, AssistantVoice } from "./AssistantMedia";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  Notice,
  Row,
  Screen,
  ScreenTitle,
  Segments,
  Txt,
  page,
} from "./ui";
import { DEVELOPMENT_BUILD, HTTP_TEST_BUILD } from "./transport";
import { withDeadline } from "./requestScope";

const results: Record<string, string> = {
  pending: "Sending…",
  ok: "Delivered privately",
  denied: "The host disabled notes",
  expired: "Host unavailable · not delivered",
  rate_limited: "Wait a moment before sending another note",
  failed: "Not delivered",
  unconfirmed: "Delivery unconfirmed after disconnect",
};

export function Assist({
  api,
  url,
  visible,
}: {
  api: Api;
  url: string;
  visible: boolean;
}) {
  const [foreground, setForeground] = useState(
      AppState.currentState === "active",
    ),
    [seats, setSeats] = useState<AssistantSeat[]>([]),
    [seat, setSeat] = useState<AssistantSeat | null>(null);
  const [state, setState] = useState(emptyAssistant),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [refresh, setRefresh] = useState(0),
    [note, setNote] = useState("");
  const [panel, setPanel] = useState("guidance"),
    [unread, setUnread] = useState(false);
  const connection = useRef<AssistantConnection | null>(null),
    operation = useRef(0),
    mounted = useRef(true);
  const list = useRef<FlatList>(null),
    atBottom = useRef(true);
  const active = visible && foreground;
  useEffect(() => {
    const sub = AppState.addEventListener("change", (value) =>
      setForeground(value === "active"),
    );
    return () => sub.remove();
  }, []);
  useEffect(
    () => () => {
      mounted.current = false;
      operation.current++;
    },
    [],
  );
  useEffect(() => {
    if (!active || seat) return;
    let live = true,
      timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();
    async function poll() {
      if (!live) return;
      try {
        const result = await withDeadline(
          (signal) => api.request("/seats", undefined, "GET", true, signal),
          15000,
          controller.signal,
        );
        if (live) {
          setSeats(parseSeats(result));
          setError("");
        }
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (live) timer = setTimeout(() => void poll(), 10000);
      }
    }
    void poll();
    return () => {
      live = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [api, active, seat, refresh]);
  useEffect(() => {
    if (!seat) return;
    const profile = api.profile;
    const client = new AssistantConnection(
      seat,
      {
        token: (force) => {
          if (profile !== api.profile) throw new Error("The profile changed.");
          return api.token(force);
        },
        socket: (token) => {
          // React Native supports authenticated WebSocket headers. Tokens never enter URLs.
          const NativeSocket = WebSocket as unknown as new (
            url: string,
            protocols: string[] | null,
            options: { headers: Record<string, string> },
          ) => SeatSocket;
          return new NativeSocket(
            seatGateway(url, DEVELOPMENT_BUILD, HTTP_TEST_BUILD),
            null,
            { headers: { Authorization: `Bearer ${token}` } },
          );
        },
        schedule: (callback, ms) => setTimeout(callback, ms),
        cancel: (timer) => clearTimeout(timer),
      },
      (next) => {
        if (mounted.current) setState(next);
      },
    );
    connection.current = client;
    setState(emptyAssistant());
    return () => {
      client.close();
      if (connection.current === client) connection.current = null;
    };
  }, [api, url, seat]);
  useEffect(() => {
    if (active) connection.current?.resume();
    else connection.current?.pause();
  }, [active, seat]);
  async function invitation(
    selected: AssistantSeat,
    action: "accept" | "decline",
  ) {
    const ticket = ++operation.current;
    setBusy(true);
    setError("");
    try {
      await withDeadline(
        (signal) =>
          api.request(
            `/seats/${selected.seat_id}/${action}`,
            {},
            "POST",
            true,
            signal,
          ),
        15000,
      );
      if (mounted.current && ticket === operation.current)
        setRefresh((x) => x + 1);
    } catch (e) {
      if (mounted.current && ticket === operation.current)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mounted.current && ticket === operation.current) setBusy(false);
    }
  }
  function leave() {
    operation.current++;
    connection.current?.close();
    connection.current = null;
    setSeat(null);
    setState(emptyAssistant());
    setNote("");
    setError("");
    setBusy(false);
    setRefresh((x) => x + 1);
  }
  const mediaActive = active && state.connected && !state.terminal;
  if (!seat)
    return (
      <Screen>
        <ScreenTitle
          eyebrow="CALL"
          title="Assist a call"
          subtitle="Join an invitation from another CoRuby user. Follow their call and help privately, with the access they choose."
          action={
            <Button
              label="Refresh"
              secondary
              onPress={() => setRefresh((x) => x + 1)}
            />
          }
        />
        {error ? <Notice tone="error">{error}</Notice> : null}
        {seats.length ? (
          seats.map((invite) => (
            <Card key={invite.seat_id}>
              <Row style={{ justifyContent: "space-between" }}>
                <Txt style={{ fontSize: 18, fontWeight: "700", flex: 1 }}>
                  {invite.host_name}
                </Txt>
                <Badge
                  label={
                    invite.status === "admitted"
                      ? "Ready to join"
                      : invite.status === "accepted"
                        ? "Waiting for host"
                        : "Invitation"
                  }
                  tone={invite.status === "admitted" ? "success" : "neutral"}
                />
              </Row>
              <Txt>{invite.title || `${invite.mode} call`}</Txt>
              <Txt muted style={{ fontSize: 12 }}>
                {[
                  "Live guidance",
                  invite.capabilities.includes("note") && "Private notes",
                  invite.capabilities.includes("voice") && "Voice",
                  invite.capabilities.includes("device.view") && "Screen",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </Txt>
              <Row>
                {invite.status === "admitted" ? (
                  <Button
                    label="Join as assistant"
                    icon="arrow"
                    onPress={() => {
                      setSeat(invite);
                      setPanel("guidance");
                    }}
                  />
                ) : invite.status === "invited" ? (
                  <Button
                    label="Accept invitation"
                    loading={busy}
                    onPress={() => void invitation(invite, "accept")}
                  />
                ) : (
                  <Txt muted>
                    The host will let you in. This page updates automatically.
                  </Txt>
                )}
                {invite.status === "invited" ? (
                  <Button
                    label="Decline"
                    secondary
                    disabled={busy}
                    onPress={() => void invitation(invite, "decline")}
                  />
                ) : null}
              </Row>
            </Card>
          ))
        ) : !error ? (
          <EmptyState
            icon="call"
            title="No invitations yet"
            description="Ask the host to invite the email you use for this account from their call’s Assistants menu. Invitations appear here automatically."
          />
        ) : null}
      </Screen>
    );
  const shared = state.sharing && state.hostConnected && !state.terminal;
  return (
    <View style={{ flex: 1 }}>
      <View style={[page, { paddingBottom: 12, gap: 12 }]}>
        <Row style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
          <View style={{ flex: 1, gap: 4 }}>
            <Txt style={{ fontSize: 20, fontWeight: "700" }}>
              Assisting {seat.host_name}
            </Txt>
            <Txt muted style={{ fontSize: 12 }}>
              {seat.title || `${seat.mode} call`}
            </Txt>
          </View>
          <Button label="Leave" secondary onPress={leave} />
        </Row>
        <Badge
          label={
            state.connected
              ? state.sharing
                ? "Connected"
                : "Host paused sharing"
              : state.status
          }
          tone={state.connected ? "success" : "warning"}
        />
        {state.terminal ? (
          <Notice>
            {state.status} The host’s call is unaffected by leaving this view.
          </Notice>
        ) : (
          <Segments
            value={panel}
            onChange={setPanel}
            options={[
              { value: "guidance", label: "Guidance" },
              { value: "transcript", label: "Transcript" },
              ...(state.capabilities.includes("device.view")
                ? [{ value: "screen", label: "Screen" }]
                : []),
              { value: "notes", label: "Notes" },
            ]}
          />
        )}
      </View>
      {!state.terminal &&
      panel === "screen" &&
      state.capabilities.includes("device.view") ? (
        <Screen>
          {mediaActive ? (
            <AssistantScreen api={api} seatId={seat.seat_id} />
          ) : (
            <Notice>
              The screen reconnects when your assistant connection is ready.
            </Notice>
          )}
        </Screen>
      ) : !state.terminal && panel === "notes" ? (
        <Screen>
          <Txt style={{ fontWeight: "700", fontSize: 18 }}>
            Private help for the host
          </Txt>
          <Txt muted>
            Notes and your voice go only to the host. They are not added to the
            meeting transcript.
          </Txt>
          {state.capabilities.includes("note") ? (
            <Card>
              <Input
                placeholder="Send the host a private note"
                value={note}
                onChangeText={setNote}
                multiline
                maxLength={1000}
                style={{ minHeight: 110 }}
              />
              <Button
                label="Send note"
                disabled={!note.trim() || !state.connected}
                onPress={() => {
                  try {
                    connection.current?.note(randomUUID(), note);
                    setNote("");
                    setError("");
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  }
                }}
              />
            </Card>
          ) : (
            <Notice>
              The host has not enabled private notes for this seat.
            </Notice>
          )}
          {[...state.notes].reverse().map((item) => (
            <Card key={item.id}>
              <Txt>{item.text}</Txt>
              <Txt muted style={{ fontSize: 12 }}>
                {results[item.result] ?? "Not delivered"}
              </Txt>
            </Card>
          ))}
          {error ? <Notice tone="error">{error}</Notice> : null}
        </Screen>
      ) : !state.terminal && !shared ? (
        <Screen>
          <Notice>
            {state.hostConnected
              ? "The host paused sharing. New transcript and guidance will appear when they resume."
              : "The host’s desktop is reconnecting. No phone microphone is used."}
          </Notice>
        </Screen>
      ) : !state.terminal ? (
        <View style={{ flex: 1 }}>
          <FlatList
            ref={list}
            key={panel}
            data={
              panel === "transcript"
                ? state.transcript.map((item) => ({
                    id: item.id,
                    title: item.speaker,
                    body: item.text,
                    detail: "",
                  }))
                : state.live.items.map((item) => ({
                    id: item.id,
                    title: item.question,
                    body: item.fast,
                    detail: item.deep,
                  }))
            }
            keyExtractor={(item) => item.id}
            contentContainerStyle={page}
            ItemSeparatorComponent={() => <View style={{ height: 12 }} />}
            keyboardShouldPersistTaps="handled"
            onScroll={(event) => {
              const { contentOffset, contentSize, layoutMeasurement } =
                event.nativeEvent;
              atBottom.current =
                contentSize.height -
                  contentOffset.y -
                  layoutMeasurement.height <
                60;
              if (atBottom.current) setUnread(false);
            }}
            scrollEventThrottle={100}
            onContentSizeChange={() => {
              if (atBottom.current)
                list.current?.scrollToEnd({ animated: false });
              else setUnread(true);
            }}
            ListEmptyComponent={
              <EmptyState
                icon="call"
                title={
                  panel === "transcript"
                    ? "Waiting for speech"
                    : "Waiting for guidance"
                }
                description="The host’s live content appears here as their call continues."
              />
            }
            renderItem={({ item }) => (
              <Card>
                <Txt selectable style={{ fontWeight: "700", fontSize: 17 }}>
                  {item.title}
                </Txt>
                {item.body ? (
                  <Txt selectable>{item.body}</Txt>
                ) : (
                  <Txt muted>Preparing the answer…</Txt>
                )}
                {item.detail ? (
                  <Txt selectable muted>
                    {item.detail}
                  </Txt>
                ) : null}
              </Card>
            )}
          />
          {unread ? (
            <View style={{ position: "absolute", right: 20, bottom: 16 }}>
              <Button
                label="Latest"
                icon="arrow"
                onPress={() => {
                  atBottom.current = true;
                  setUnread(false);
                  list.current?.scrollToEnd({ animated: true });
                }}
              />
            </View>
          ) : null}
        </View>
      ) : null}
      {mediaActive && state.capabilities.includes("voice") ? (
        <View style={{ paddingHorizontal: 20, paddingBottom: 10 }}>
          <AssistantVoice api={api} seatId={seat.seat_id} allowed />
        </View>
      ) : null}
    </View>
  );
}
