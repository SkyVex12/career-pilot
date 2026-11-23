import React, { useContext, useEffect, useRef, useState } from "react";
import { Alert, AppState, Linking, Switch, View } from "react-native";
import type { Api } from "./api";
import {
  useWorkspaceRequests,
  type WorkspaceRequest,
} from "./useWorkspaceRequests";
import { Badge, Button, Card, Loading, Notice, Row, Theme, Txt } from "./ui";
import {
  calendarAuthorizationUrl,
  calendarStatusLabel,
  disconnectedCalendar,
  parseCalendarConnections,
  parseCalendarDisconnect,
  syncCalendar,
  providerName,
  sameSelection,
  toggleCalendar,
  type CalendarChange,
  type CalendarConnection,
  type CalendarProvider,
} from "./calendarData";

const heading = { fontSize: 16, lineHeight: 23, fontWeight: "600" as const };
type PendingAuthorization = {
  provider: CalendarProvider;
  expiresAt: number;
  previousError: string | null;
};

/** Embedded in meeting preparation; account tokens remain on the API server. */
export function CalendarConnections({
  api,
  visible = true,
  onChanged,
  onNotice,
  onBusy,
}: {
  api: Api;
  visible?: boolean;
  onChanged: (change: CalendarChange) => void;
  onNotice: (message: string) => void;
  onBusy?: (busy: boolean) => void;
}) {
  const reads = useWorkspaceRequests(api),
    writes = useWorkspaceRequests(api),
    colors = useContext(Theme);
  const [connections, setConnections] = useState<CalendarConnection[] | null>(
    null,
  );
  const [drafts, setDrafts] = useState<
    Partial<Record<CalendarProvider, string[]>>
  >({});
  const [pending, setPending] = useState<PendingAuthorization | null>(null);
  const [activeOperation, setActiveOperation] = useState("");
  const [consentReminder, setConsentReminder] = useState<
    Partial<Record<CalendarProvider, boolean>>
  >({});
  const dirty = useRef(new Set<CalendarProvider>()),
    writing = useRef(false),
    alive = useRef(true),
    visibleRef = useRef(visible);
  const pendingRef = useRef(pending),
    changed = useRef(onChanged),
    notice = useRef(onNotice);
  const expiredAuthorization = useRef<CalendarProvider | null>(null);
  const lastStatuses = useRef(new Map<CalendarProvider, string>());
  visibleRef.current = visible;
  changed.current = onChanged;
  notice.current = onNotice;
  const error = writes.error || reads.error;
  const disabled = writes.busy || reads.loading;

  function authorization(value: PendingAuthorization | null) {
    pendingRef.current = value;
    setPending(value);
  }
  async function refresh() {
    if (writing.current || !visibleRef.current) return;
    const result = await reads.read(async (request) =>
      parseCalendarConnections(await request("/calendars")),
    );
    if (!result) return;
    setConnections(result);
    for (const connection of result) {
      const previous = lastStatuses.current.get(connection.provider);
      const current = `${connection.connected}:${connection.status}`;
      if (previous === "true:active" && current !== previous)
        changed.current({ provider: connection.provider, kind: "unavailable" });
      lastStatuses.current.set(connection.provider, current);
    }
    setDrafts((previous) =>
      Object.fromEntries(
        result.map((connection) => {
          const allowed = new Set(connection.calendars.map((c) => c.id));
          const saved = connection.selected_calendars.filter((id) =>
            allowed.has(id),
          );
          return [
            connection.provider,
            connection.connected && dirty.current.has(connection.provider)
              ? (previous[connection.provider] ?? saved).filter((id) =>
                  allowed.has(id),
                )
              : saved,
          ];
        }),
      ),
    );
    const waiting =
      pendingRef.current ??
      (expiredAuthorization.current
        ? {
            provider: expiredAuthorization.current,
            expiresAt: 0,
            previousError: null,
          }
        : null);
    if (waiting) {
      const connection = result.find((c) => c.provider === waiting.provider)!;
      if (connection.connected && connection.status === "active") {
        authorization(null);
        expiredAuthorization.current = null;
        writes.setError("");
        dirty.current.delete(connection.provider);
        setDrafts((previous) => ({
          ...previous,
          [connection.provider]: connection.selected_calendars,
        }));
        setConsentReminder((previous) => ({
          ...previous,
          [connection.provider]: false,
        }));
        notice.current(
          `${providerName(connection.provider)} connected. Choose calendars, then save and sync.`,
        );
        changed.current({ provider: connection.provider, kind: "connected" });
      } else if (
        connection.last_error &&
        connection.last_error !== waiting.previousError
      )
        authorization(null);
    }
  }
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      pendingRef.current = null;
    };
  }, [api]);
  useEffect(() => {
    if (visible && reads.foreground) void refresh();
    else reads.cancelRead();
    return () => reads.cancelRead();
  }, [api, visible, reads.foreground]);
  useEffect(() => {
    onBusy?.(writes.busy);
    return () => onBusy?.(false);
  }, [writes.busy, onBusy]);
  useEffect(() => {
    if (!pending) return;
    const timer = setTimeout(
      () => {
        expiredAuthorization.current = pending.provider;
        authorization(null);
        writes.setError(
          "Calendar sign-in expired. Connect again to finish authorization.",
        );
      },
      Math.max(0, pending.expiresAt - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [pending]);

  async function mutate<T>(
    name: string,
    operation: (request: WorkspaceRequest, signal: AbortSignal) => Promise<T>,
  ) {
    if (writing.current) return undefined;
    writing.current = true;
    setActiveOperation(name);
    reads.cancelRead();
    writes.setError("");
    try {
      return await writes.mutate(operation);
    } finally {
      writing.current = false;
      if (alive.current) setActiveOperation("");
    }
  }
  async function connect(connection: CalendarConnection) {
    if (!connection.available) return;
    await mutate(`${connection.provider}:connect`, async (request, signal) => {
      const url = calendarAuthorizationUrl(
        connection.provider,
        await request(`/calendars/${connection.provider}/connect`, {}, "POST"),
      );
      signal.throwIfAborted();
      if (
        !visibleRef.current ||
        api.profile !== writes.profile ||
        ["background", "inactive"].includes(AppState.currentState ?? "active")
      )
        throw new Error(
          "Return to calendar settings and connect again to open sign-in.",
        );
      expiredAuthorization.current = null;
      dirty.current.delete(connection.provider);
      authorization({
        provider: connection.provider,
        expiresAt: Date.now() + 600_000,
        previousError: connection.last_error,
      });
      try {
        await Linking.openURL(url);
      } catch {
        authorization(null);
        throw new Error(
          "Could not open the calendar sign-in page. Check your browser and try again.",
        );
      }
      return true;
    });
    if (alive.current && reads.foreground && visibleRef.current) void refresh();
  }
  function toggle(connection: CalendarConnection, id: string) {
    try {
      const next = toggleCalendar(
        connection,
        drafts[connection.provider] ?? connection.selected_calendars,
        id,
      );
      dirty.current.add(connection.provider);
      writes.setError("");
      setDrafts((previous) => ({ ...previous, [connection.provider]: next }));
    } catch (error) {
      writes.setError(error instanceof Error ? error.message : String(error));
    }
  }
  async function sync(connection: CalendarConnection, saveSelection: boolean) {
    const result = await mutate(
      `${connection.provider}:${saveSelection ? "save" : "sync"}`,
      (request, signal) =>
        syncCalendar({
          request,
          connection,
          signal,
          selected: saveSelection
            ? (drafts[connection.provider] ?? connection.selected_calendars)
            : undefined,
          onSelectionSaved: (selected) => {
            dirty.current.delete(connection.provider);
            setConnections(
              (previous) =>
                previous?.map((c) =>
                  c.provider === connection.provider
                    ? { ...c, selected_calendars: selected, last_sync_at: null }
                    : c,
                ) ?? null,
            );
            setDrafts((previous) => ({
              ...previous,
              [connection.provider]: selected,
            }));
            changed.current({
              provider: connection.provider,
              kind: "selection",
            });
          },
        }),
    );
    if (!alive.current) return;
    if (result) {
      notice.current(
        `${providerName(connection.provider)} synced. ${result.value.event_count} event${result.value.event_count === 1 ? "" : "s"} available for preparation.`,
      );
      changed.current({ provider: connection.provider, kind: "sync" });
    }
    void refresh();
  }
  function disconnect(connection: CalendarConnection) {
    Alert.alert(
      `Disconnect ${providerName(connection.provider)}?`,
      "This removes cached meetings and calendar access from this profile. Saved calls and their notes remain.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Disconnect",
          style: "destructive",
          onPress: () => {
            void (async () => {
              const result = await mutate(
                `${connection.provider}:disconnect`,
                async (request) =>
                  parseCalendarDisconnect(
                    await request(
                      `/calendars/${connection.provider}`,
                      undefined,
                      "DELETE",
                    ),
                  ),
              );
              if (!result) {
                if (alive.current) void refresh();
                return;
              }
              if (pendingRef.current?.provider === connection.provider)
                authorization(null);
              if (expiredAuthorization.current === connection.provider)
                expiredAuthorization.current = null;
              dirty.current.delete(connection.provider);
              setDrafts((previous) => ({
                ...previous,
                [connection.provider]: [],
              }));
              setConnections(
                (previous) =>
                  previous?.map((c) =>
                    c.provider === connection.provider
                      ? disconnectedCalendar(c)
                      : c,
                  ) ?? null,
              );
              setConsentReminder((previous) => ({
                ...previous,
                [connection.provider]:
                  result.value.provider_consent_removal_required,
              }));
              changed.current({
                provider: connection.provider,
                kind: "disconnected",
              });
              notice.current(
                `${providerName(connection.provider)} disconnected.`,
              );
              void refresh();
            })();
          },
        },
      ],
    );
  }

  return (
    <View style={{ gap: 12 }}>
      <Txt muted>
        Calendar access is read-only. Select up to 10 calendars per provider for
        meeting preparation.
      </Txt>
      {error ? (
        <Notice
          tone="error"
          action={
            <Button
              label="Refresh status"
              secondary
              compact
              disabled={disabled}
              onPress={() => {
                writes.setError("");
                void refresh();
              }}
            />
          }
        >
          {error}
        </Notice>
      ) : null}
      {reads.loading && !connections ? (
        <Loading label="Loading calendar connections" />
      ) : null}
      {connections?.map((connection) => {
        const selected =
          drafts[connection.provider] ?? connection.selected_calendars;
        const modified = !sameSelection(
          selected,
          connection.selected_calendars,
        );
        const waiting = pending?.provider === connection.provider;
        const reconnect =
          !connection.connected || connection.status === "reauth_required";
        return (
          <Card key={connection.provider} subtle>
            <Row style={{ justifyContent: "space-between" }}>
              <Txt style={heading}>{providerName(connection.provider)}</Txt>
              <Badge
                label={
                  waiting ? "Sign-in pending" : calendarStatusLabel(connection)
                }
                tone={
                  connection.status === "reauth_required" ||
                  connection.status === "error"
                    ? "warning"
                    : connection.connected
                      ? "success"
                      : "neutral"
                }
              />
            </Row>
            {!connection.available ? (
              <Txt muted>
                This server has not enabled {providerName(connection.provider)}{" "}
                yet. You can still choose call context manually.
              </Txt>
            ) : reconnect ? (
              <>
                <Txt muted>
                  {waiting
                    ? "Finish sign-in in your browser, then return here. Status refreshes when this app becomes active."
                    : "Sign in with your calendar account, then choose which calendars to use."}
                </Txt>
                <Button
                  label={
                    waiting
                      ? "Open sign-in again"
                      : connection.status === "reauth_required"
                        ? "Reconnect calendar"
                        : "Connect calendar"
                  }
                  secondary
                  disabled={disabled}
                  loading={activeOperation === `${connection.provider}:connect`}
                  onPress={() => void connect(connection)}
                />
              </>
            ) : (
              <>
                <Txt muted>
                  {connection.last_sync_at
                    ? `Last synced ${new Date(connection.last_sync_at).toLocaleString()}`
                    : "Choose calendars, then save and sync to load upcoming meetings."}
                </Txt>
                {!connection.calendars.length ? (
                  <Txt muted>
                    No calendars were returned. Try syncing to refresh the
                    available calendars.
                  </Txt>
                ) : (
                  connection.calendars.map((calendar) => (
                    <Row
                      key={calendar.id}
                      style={{ justifyContent: "space-between" }}
                    >
                      <Txt style={{ flex: 1 }}>{calendar.name}</Txt>
                      <Switch
                        accessibilityLabel={`Use ${calendar.name} for meeting preparation`}
                        value={selected.includes(calendar.id)}
                        disabled={
                          disabled ||
                          (!selected.includes(calendar.id) &&
                            selected.length >= 10)
                        }
                        onValueChange={() => toggle(connection, calendar.id)}
                        trackColor={{ false: colors.line, true: colors.ruby }}
                        thumbColor={colors.card}
                      />
                    </Row>
                  ))
                )}
                <Txt muted>
                  {selected.length} of 10 calendars selected
                  {modified ? " · Changes not saved" : ""}
                </Txt>
                <Row>
                  <Button
                    label="Save and sync"
                    loading={activeOperation === `${connection.provider}:save`}
                    disabled={disabled || !modified}
                    onPress={() => void sync(connection, true)}
                  />
                  <Button
                    label="Sync calendars"
                    loading={activeOperation === `${connection.provider}:sync`}
                    secondary
                    disabled={disabled || modified}
                    onPress={() => void sync(connection, false)}
                  />
                </Row>
                <Txt muted>
                  Calendar sync is available once every five minutes. Refreshing
                  connection status does not sync events.
                </Txt>
              </>
            )}
            {connection.last_error ? (
              <Notice tone="error">{connection.last_error}</Notice>
            ) : null}
            {consentReminder[connection.provider] ? (
              <Notice>
                To remove provider authorization too, remove CoRuby from your
                calendar account's connected apps.
              </Notice>
            ) : null}
            {connection.connected ||
            connection.status !== "disconnected" ||
            waiting ? (
              <Button
                label={
                  connection.connected
                    ? "Disconnect calendar"
                    : "Cancel calendar connection"
                }
                secondary
                compact
                danger
                disabled={disabled}
                loading={
                  activeOperation === `${connection.provider}:disconnect`
                }
                onPress={() => disconnect(connection)}
              />
            ) : null}
          </Card>
        );
      })}
      <Button
        label="Refresh connection status"
        icon="refresh"
        secondary
        compact
        disabled={disabled}
        onPress={() => {
          writes.setError("");
          void refresh();
        }}
      />
    </View>
  );
}
