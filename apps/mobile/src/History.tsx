import React, { useEffect, useRef, useState } from "react";
import { Alert, ScrollView } from "react-native";
import { Api } from "./api";
import { RequestScope } from "./requestScope";
import { Button, Card, Txt, page } from "./ui";
import type { ImportedQuestion } from "./Practice";
type Call = {
  id: string;
  title: string;
  mode: string;
  status: string;
  started_at: string;
  outputs?: Record<string, string>;
  notes?: string;
};
type Segment = {
  seg_id: number;
  channel: "me" | "them";
  text: string;
  start_ms: number;
};
export function History({
  api,
  practice,
}: {
  api: Api;
  practice: (q: ImportedQuestion) => void;
}) {
  const [calls, setCalls] = useState<Call[]>([]),
    [call, setCall] = useState<Call | null>(null),
    [segments, setSegments] = useState<Segment[]>([]),
    [error, setError] = useState(""),
    [before, setBefore] = useState<string | null>(null);
  const alive = useRef(true),
    selection = useRef(new RequestScope()),
    listing = useRef(new RequestScope());
  async function refresh(more = false) {
    try {
      const d = await listing.current.run((signal) =>
        api.request<{
          sessions: Call[];
          next_before: string | null;
        }>(
          `/sessions${more && before ? `?before=${encodeURIComponent(before)}` : ""}`,
          undefined,
          "GET",
          true,
          signal,
        ),
      );
      if (!d) return;
      setCalls((old) => (more ? [...old, ...d.sessions] : d.sessions));
      setBefore(d.next_before);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      selection.current.invalidate();
      listing.current.invalidate();
    };
  }, [api]);
  async function open(id: string) {
    try {
      setCall(null);
      setSegments([]);
      setError("");
      const result = await selection.current.run(async (signal) => {
        const detail = await api.request<Call>(
          `/sessions/${id}`,
          undefined,
          "GET",
          true,
          signal,
        );
        const transcript = await api.request<{
          stored: boolean;
          segments: Segment[];
        }>(`/sessions/${id}/transcript`, undefined, "GET", true, signal);
        return { detail, transcript };
      });
      if (!result) return;
      setCall(result.detail);
      setSegments(result.transcript.segments);
      setError(
        result.transcript.stored
          ? ""
          : "This call did not retain a transcript.",
      );
    } catch (e) {
      setError(String(e));
    }
  }
  async function remove(id: string) {
    selection.current.invalidate();
    listing.current.invalidate();
    setCall(null);
    setSegments([]);
    setCalls((old) => old.filter((c) => c.id !== id));
    try {
      await api.request(`/sessions/${id}`, undefined, "DELETE");
      if (alive.current) await refresh();
    } catch (e) {
      if (alive.current) {
        setError(String(e));
        await refresh();
      }
    }
  }
  return (
    <ScrollView contentContainerStyle={page}>
      <Txt title>Call history</Txt>
      {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
      {call ? (
        <>
          <Button
            label="All calls"
            secondary
            onPress={() => {
              selection.current.invalidate();
              setCall(null);
              setSegments([]);
            }}
          />
          <Txt title>{call.title || call.mode}</Txt>
          <Txt>
            {new Date(call.started_at).toLocaleString()} · {call.status}
          </Txt>
          {Object.entries(call.outputs ?? {}).map(([kind, text]) => (
            <Card key={kind}>
              <Txt title>{kind}</Txt>
              <Txt selectable>{text}</Txt>
            </Card>
          ))}
          {call.notes ? (
            <Card>
              <Txt title>Notes</Txt>
              <Txt selectable>{call.notes}</Txt>
            </Card>
          ) : null}
          <Txt title>Saved transcript</Txt>
          {segments.map((s) => (
            <Card key={`${s.channel}:${s.seg_id}`}>
              <Txt muted>
                {s.channel === "them" ? "Other speaker" : "You"} ·{" "}
                {Math.floor(s.start_ms / 60000)}:
                {String(Math.floor(s.start_ms / 1000) % 60).padStart(2, "0")}
              </Txt>
              <Txt selectable>{s.text}</Txt>
              <Button
                label="Practice this question"
                secondary
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
            onPress={() =>
              Alert.alert(
                "Delete this call?",
                "This removes its transcript and saved outputs. Previously approved memory remains.",
                [
                  { text: "Cancel" },
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
      ) : (
        <>
          <Button
            label="Refresh history"
            secondary
            onPress={() => void refresh()}
          />
          {calls.map((c) => (
            <Card key={c.id}>
              <Txt>{c.title || c.mode}</Txt>
              <Txt muted>
                {new Date(c.started_at).toLocaleString()} · {c.status}
              </Txt>
              <Button
                label="Read call"
                secondary
                onPress={() => void open(c.id)}
              />
            </Card>
          ))}
          {before ? (
            <Button
              label="Older calls"
              secondary
              onPress={() => void refresh(true)}
            />
          ) : null}
        </>
      )}
    </ScrollView>
  );
}
type Preparation = {
  meeting: { title: string; start: string; end: string; instance_id: string };
  candidates: {
    application_id: string;
    company: string;
    job_title: string;
    resume_id?: string;
  }[];
  selected_application_id?: string;
  requires_confirmation: boolean;
  overlaps: boolean;
  previous: { notes: string; unfinished_actions: string[] }[];
};
export function Preparation({ api }: { api: Api }) {
  const [items, setItems] = useState<Preparation[]>([]),
    [states, setStates] = useState<
      {
        provider: string;
        status: string;
        last_sync_at?: string;
        last_error?: string;
      }[]
    >([]),
    [error, setError] = useState(""),
    [detail, setDetail] = useState<{ label: string; excerpt: string }[] | null>(
      null,
    );
  async function refresh() {
    try {
      const d = await api.request<{
        items: Preparation[];
        connections: typeof states;
      }>("/meeting-preparation");
      setItems(d.items);
      setStates(d.connections);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    void refresh();
  }, [api]);
  async function preview(id: string) {
    try {
      const d = await api.request<{
        sources: { label: string; excerpt: string }[];
      }>(`/practice/context?application_id=${id}`);
      setDetail(d.sources);
    } catch (e) {
      setError(String(e));
    }
  }
  return (
    <ScrollView contentContainerStyle={page}>
      <Txt title>Meeting preparation</Txt>
      <Txt muted>
        Upcoming meetings use calendars connected in desktop Settings. Refresh
        to see provider changes.
      </Txt>
      <Button
        label="Refresh preparation"
        secondary
        onPress={() => void refresh()}
      />
      {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
      {states.map((s) => (
        <Txt key={s.provider}>
          {s.provider}: {s.status}
          {s.last_sync_at
            ? ` · ${new Date(s.last_sync_at).toLocaleString()}`
            : ""}
          {s.last_error ? ` · ${s.last_error}` : ""}
        </Txt>
      ))}
      {items.length === 0 ? (
        <Txt muted>
          No upcoming meeting in the preparation window. Calendar authorization
          and selection are available on desktop.
        </Txt>
      ) : null}
      {items.map((i) => (
        <Card key={i.meeting.instance_id}>
          <Txt title>{i.meeting.title}</Txt>
          <Txt>
            {new Date(i.meeting.start).toLocaleString()} –{" "}
            {new Date(i.meeting.end).toLocaleTimeString()}
          </Txt>
          {i.overlaps ? <Txt>Overlaps another meeting.</Txt> : null}
          {i.requires_confirmation ? (
            <Txt>
              Possible application matches; choose one to review. No context has
              been selected automatically.
            </Txt>
          ) : null}
          {i.candidates.map((a) => (
            <Button
              key={a.application_id}
              label={`Review ${a.company} · ${a.job_title}${a.resume_id ? " · resume paired" : ""}`}
              secondary
              onPress={() => void preview(a.application_id)}
            />
          ))}
          {i.previous.map((p, n) => (
            <Card key={n}>
              <Txt>Previous meeting notes</Txt>
              <Txt>{p.notes}</Txt>
              <Txt>Unfinished actions: {p.unfinished_actions.join("\n")}</Txt>
            </Card>
          ))}
        </Card>
      ))}
      {detail ? (
        <>
          <Button
            label="Close context"
            secondary
            onPress={() => setDetail(null)}
          />
          {detail.map((s, i) => (
            <Card key={i}>
              <Txt>{s.label}</Txt>
              <Txt selectable>{s.excerpt}</Txt>
            </Card>
          ))}
        </>
      ) : null}
    </ScrollView>
  );
}
