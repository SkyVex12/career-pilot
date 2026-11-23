import React, { useEffect, useRef, useState } from "react";
import { Alert, View } from "react-native";
import { Api } from "./api";
import { RequestScope } from "./requestScope";
import {
  ActionCard,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  IconButton,
  Input,
  Loading,
  Notice,
  Row,
  Screen,
  ScreenTitle,
  Select,
  Txt,
} from "./ui";
import {
  meetingContext,
  meetingKey,
  parseCall,
  parseCalls,
  parsePreparation,
  parseSources,
  parseTranscript,
  type Call,
  type PreparationData,
  type PreparationItem,
  type Segment,
} from "./historyData";
import type { ImportedQuestion } from "./Practice";
import { CalendarConnections } from "./CalendarConnections";
import { clearCalendarPreparation, type CalendarChange } from "./calendarData";
import { FloatingNotice } from "./FloatingNotice";
const when = (date: string) =>
  new Date(date).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
const heading = { fontSize: 17, lineHeight: 24, fontWeight: "600" as const };

export function History({
  api,
  practice,
  onAsk,
}: {
  api: Api;
  practice: (q: ImportedQuestion) => void;
  onAsk?: (sessionId: string) => void;
}) {
  const [calls, setCalls] = useState<Call[]>([]),
    [call, setCall] = useState<Call | null>(null),
    [selected, setSelected] = useState<string | null>(null),
    [segments, setSegments] = useState<Segment[]>([]),
    [stored, setStored] = useState(true),
    [error, setError] = useState(""),
    [before, setBefore] = useState<string | null>(null),
    [loading, setLoading] = useState(false),
    [opening, setOpening] = useState(false),
    [deleting, setDeleting] = useState(false),
    [query, setQuery] = useState("");
  const listing = useRef(new RequestScope()),
    selection = useRef(new RequestScope()),
    mutation = useRef(new RequestScope()),
    revision = useRef(0),
    detailRevision = useRef(0),
    alive = useRef(true);
  async function refresh(more = false) {
    const ticket = ++revision.current;
    setLoading(true);
    setError("");
    try {
      const d = await listing.current.run(async (signal) =>
        parseCalls(
          await api.request(
            `/sessions${more && before ? `?before=${encodeURIComponent(before)}` : ""}`,
            undefined,
            "GET",
            true,
            signal,
          ),
        ),
      );
      if (!d) return;
      setCalls((old) =>
        more
          ? [...new Map([...old, ...d.sessions].map((c) => [c.id, c])).values()]
          : d.sessions,
      );
      setBefore(d.next_before);
    } catch (e) {
      setError(String(e));
    } finally {
      if (ticket === revision.current) setLoading(false);
    }
  }
  function back() {
    detailRevision.current++;
    selection.current.invalidate();
    setSelected(null);
    setCall(null);
    setSegments([]);
    setOpening(false);
    setError("");
  }
  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      revision.current++;
      detailRevision.current++;
      listing.current.invalidate();
      selection.current.invalidate();
      mutation.current.invalidate();
    };
  }, [api]);
  async function open(id: string) {
    const ticket = ++detailRevision.current;
    setSelected(id);
    setCall(null);
    setSegments([]);
    setError("");
    setOpening(true);
    try {
      const result = await selection.current.run(async (signal) => {
        const [detail, transcript] = await Promise.all([
          api.request(
            `/sessions/${encodeURIComponent(id)}`,
            undefined,
            "GET",
            true,
            signal,
          ),
          api.request(
            `/sessions/${encodeURIComponent(id)}/transcript`,
            undefined,
            "GET",
            true,
            signal,
          ),
        ]);
        const parsed = parseCall(detail);
        if (parsed.id !== id)
          throw new Error(
            "The server returned a different saved call. Please retry.",
          );
        return { detail: parsed, transcript: parseTranscript(transcript) };
      });
      if (!result) return;
      setCall(result.detail);
      setSegments(result.transcript.segments);
      setStored(result.transcript.stored);
    } catch (e) {
      setError(String(e));
    } finally {
      if (ticket === detailRevision.current) setOpening(false);
    }
  }
  async function remove(id: string) {
    setDeleting(true);
    setError("");
    try {
      const done = await mutation.current.run(async (signal) => {
        await api.request(
          `/sessions/${encodeURIComponent(id)}`,
          undefined,
          "DELETE",
          true,
          signal,
        );
        return true;
      });
      if (!done) return;
      back();
      setCalls((old) => old.filter((c) => c.id !== id));
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      if (alive.current) setDeleting(false);
    }
  }
  const filtered = calls.filter((c) =>
    `${c.title} ${c.mode} ${c.status}`
      .toLocaleLowerCase()
      .includes(query.toLocaleLowerCase()),
  );
  return (
    <Screen key={call?.id ?? "calls"}>
      {selected ? (
        <Button
          label="Back to history"
          secondary
          compact
          disabled={deleting}
          onPress={back}
        />
      ) : null}
      <ScreenTitle
        eyebrow="Your conversations"
        title={selected ? call?.title || "Saved call" : "History"}
        subtitle={
          selected
            ? call
              ? `${when(call.started_at)} · ${call.mode}`
              : "Loading your saved conversation"
            : "Revisit notes, answers, and moments worth practicing."
        }
        action={
          !selected ? (
            <IconButton
              label="Refresh history"
              icon="refresh"
              disabled={loading}
              onPress={() => void refresh()}
            />
          ) : undefined
        }
      />
      {error ? (
        <Notice
          tone="error"
          action={
            <Button
              label="Try again"
              secondary
              compact
              onPress={() => (selected ? void open(selected) : void refresh())}
            />
          }
        >
          {error}
        </Notice>
      ) : null}
      {selected ? (
        <>
          {opening ? <Loading label="Opening saved call..." /> : null}
          {call ? (
            <>
              <Badge
                label={call.status}
                tone={call.status === "ready" ? "success" : "neutral"}
              />
              {onAsk ? (
                <Button
                  label="Ask about this call"
                  icon="work"
                  secondary
                  disabled={deleting}
                  onPress={() => onAsk(call.id)}
                />
              ) : null}
              {Object.entries(call.outputs ?? {}).map(([kind, text]) => (
                <Card key={kind}>
                  <Txt style={heading}>{kind.replace(/_/g, " ")}</Txt>
                  <Txt selectable>{text}</Txt>
                </Card>
              ))}
              {call.notes ? (
                <Card>
                  <Txt style={heading}>Your notes</Txt>
                  <Txt selectable>{call.notes}</Txt>
                </Card>
              ) : null}
              <Row>
                <Txt style={heading}>Saved transcript</Txt>
                <Badge label={`${segments.length} segments`} />
              </Row>
              {!stored ? (
                <Notice>This call did not retain a transcript.</Notice>
              ) : !segments.length ? (
                <Notice>
                  No transcript segments were saved for this call.
                </Notice>
              ) : null}
              {segments.map((s) => (
                <Card key={`${s.channel}:${s.seg_id}`}>
                  <Row>
                    <Badge
                      label={s.channel === "them" ? "Other speaker" : "You"}
                    />
                    <Txt muted>
                      {Math.floor(s.start_ms / 60000)}:
                      {String(Math.floor(s.start_ms / 1000) % 60).padStart(
                        2,
                        "0",
                      )}
                    </Txt>
                  </Row>
                  <Txt selectable>{s.text}</Txt>
                  <Button
                    label="Practice this question"
                    icon="practice"
                    secondary
                    compact
                    onPress={() =>
                      practice({
                        session_id: call.id,
                        channel: s.channel,
                        seg_id: s.seg_id,
                      })
                    }
                  />
                </Card>
              ))}
              <Button
                label="Delete saved call"
                secondary
                danger
                loading={deleting}
                onPress={() =>
                  Alert.alert(
                    "Delete this call?",
                    "This removes its transcript and saved outputs. Previously approved memory remains.",
                    [
                      { text: "Cancel", style: "cancel" },
                      {
                        text: "Delete",
                        style: "destructive",
                        onPress: () => void remove(call.id),
                      },
                    ],
                  )
                }
              />
            </>
          ) : null}
        </>
      ) : (
        <>
          {calls.length ? (
            <Input
              placeholder="Search loaded calls"
              value={query}
              onChangeText={setQuery}
              autoCorrect={false}
            />
          ) : null}
          {loading && !calls.length ? (
            <Loading label="Loading your calls..." />
          ) : null}
          {!loading && !error && !filtered.length ? (
            <EmptyState
              icon="history"
              title={query ? "No matching calls" : "Your history starts here"}
              description={
                query
                  ? "Try a different title or load older calls."
                  : "Saved conversations and their notes will appear after your first call."
              }
            />
          ) : null}
          {filtered.map((c) => (
            <ActionCard
              key={c.id}
              label={`Read ${c.title || "untitled call"}`}
              onPress={() => void open(c.id)}
            >
              <Row style={{ justifyContent: "space-between" }}>
                <Badge label={c.mode} />
                <Badge
                  label={c.status}
                  tone={c.status === "ready" ? "success" : "neutral"}
                />
              </Row>
              <Txt style={heading}>{c.title || "Untitled call"}</Txt>
              <Txt muted style={{ fontSize: 13 }}>
                {when(c.started_at)}
              </Txt>
            </ActionCard>
          ))}
          {before ? (
            <Button
              label="Load older calls"
              secondary
              loading={loading}
              onPress={() => void refresh(true)}
            />
          ) : null}
        </>
      )}
    </Screen>
  );
}

export function Preparation({
  api,
  visible = true,
}: {
  api: Api;
  visible?: boolean;
}) {
  const [data, setData] = useState<PreparationData | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [selected, setSelected] = useState<string | null>(null),
    [version, setVersion] = useState(0),
    [saved, setSaved] = useState(false),
    [editorState, setEditorState] = useState({ dirty: false, saving: false });
  const [calendarOpen, setCalendarOpen] = useState(false),
    [calendarVisited, setCalendarVisited] = useState(false),
    [calendarBusy, setCalendarBusy] = useState(false),
    [calendarNotice, setCalendarNotice] = useState("");
  const listing = useRef(new RequestScope()),
    revision = useRef(0),
    preparationAlive = useRef(true),
    visibleRef = useRef(visible),
    reloadPending = useRef(true);
  visibleRef.current = visible;
  async function refresh() {
    reloadPending.current = true;
    if (!visibleRef.current) return;
    const ticket = ++revision.current;
    setLoading(true);
    setError("");
    try {
      const next = await listing.current.run(async (signal) =>
        parsePreparation(
          await api.request(
            "/meeting-preparation",
            undefined,
            "GET",
            true,
            signal,
          ),
        ),
      );
      if (!next) return;
      reloadPending.current = false;
      setData(next);
      setVersion((v) => v + 1);
    } catch (e) {
      setError(String(e));
    } finally {
      if (revision.current === ticket) setLoading(false);
    }
  }
  useEffect(() => {
    preparationAlive.current = true;
    return () => {
      preparationAlive.current = false;
      revision.current++;
      listing.current.invalidate();
    };
  }, [api]);
  useEffect(() => {
    if (visible) {
      if (reloadPending.current) void refresh();
    } else {
      revision.current++;
      listing.current.invalidate();
      setLoading(false);
    }
  }, [api, visible]);
  useEffect(() => {
    if (editorState.dirty) setSaved(false);
  }, [editorState.dirty]);
  function backToMeetings() {
    const leave = () => {
      setSelected(null);
      setSaved(false);
      setEditorState({ dirty: false, saving: false });
    };
    if (!editorState.dirty) return leave();
    Alert.alert(
      "Discard meeting changes?",
      "Your application, saved call, and action edits have not been saved.",
      [
        { text: "Keep editing", style: "cancel" },
        { text: "Discard changes", style: "destructive", onPress: leave },
      ],
    );
  }
  function calendarChanged(change: CalendarChange) {
    // Clear old provider events immediately after selection, reauthorization or
    // disconnect, even if the subsequent preparation read cannot reach the API.
    if (change.kind !== "sync") {
      revision.current++;
      listing.current.invalidate();
      setData((previous) => {
        if (!previous) return previous;
        return clearCalendarPreparation(
          previous,
          change.provider,
          change.kind === "disconnected",
        );
      });
    }
    reloadPending.current = true;
    if (!selected) void refresh();
  }
  const item = data?.items.find((i) => meetingKey(i) === selected);
  return (
    <View style={{ flex: 1 }}>
      <Screen>
        {selected ? (
          <Button
            label="Back to meetings"
            secondary
            compact
            disabled={editorState.saving}
            onPress={backToMeetings}
          />
        ) : null}
        <ScreenTitle
          eyebrow="Before your next call"
          title={item ? item.meeting.title : "Meeting preparation"}
          subtitle={
            item
              ? `${when(item.meeting.start)} – ${new Date(item.meeting.end).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`
              : "Review the right context before the conversation begins."
          }
          action={
            !selected ? (
              <IconButton
                label="Refresh meetings"
                icon="refresh"
                disabled={loading}
                onPress={() => void refresh()}
              />
            ) : undefined
          }
        />
        <View
          style={{ display: selected ? "none" : "flex", gap: 12 }}
          accessibilityElementsHidden={!!selected}
          importantForAccessibility={selected ? "no-hide-descendants" : "auto"}
        >
          <Row style={{ justifyContent: "space-between" }}>
            <Txt muted>
              {data?.connections.length
                ? `${data.connections.length} calendar connection${data.connections.length === 1 ? "" : "s"}`
                : "Optional read-only calendars"}
            </Txt>
            <Button
              label={calendarOpen ? "Hide calendars" : "Manage calendars"}
              icon="calendar"
              secondary
              compact
              disabled={calendarBusy}
              onPress={() => {
                setCalendarVisited(true);
                setCalendarOpen((value) => !value);
              }}
            />
          </Row>
          {calendarVisited ? (
            <View
              style={{ display: calendarOpen ? "flex" : "none" }}
              accessibilityElementsHidden={!calendarOpen}
              importantForAccessibility={
                calendarOpen ? "auto" : "no-hide-descendants"
              }
            >
              <CalendarConnections
                api={api}
                visible={visible && !selected && calendarOpen}
                onChanged={calendarChanged}
                onNotice={setCalendarNotice}
                onBusy={setCalendarBusy}
              />
            </View>
          ) : null}
        </View>
        {error ? (
          <Notice
            tone="error"
            action={
              <Button
                label="Try again"
                secondary
                onPress={() => void refresh()}
              />
            }
          >
            {error}
          </Notice>
        ) : null}
        {loading ? <Loading label="Refreshing preparation..." /> : null}
        {saved ? <Notice tone="success">Meeting context saved.</Notice> : null}
        {item && data ? (
          <MeetingEditor
            key={`${selected}:${version}`}
            api={api}
            item={item}
            data={data}
            visible={visible}
            onStateChange={setEditorState}
            onSaved={async () => {
              await refresh();
              if (preparationAlive.current) setSaved(true);
            }}
          />
        ) : selected && !loading ? (
          <Notice>
            This meeting is no longer in the preparation window. Return to the
            meeting list.
          </Notice>
        ) : (
          <>
            {!calendarOpen &&
              data?.connections.map((c) => (
                <Card key={c.connection_id} subtle>
                  <Row>
                    <Txt style={{ fontWeight: "600", flex: 1 }}>
                      {c.provider === "google"
                        ? "Google Calendar"
                        : c.provider === "outlook"
                          ? "Outlook"
                          : c.provider}
                    </Txt>
                    <Badge
                      label={c.status}
                      tone={c.status === "active" ? "success" : "warning"}
                    />
                  </Row>
                  {c.last_sync_at ? (
                    <Txt muted>Last synced {when(c.last_sync_at)}</Txt>
                  ) : null}
                  {c.last_error ? (
                    <Notice tone="error">{c.last_error}</Notice>
                  ) : null}
                </Card>
              ))}
            {data && !data.items.length && !error && !loading ? (
              <EmptyState
                icon="calendar"
                title="Nothing to prepare just yet"
                description="Upcoming meetings from your selected calendars appear here. Use Manage calendars to connect, select and sync them."
              />
            ) : null}
            {data?.items.map((i) => (
              <ActionCard
                key={meetingKey(i)}
                label={`Prepare for ${i.meeting.title}`}
                onPress={() => {
                  if (!calendarBusy) setSelected(meetingKey(i));
                }}
              >
                <Row>
                  <Badge label={when(i.meeting.start)} tone="ruby" />
                  {i.overlaps ? (
                    <Badge label="Overlapping meeting" tone="warning" />
                  ) : null}
                </Row>
                <Txt style={heading}>{i.meeting.title}</Txt>
                <Txt muted>
                  {i.requires_confirmation
                    ? "Choose the application and saved call to use."
                    : "Review your context, notes, and open actions."}
                </Txt>
              </ActionCard>
            ))}
          </>
        )}
      </Screen>
      <FloatingNotice
        message={calendarNotice}
        onDismiss={() => setCalendarNotice("")}
      />
    </View>
  );
}

function MeetingEditor({
  api,
  item,
  data,
  visible,
  onStateChange,
  onSaved,
}: {
  api: Api;
  item: PreparationItem;
  data: PreparationData;
  visible: boolean;
  onStateChange: (state: { dirty: boolean; saving: boolean }) => void;
  onSaved: () => Promise<void>;
}) {
  const [application, setApplication] = useState(
      item.selected_application_id ?? "",
    ),
    [session, setSession] = useState(item.linked_session_id ?? ""),
    [actions, setActions] = useState(item.unfinished_actions.join("\n")),
    [sources, setSources] = useState<
      { label: string; excerpt: string }[] | null
    >(null),
    [error, setError] = useState(""),
    [saving, setSaving] = useState(false),
    [previewing, setPreviewing] = useState(false),
    [saved, setSaved] = useState(false);
  const previewScope = useRef(new RequestScope()),
    saveScope = useRef(new RequestScope()),
    revision = useRef(0),
    alive = useRef(true),
    baseline = useRef({ application, session, actions }),
    savingRef = useRef(false);
  const dirty =
    application !== baseline.current.application ||
    session !== baseline.current.session ||
    actions !== baseline.current.actions;
  useEffect(() => {
    onStateChange({ dirty, saving });
  }, [dirty, saving, onStateChange]);
  useEffect(() => {
    if (!visible) {
      // Read-only review stops offscreen. An explicit save may finish so its
      // acknowledgement is retained; Preparation defers its refresh until visible.
      revision.current++;
      previewScope.current.invalidate();
      setPreviewing(false);
    }
  }, [visible]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      revision.current++;
      previewScope.current.invalidate();
      saveScope.current.invalidate();
    };
  }, [api]);
  function closeSources() {
    revision.current++;
    previewScope.current.invalidate();
    setSources(null);
    setPreviewing(false);
  }
  const provider = data.connections.find(
    (c) => c.connection_id === item.meeting.account_id,
  )?.provider;
  async function preview() {
    if (!visible) return;
    const ticket = ++revision.current;
    setSources(null);
    setPreviewing(true);
    setError("");
    try {
      const result = await previewScope.current.run(async (signal) =>
        parseSources(
          await api.request(
            `/practice/context?application_id=${encodeURIComponent(application)}`,
            undefined,
            "GET",
            true,
            signal,
          ),
        ),
      );
      if (result) setSources(result);
    } catch (e) {
      setError(String(e));
    } finally {
      if (ticket === revision.current) setPreviewing(false);
    }
  }
  async function save() {
    if (!provider || savingRef.current || !visible) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const body = meetingContext(item, application, session, actions);
      const done = await saveScope.current.run(async (signal) => {
        const result = await api.request<{ saved: boolean }>(
          `/calendars/${encodeURIComponent(provider)}/context`,
          body,
          "PUT",
          true,
          signal,
        );
        if (!result || result.saved !== true)
          throw new Error(
            "The server did not confirm that the meeting context was saved. Refresh before retrying.",
          );
        return true;
      });
      if (!done) return;
      baseline.current = { application, session, actions };
      setSaved(true);
      onStateChange({ dirty: false, saving: true });
      await onSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      savingRef.current = false;
      if (alive.current) setSaving(false);
    }
  }
  return (
    <>
      {item.overlaps ? (
        <Notice>This meeting overlaps another calendar event.</Notice>
      ) : null}
      {error ? <Notice tone="error">{error}</Notice> : null}
      <Card>
        <Txt style={heading}>Meeting context</Txt>
        <Txt muted>
          Choose what belongs to this meeting. Saving context does not start a
          call.
        </Txt>
        <Select
          label="Application"
          value={application}
          disabled={saving}
          options={[
            { value: "", label: "No application" },
            ...data.applications.map((a) => ({
              value: a.application_id,
              label: `${a.company} · ${a.job_title}`,
              description: item.candidates.some(
                (c) => c.application_id === a.application_id,
              )
                ? "Possible meeting match"
                : undefined,
            })),
          ]}
          onChange={(value) => {
            closeSources();
            setApplication(value);
            setSaved(false);
          }}
        />
        {application ? (
          <Button
            label="Review application context"
            secondary
            icon="file"
            loading={previewing}
            disabled={saving}
            onPress={() => void preview()}
          />
        ) : null}
        {previewing ? (
          <Button
            label="Cancel context review"
            secondary
            compact
            onPress={closeSources}
          />
        ) : null}
        {sources ? (
          <View style={{ gap: 12 }} accessibilityLiveRegion="polite">
            <Row style={{ justifyContent: "space-between" }}>
              <Txt style={heading}>Application context</Txt>
              <IconButton
                label="Close context"
                icon="close"
                onPress={closeSources}
              />
            </Row>
            {!sources.length ? (
              <Notice>
                No source context is available for this application.
              </Notice>
            ) : (
              sources.map((source, index) => (
                <Card key={index}>
                  <Txt style={heading}>{source.label}</Txt>
                  <Txt selectable>{source.excerpt}</Txt>
                </Card>
              ))
            )}
          </View>
        ) : null}
        <Select
          label="Linked saved call"
          value={session}
          disabled={saving}
          options={[
            { value: "", label: "No saved call" },
            ...data.saved_sessions.map((s) => ({
              value: s.id,
              label: s.title,
              description: when(s.started_at),
            })),
          ]}
          onChange={(value) => {
            setSession(value);
            setSaved(false);
          }}
        />
        <Field
          label="Unfinished actions"
          hint="One action per line. Remove completed actions before saving."
        >
          <Input
            multiline
            numberOfLines={4}
            value={actions}
            editable={!saving}
            placeholder="Add the next steps to carry forward"
            onChangeText={(value) => {
              setActions(value);
              setSaved(false);
            }}
          />
        </Field>
        {!provider ? (
          <Notice>
            Return to the meeting list and reconnect this calendar under Manage
            calendars before changing context.
          </Notice>
        ) : (
          <Button
            label="Save meeting context"
            icon="check"
            loading={saving}
            onPress={() => void save()}
          />
        )}
        {saved ? <Notice tone="success">Meeting context saved.</Notice> : null}
      </Card>
      {item.previous.some((p) => p.notes) ? (
        <View style={{ gap: 12 }}>
          <Txt style={heading}>Notes from linked calls</Txt>
          {item.previous
            .filter((p) => p.notes)
            .map((previous, index) => (
              <Card key={index}>
                <Txt selectable>{previous.notes}</Txt>
              </Card>
            ))}
        </View>
      ) : (
        <Notice>
          Linked call notes appear here when available for this meeting series.
        </Notice>
      )}
    </>
  );
}
