import React, { useContext, useEffect, useRef, useState } from "react";
import {
  AppState,
  Pressable,
  ScrollView,
  View,
  useWindowDimensions,
} from "react-native";
import { VideoTrack, type TrackReference } from "@livekit/react-native";
import {
  Room,
  RoomEvent,
  Track,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import { requestRecordingPermissionsAsync } from "expo-audio";
import { randomUUID } from "expo-crypto";
import { assistantAudio } from "./assistantAudio";
import type { Api } from "./api";
import {
  Button,
  Card,
  Field,
  Input,
  Notice,
  Row,
  Select,
  Theme,
  Txt,
} from "./ui";
import {
  controlReply,
  keyStroke,
  qualities,
  screenPoint,
  textKeys,
  type RemoteInput,
  type ScreenConsent,
  type ScreenQuality,
} from "./assistantScreen";
import { DEVELOPMENT_BUILD, HTTP_TEST_BUILD, transportURL } from "./transport";
import { withDeadline } from "./requestScope";

const encoder = new TextEncoder(),
  decoder = new TextDecoder();
/** No microphone is requested until the user taps Talk. Leaving/backgrounding stops tracks. */
export function AssistantVoice({
  api,
  seatId,
  allowed,
}: {
  api: Api;
  seatId: string;
  allowed: boolean;
}) {
  const room = useRef<Room | null>(null),
    epoch = useRef(0);
  const audioOwner = useRef({}).current;
  const [talking, setTalking] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function stop() {
    epoch.current++;
    const active = room.current;
    room.current = null;
    setTalking(false);
    setBusy(false);
    if (active) {
      await active.localParticipant.setMicrophoneEnabled(false).catch(() => {});
      await active.disconnect().catch(() => {});
    }
    await assistantAudio.stop(audioOwner).catch(() => {});
  }
  useEffect(() => {
    if (!allowed) void stop();
    return () => {
      void stop();
    };
  }, [allowed, seatId]);
  async function talk() {
    if (talking || busy) {
      await stop();
      return;
    }
    const ticket = ++epoch.current;
    setBusy(true);
    setError("");
    const current = () =>
      epoch.current === ticket && allowed && AppState.currentState === "active";
    let next: Room | null = null;
    try {
      const permission = await requestRecordingPermissionsAsync();
      if (!current()) return;
      if (!permission.granted)
        throw new Error(
          "Allow microphone access to speak privately to the host.",
        );
      const grant = await withDeadline(
        (signal) =>
          api.request<{ url: string; token: string }>(
            `/seats/${seatId}/voice`,
            {},
            "POST",
            true,
            signal,
          ),
        15000,
      );
      if (!current()) return;
      transportURL(grant.url, "websocket", DEVELOPMENT_BUILD, HTTP_TEST_BUILD);
      await assistantAudio.start(audioOwner, current);
      if (!current()) {
        await assistantAudio.stop(audioOwner);
        return;
      }
      next = new Room();
      room.current = next;
      next.on(RoomEvent.Disconnected, () => {
        if (current()) {
          setError("Voice disconnected. Tap Talk to reconnect.");
          void stop();
        }
      });
      next.on(RoomEvent.Reconnecting, () => {
        if (current()) {
          setError("Voice interrupted. Tap Talk when connected again.");
          void stop();
        }
      });
      await withDeadline(
        () => next!.connect(grant.url, grant.token, { autoSubscribe: false }),
        15000,
      );
      if (!current()) {
        await next.disconnect();
        return;
      }
      await next.localParticipant.setMicrophoneEnabled(true, {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      });
      if (!current()) {
        await next.localParticipant.setMicrophoneEnabled(false);
        await next.disconnect();
        return;
      }
      setTalking(true);
    } catch (e) {
      if (current()) {
        setError(e instanceof Error ? e.message : String(e));
        await stop();
      } else if (next) await next.disconnect().catch(() => {});
    } finally {
      if (current()) setBusy(false);
    }
  }
  if (!allowed) return null;
  return (
    <View style={{ gap: 8 }}>
      <Button
        label={
          talking
            ? "Mute microphone"
            : busy
              ? "Connecting voice…"
              : "Talk to host"
        }
        icon="mic"
        secondary={!talking}
        onPress={() => void talk()}
        loading={busy}
      />
      <Txt muted style={{ fontSize: 12 }}>
        {talking
          ? "Microphone on · Only the host hears you."
          : "Your microphone stays off until you tap Talk."}
      </Txt>
      {error ? <Notice tone="error">{error}</Notice> : null}
    </View>
  );
}

export function AssistantScreen({ api, seatId }: { api: Api; seatId: string }) {
  const c = useContext(Theme),
    dimensions = useWindowDimensions();
  const room = useRef<Room | null>(null),
    consent = useRef<ScreenConsent>({ requestId: null, controlling: false });
  const [control, setControl] = useState(consent.current),
    [canRequest, setCanRequest] = useState(false);
  const [track, setTrack] = useState<TrackReference>(),
    [status, setStatus] = useState("Connecting shared screen…"),
    [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0),
    [quality, setQuality] = useState<ScreenQuality>("1080p"),
    [actual, setActual] = useState("");
  const [size, setSize] = useState({ width: 1920, height: 1080 }),
    [width, setWidth] = useState(0),
    [zoom, setZoom] = useState(1),
    [typing, setTyping] = useState("");
  const requestTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const down = useRef<{ x: number; y: number } | null>(null),
    lastMove = useRef(0),
    qualityRef = useRef<ScreenQuality>(quality);
  function clearRequest() {
    if (requestTimer.current) clearTimeout(requestTimer.current);
    requestTimer.current = null;
  }
  function updateConsent(value: ScreenConsent) {
    consent.current = value;
    setControl(value);
    if (!value.requestId) clearRequest();
  }
  async function publish(topic: string, payload: unknown, reliable = true) {
    if (!room.current) throw new Error("The screen is disconnected.");
    await room.current.localParticipant.publishData(
      encoder.encode(JSON.stringify(payload)),
      { topic, reliable, destinationIdentities: ["host"] },
    );
  }
  async function release() {
    updateConsent({ requestId: null, controlling: false });
    down.current = null;
    await publish("release", {}).catch(() => {});
  }
  useEffect(() => {
    let active = true,
      retry: ReturnType<typeof setTimeout> | null = null,
      tries = 0;
    const abort = new AbortController();
    async function connect() {
      if (!active) return;
      setStatus("Connecting shared screen…");
      setTrack(undefined);
      updateConsent({ requestId: null, controlling: false });
      let next: Room | null = null;
      try {
        const grant = await withDeadline(
          (signal) =>
            api.request<{
              url: string;
              token: string;
              control?: boolean;
              can_request_control?: boolean;
            }>(`/seats/${seatId}/screen`, {}, "POST", true, signal),
          15000,
          abort.signal,
        );
        if (!active) return;
        transportURL(
          grant.url,
          "websocket",
          DEVELOPMENT_BUILD,
          HTTP_TEST_BUILD,
        );
        setCanRequest(
          grant.can_request_control === true || grant.control === true,
        );
        next = new Room({ adaptiveStream: false, dynacast: false });
        room.current = next;
        const owns = () => active && room.current === next;
        next.on(
          RoomEvent.TrackSubscribed,
          (
            remote: RemoteTrack,
            publication: RemoteTrackPublication,
            participant: RemoteParticipant,
          ) => {
            if (
              !owns() ||
              participant.identity !== "host" ||
              remote.kind !== Track.Kind.Video
            )
              return;
            setTrack({
              participant,
              publication,
              source: Track.Source.ScreenShare,
            });
            if (publication.dimensions?.width && publication.dimensions.height)
              setSize(publication.dimensions);
            setStatus("Screen shared by host");
            void publish("screen_quality", {
              quality: qualityRef.current,
            }).catch(() => {});
          },
        );
        next.on(RoomEvent.TrackUnsubscribed, () => {
          if (owns()) {
            setTrack(undefined);
            setStatus("Waiting for the host to share their screen.");
            void release();
          }
        });
        next.on(RoomEvent.Reconnecting, () => {
          if (owns()) {
            setStatus("Screen reconnecting…");
            setTrack(undefined);
            void release();
          }
        });
        next.on(RoomEvent.Reconnected, () => {
          if (owns()) {
            setStatus("Connected. Waiting for the shared screen.");
            for (const participant of next!.remoteParticipants.values())
              if (participant.identity === "host")
                for (const publication of participant.videoTrackPublications.values())
                  if (publication.track)
                    setTrack({
                      participant,
                      publication,
                      source: Track.Source.ScreenShare,
                    });
          }
        });
        next.on(
          RoomEvent.DataReceived,
          (
            payload: Uint8Array,
            sender?: RemoteParticipant,
            _kind?: unknown,
            topic?: string,
          ) => {
            if (!owns() || sender?.identity !== "host" || payload.length > 8192)
              return;
            try {
              const data = JSON.parse(decoder.decode(payload));
              if (topic === "control") {
                updateConsent(
                  controlReply(consent.current, sender.identity, data),
                );
                if (data.on === false && data.reason)
                  setError(String(data.reason));
              }
              if (topic === "screen_quality_status") {
                if (qualities.includes(data.quality)) {
                  setQuality(data.quality);
                  qualityRef.current = data.quality;
                }
                if (
                  Number.isFinite(data.width) &&
                  Number.isFinite(data.height) &&
                  data.width > 0 &&
                  data.height > 0
                ) {
                  setSize({ width: data.width, height: data.height });
                  setActual(`${data.width} × ${data.height}`);
                }
                if (data.error) setError(String(data.error));
                else setError("");
              }
            } catch {
              /* Malformed peer metadata has no authority. */
            }
          },
        );
        next.on(RoomEvent.Disconnected, () => {
          if (!owns()) return;
          room.current = null;
          setTrack(undefined);
          updateConsent({ requestId: null, controlling: false });
          if (tries++ < 3) {
            setStatus("Screen reconnecting…");
            retry = setTimeout(() => void connect(), 1000 * tries);
          } else {
            setStatus("Screen disconnected.");
            setError("Try connecting the screen again.");
          }
        });
        await withDeadline(
          () => next!.connect(grant.url, grant.token, { autoSubscribe: true }),
          15000,
          abort.signal,
        );
        if (!owns()) {
          await next.disconnect();
          return;
        }
        setStatus("Connected. The host chooses when to share their screen.");
      } catch (e) {
        if (active) {
          room.current = null;
          setError(e instanceof Error ? e.message : String(e));
          setStatus("Screen unavailable");
        }
        if (next) await next.disconnect().catch(() => {});
      }
    }
    void connect();
    return () => {
      active = false;
      abort.abort();
      if (retry) clearTimeout(retry);
      clearRequest();
      const previous = room.current;
      room.current = null;
      consent.current = { requestId: null, controlling: false };
      if (previous) {
        void previous.localParticipant
          .publishData(encoder.encode("{}"), {
            topic: "release",
            reliable: true,
            destinationIdentities: ["host"],
          })
          .catch(() => {});
        void previous.disconnect().catch(() => {});
      }
    };
  }, [api, seatId, attempt]);
  async function guarded(action: () => Promise<void>) {
    try {
      setError("");
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  async function send(events: RemoteInput[]) {
    if (consent.current.controlling && track)
      await publish("input", { events }, !events.every((e) => e.t === "move"));
  }
  const canvasWidth = (width || Math.min(dimensions.width - 76, 760)) * zoom,
    canvasHeight = (canvasWidth * size.height) / size.width;
  return (
    <Card>
      <Row style={{ justifyContent: "space-between" }}>
        <Txt style={{ fontWeight: "700" }}>Shared screen</Txt>
        <Button
          label="Reconnect"
          secondary
          onPress={() => setAttempt((x) => x + 1)}
        />
      </Row>
      <Txt muted style={{ fontSize: 12 }}>
        {status}
        {actual ? ` · ${actual}` : ""}
      </Txt>
      <View
        onLayout={(e) => setWidth(e.nativeEvent.layout.width)}
        style={{
          borderRadius: 12,
          overflow: "hidden",
          backgroundColor: "#111",
          height: Math.min(
            440,
            Math.max(180, ((width || 300) * size.height) / size.width),
          ),
        }}
      >
        {track ? (
          <ScrollView
            nestedScrollEnabled
            scrollEnabled={!control.controlling}
            contentContainerStyle={{ height: canvasHeight }}
          >
            <ScrollView
              horizontal
              nestedScrollEnabled
              scrollEnabled={!control.controlling}
              contentContainerStyle={{ width: canvasWidth }}
            >
              <View style={{ width: canvasWidth, height: canvasHeight }}>
                <VideoTrack
                  trackRef={track}
                  style={{ width: canvasWidth, height: canvasHeight }}
                  objectFit="contain"
                />
                {control.controlling ? (
                  <View
                    style={{
                      position: "absolute",
                      top: 0,
                      bottom: 0,
                      left: 0,
                      right: 0,
                    }}
                    accessible
                    accessibilityLabel="Remote desktop touch control"
                    onStartShouldSetResponder={() => true}
                    onMoveShouldSetResponder={() => true}
                    onResponderGrant={(e) => {
                      const point = screenPoint(
                        e.nativeEvent.locationX,
                        e.nativeEvent.locationY,
                        canvasWidth,
                        canvasHeight,
                      );
                      if (point) {
                        down.current = point;
                        void guarded(() =>
                          send([{ t: "down", button: 0, ...point }]),
                        );
                      }
                    }}
                    onResponderMove={(e) => {
                      if (Date.now() - lastMove.current < 40) return;
                      lastMove.current = Date.now();
                      const point = screenPoint(
                        e.nativeEvent.locationX,
                        e.nativeEvent.locationY,
                        canvasWidth,
                        canvasHeight,
                      );
                      if (point) {
                        down.current = point;
                        void guarded(() => send([{ t: "move", ...point }]));
                      }
                    }}
                    onResponderRelease={() => {
                      if (down.current) {
                        const point = down.current;
                        down.current = null;
                        void guarded(() =>
                          send([{ t: "up", button: 0, ...point }]),
                        );
                      }
                    }}
                    onResponderTerminate={() => void release()}
                  />
                ) : null}
              </View>
            </ScrollView>
          </ScrollView>
        ) : (
          <View style={{ flex: 1, justifyContent: "center", padding: 20 }}>
            <Txt muted style={{ textAlign: "center" }}>
              The host’s shared screen appears here.
            </Txt>
          </View>
        )}
      </View>
      <Row>
        <View style={{ flex: 1 }}>
          <Select
            label="Screen resolution"
            value={quality}
            options={qualities.map((value) => ({
              value,
              label: value === "2160p" ? "4K · 2160p" : value,
            }))}
            disabled={!track}
            onChange={(value) => {
              const next = value as ScreenQuality;
              qualityRef.current = next;
              setQuality(next);
              void guarded(() => publish("screen_quality", { quality: next }));
            }}
          />
        </View>
        <View style={{ flex: 1 }}>
          <Select
            label="Zoom"
            value={String(zoom)}
            options={[1, 1.5, 2, 3].map((value) => ({
              value: String(value),
              label: value === 1 ? "Fit screen" : `${value}×`,
            }))}
            onChange={(value) => setZoom(Number(value))}
          />
        </View>
      </Row>
      <Txt muted style={{ fontSize: 12 }}>
        Higher resolution improves text clarity and uses more data. Zoom and pan
        to read small text.
      </Txt>
      {canRequest ? (
        <Button
          label={
            control.controlling
              ? "Stop remote control"
              : control.requestId
                ? "Cancel control request"
                : "Request remote control"
          }
          secondary={!control.controlling}
          disabled={!track}
          onPress={() =>
            void guarded(async () => {
              if (control.controlling || control.requestId) return release();
              const requestId = randomUUID();
              updateConsent({ controlling: false, requestId });
              requestTimer.current = setTimeout(() => {
                void release();
                setError(
                  "The control request expired. Ask again when the host is ready.",
                );
              }, 60000);
              try {
                await publish("ask", { requestId });
              } catch (e) {
                updateConsent({ controlling: false, requestId: null });
                throw e;
              }
            })
          }
        />
      ) : null}
      {control.requestId ? (
        <Notice>
          The host must approve your request before any touch or key can control
          their computer.
        </Notice>
      ) : null}
      {control.controlling ? (
        <View style={{ gap: 12 }}>
          <Notice tone="success">
            The host approved remote control. Tap to click, drag to move, or use
            the keys below. Either person can stop control.
          </Notice>
          <Row>
            {[
              "Escape",
              "Tab",
              "Enter",
              "Backspace",
              "ArrowLeft",
              "ArrowRight",
              "ArrowUp",
              "ArrowDown",
            ].map((code) => (
              <Button
                key={code}
                label={code.replace("Arrow", "")}
                secondary
                onPress={() => void guarded(() => send(keyStroke(code)))}
              />
            ))}
          </Row>
          <Row>
            <Button
              secondary
              label="Scroll up"
              onPress={() =>
                void guarded(() => send([{ t: "wheel", dx: 0, dy: -240 }]))
              }
            />
            <Button
              secondary
              label="Scroll down"
              onPress={() =>
                void guarded(() => send([{ t: "wheel", dx: 0, dy: 240 }]))
              }
            />
          </Row>
          <Field
            label="Type on host computer"
            hint="US English keyboard. The host selects the destination field first."
          >
            <Input
              value={typing}
              onChangeText={setTyping}
              maxLength={500}
              placeholder="Text to type"
              autoCorrect={false}
              autoCapitalize="none"
            />
          </Field>
          <Button
            label="Send text"
            disabled={!typing}
            onPress={() =>
              void guarded(async () => {
                const events = textKeys(typing);
                for (let i = 0; i < events.length; i += 20) {
                  if (!consent.current.controlling)
                    throw new Error("Remote control stopped.");
                  await send(events.slice(i, i + 20));
                  await new Promise((resolve) => setTimeout(resolve, 250));
                }
                setTyping("");
              })
            }
          />
        </View>
      ) : null}
      {error ? <Notice tone="error">{error}</Notice> : null}
    </Card>
  );
}
