import React, { useContext, useEffect, useRef, useState } from "react";
import { AppState, Modal, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { randomUUID } from "expo-crypto";
import { assistantAudio } from "./assistantAudio";
import {
  Room,
  RoomEvent,
  type RemoteParticipant,
  type RemoteTrackPublication,
} from "livekit-client";
import type { Api } from "./api";
import { updateAssistantPermissions } from "./hostAssistantPermissions";
import {
  Button,
  Card,
  Field,
  Input,
  Notice,
  Row,
  Screen,
  ScreenTitle,
  Select,
  Txt,
  Theme,
} from "./ui";
import { withDeadline } from "./requestScope";
import { DEVELOPMENT_BUILD, HTTP_TEST_BUILD, transportURL } from "./transport";

type HostSeat = {
  seat_id: string;
  name: string;
  email: string;
  status: string;
  capabilities: string[];
  connected?: boolean;
};
type SeatList = { seats: HostSeat[]; voice?: boolean; screen?: boolean };
const open = (seat: HostSeat) =>
  ["invited", "accepted", "admitted"].includes(seat.status);
/** Host voice receives only explicitly permitted assistant identities; never publishes a mic. */
function HostListener({
  api,
  sessionId,
  seats,
}: {
  api: Api;
  sessionId: string;
  seats: HostSeat[];
}) {
  const [error, setError] = useState(""),
    room = useRef<Room | null>(null),
    allowed = useRef(new Set<string>());
  allowed.current = new Set(
    seats
      .filter(
        (s) => s.status === "admitted" && s.capabilities.includes("voice"),
      )
      .map((s) => `seat:${s.seat_id}`),
  );
  function subscriptions() {
    for (const participant of room.current?.remoteParticipants.values() ?? [])
      for (const publication of participant.audioTrackPublications.values())
        publication.setSubscribed(allowed.current.has(participant.identity));
  }
  useEffect(subscriptions, [seats]);
  useEffect(() => {
    let active = true;
    const abort = new AbortController();
    const deviceId = randomUUID();
    const audioOwner = {};
    let next: Room | null = null;
    void (async () => {
      try {
        const grant = await withDeadline(
          (signal) =>
            api.request<{ url: string; token: string; identity: string }>(
              `/sessions/${sessionId}/voice?device_id=${deviceId}`,
              {},
              "POST",
              true,
              signal,
            ),
          15000,
          abort.signal,
        );
        if (!active) return;
        if (grant.identity !== `host:${deviceId}`)
          throw new Error(
            "Update the server to listen on this phone without interrupting desktop audio.",
          );
        transportURL(
          grant.url,
          "websocket",
          DEVELOPMENT_BUILD,
          HTTP_TEST_BUILD,
        );
        await assistantAudio.start(audioOwner, () => active);
        if (!active) {
          await assistantAudio.stop(audioOwner);
          return;
        }
        next = new Room();
        room.current = next;
        next.on(
          RoomEvent.TrackPublished,
          (
            publication: RemoteTrackPublication,
            participant: RemoteParticipant,
          ) => {
            if (active)
              publication.setSubscribed(
                allowed.current.has(participant.identity),
              );
          },
        );
        next.on(RoomEvent.Reconnected, subscriptions);
        next.on(RoomEvent.Disconnected, () => {
          void assistantAudio.stop(audioOwner).catch(() => {});
          if (active)
            setError(
              "Assistant audio disconnected. Turn listening off and back on to reconnect.",
            );
        });
        await withDeadline(
          () => next!.connect(grant.url, grant.token, { autoSubscribe: false }),
          15000,
          abort.signal,
        );
        if (!active) {
          await next.disconnect();
          return;
        }
        subscriptions();
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : String(e));
        if (next) await next.disconnect().catch(() => {});
        await assistantAudio.stop(audioOwner).catch(() => {});
      }
    })();
    return () => {
      active = false;
      abort.abort();
      const previous = room.current;
      room.current = null;
      if (previous) void previous.disconnect().catch(() => {});
      void assistantAudio.stop(audioOwner).catch(() => {});
    };
  }, [api, sessionId]);
  return error ? (
    <Notice tone="error">{error}</Notice>
  ) : (
    <Txt muted style={{ fontSize: 12 }}>
      Assistant voice plays on this phone. Use headphones to keep it private.
    </Txt>
  );
}
export function HostAssistants({
  api,
  sessionId,
  visible = true,
  connected,
  sharing,
  canShare = false,
  onSharing,
  recording = false,
}: {
  api: Api;
  sessionId: string;
  visible?: boolean;
  connected: boolean;
  sharing: boolean;
  canShare?: boolean;
  onSharing: (on: boolean) => void;
  recording?: boolean;
}) {
  const colors = useContext(Theme);
  const [expanded, setExpanded] = useState(false),
    [foreground, setForeground] = useState(AppState.currentState === "active"),
    [list, setList] = useState<SeatList>({ seats: [] }),
    [email, setEmail] = useState(""),
    [caps, setCaps] = useState("off"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0),
    [listen, setListen] = useState(false);
  const [sharingRequest, setSharingRequest] = useState<boolean | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    if (sharingRequest === null) return;
    if (sharing === sharingRequest) {
      setSharingRequest(null);
      return;
    }
    const timer = setTimeout(() => {
      setSharingRequest(null);
      setError(
        "The host did not confirm this sharing change. Check the call connection and try again.",
      );
    }, 15000);
    return () => clearTimeout(timer);
  }, [sharing, sharingRequest]);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  useEffect(() => {
    const sub = AppState.addEventListener("change", (value) => {
      const on = value === "active";
      setForeground(on);
      if (!on) setListen(false);
    });
    return () => sub.remove();
  }, []);
  useEffect(() => {
    if (!visible || !connected || recording) setListen(false);
  }, [visible, connected, recording]);
  useEffect(() => {
    if (!visible || !foreground || !connected) return;
    let active = true,
      timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();
    async function poll() {
      try {
        const value = await withDeadline(
          (signal) =>
            api.request<SeatList>(
              `/sessions/${sessionId}/seats`,
              undefined,
              "GET",
              true,
              signal,
            ),
          15000,
          controller.signal,
        );
        if (!Array.isArray(value.seats))
          throw new Error("Assistant list unavailable.");
        if (active) setList(value);
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (active)
          timer = setTimeout(
            () => void poll(),
            expanded || listen ? 5000 : 15000,
          );
      }
    }
    void poll();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [
    api,
    sessionId,
    foreground,
    visible,
    connected,
    revision,
    expanded,
    listen,
  ]);
  async function action(run: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await run();
      if (mounted.current) setRevision((x) => x + 1);
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  function setCapabilities(seat: HostSeat, next: string[]) {
    return updateAssistantPermissions(
      api,
      sessionId,
      seat.seat_id,
      seat.capabilities,
      next,
      () => {
        if (mounted.current) setRevision((value) => value + 1);
      },
    );
  }
  function capabilities(
    value: string,
    previous = ["view", "note", ...(list.voice ? ["voice"] : [])],
  ) {
    return [
      ...previous.filter((x) => x !== "device.view" && x !== "device.control"),
      ...(value !== "off" ? ["device.view"] : []),
    ];
  }
  const count = list.seats.filter(open).length;
  return (
    <View style={{ gap: 8 }}>
      <Row>
        <Button
          label={`Assistants${count ? ` · ${count}` : ""}`}
          secondary
          disabled={!connected}
          onPress={() => setExpanded(true)}
        />
        {list.voice &&
        list.seats.some(
          (s) => s.status === "admitted" && s.capabilities.includes("voice"),
        ) ? (
          <Button
            label={listen ? "Mute assistants on phone" : "Listen to assistants"}
            secondary
            disabled={!connected || recording}
            onPress={() => setListen((value) => !value)}
          />
        ) : null}
      </Row>
      {listen && visible && foreground && connected && !recording ? (
        <HostListener api={api} sessionId={sessionId} seats={list.seats} />
      ) : null}
      <Modal
        visible={expanded && visible}
        animationType="slide"
        onRequestClose={() => setExpanded(false)}
      >
        <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
          <Screen>
            <ScreenTitle
              title="Assistants"
              subtitle="Invite someone to follow this call and help privately."
              action={
                <Button
                  label="Done"
                  secondary
                  onPress={() => setExpanded(false)}
                />
              }
            />
            {canShare ? (
              <Button
                label={
                  sharing
                    ? "Pause transcript and guidance sharing"
                    : "Resume sharing with assistants"
                }
                secondary
                disabled={!connected || busy}
                loading={sharingRequest !== null}
                onPress={() => {
                  setError("");
                  setSharingRequest(!sharing);
                  onSharing(!sharing);
                }}
              />
            ) : null}
            <Card>
              <Field label="Assistant email">
                <Input
                  value={email}
                  onChangeText={setEmail}
                  placeholder="name@example.com"
                  autoCapitalize="none"
                  keyboardType="email-address"
                />
              </Field>
              {list.screen ? (
                <Select
                  label="Screen access"
                  value={caps}
                  options={[
                    { value: "off", label: "No screen access" },
                    { value: "view", label: "View desktop screen" },
                  ]}
                  onChange={setCaps}
                />
              ) : null}
              <Txt muted style={{ fontSize: 12 }}>
                Notes and available voice are included. Desktop screen sharing
                starts only on the computer; remote control always needs the
                host’s approval.
              </Txt>
              <Button
                label="Send invitation"
                loading={busy}
                disabled={
                  !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim()) ||
                  count >= 2 ||
                  !connected
                }
                onPress={() =>
                  void action(async () => {
                    await api.request(
                      `/sessions/${sessionId}/seats`,
                      { email: email.trim(), capabilities: capabilities(caps) },
                      "POST",
                    );
                    if (mounted.current) setEmail("");
                  })
                }
              />
              {count >= 2 ? (
                <Txt muted>
                  Two assistants maximum. Remove one before inviting another.
                </Txt>
              ) : null}
            </Card>
            {list.seats.filter(open).map((seat) => (
              <Card key={seat.seat_id}>
                <Txt style={{ fontWeight: "700" }}>
                  {seat.name || seat.email}
                </Txt>
                <Txt muted>
                  {seat.email} ·{" "}
                  {seat.status === "admitted"
                    ? seat.connected
                      ? "Connected"
                      : "Ready to join"
                    : seat.status}
                </Txt>
                {seat.status === "accepted" ? (
                  <Button
                    label="Let in"
                    disabled={busy}
                    onPress={() =>
                      void action(() =>
                        api.request(
                          `/sessions/${sessionId}/seats/${seat.seat_id}`,
                          { admit: true },
                          "PATCH",
                        ),
                      )
                    }
                  />
                ) : null}
                {list.screen ? (
                  <Select
                    label={`Screen access for ${seat.name || seat.email}`}
                    value={
                      seat.capabilities.includes("device.view") ? "view" : "off"
                    }
                    disabled={busy}
                    options={[
                      { value: "off", label: "No screen access" },
                      {
                        value: "view",
                        label:
                          "View desktop screen · control requires approval",
                      },
                    ]}
                    onChange={(value) =>
                      void action(() =>
                        setCapabilities(
                          seat,
                          capabilities(value, seat.capabilities),
                        ),
                      )
                    }
                  />
                ) : null}
                <Row>
                  <Button
                    label={
                      seat.capabilities.includes("note")
                        ? "Disable notes"
                        : "Allow notes"
                    }
                    secondary
                    disabled={busy}
                    onPress={() =>
                      void action(() =>
                        setCapabilities(
                          seat,
                          seat.capabilities.includes("note")
                            ? seat.capabilities.filter((x) => x !== "note")
                            : [...seat.capabilities, "note"],
                        ),
                      )
                    }
                  />
                  {list.voice ? (
                    <Button
                      label={
                        seat.capabilities.includes("voice")
                          ? "Disable voice"
                          : "Allow voice"
                      }
                      secondary
                      disabled={busy}
                      onPress={() =>
                        void action(() =>
                          setCapabilities(
                            seat,
                            seat.capabilities.includes("voice")
                              ? seat.capabilities.filter((x) => x !== "voice")
                              : [...seat.capabilities, "voice"],
                          ),
                        )
                      }
                    />
                  ) : null}
                  <Button
                    label="Remove"
                    secondary
                    danger
                    disabled={busy}
                    onPress={() =>
                      void action(() =>
                        api.request(
                          `/sessions/${sessionId}/seats/${seat.seat_id}`,
                          undefined,
                          "DELETE",
                        ),
                      )
                    }
                  />
                </Row>
              </Card>
            ))}
            {recording ? (
              <Notice>
                Pause the phone microphone before listening to assistant voice,
                so it is not recorded into the call.
              </Notice>
            ) : null}
            {error ? <Notice tone="error">{error}</Notice> : null}
          </Screen>
        </SafeAreaView>
      </Modal>
    </View>
  );
}
