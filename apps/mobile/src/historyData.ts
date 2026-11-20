export type Call = {
  id: string;
  title: string;
  mode: string;
  status: string;
  started_at: string;
  outputs?: Record<string, string>;
  notes?: string;
};
export type Segment = {
  seg_id: number;
  channel: "me" | "them";
  text: string;
  start_ms: number;
};
export type Application = {
  application_id: string;
  company: string;
  job_title: string;
};
export type PreparationItem = {
  meeting: {
    account_id: string;
    calendar_id: string;
    instance_id: string;
    title: string;
    start: string;
    end: string;
  };
  candidates: Application[];
  selected_application_id: string | null;
  linked_session_id: string | null;
  requires_confirmation: boolean;
  overlaps: boolean;
  unfinished_actions: string[];
  previous: { notes: string; unfinished_actions: string[] }[];
};
export type PreparationData = {
  items: PreparationItem[];
  connections: {
    connection_id: string;
    provider: string;
    status: string;
    last_sync_at?: string | null;
    last_error?: string | null;
  }[];
  applications: Application[];
  saved_sessions: { id: string; title: string; started_at: string }[];
};
function object(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v))
    throw new Error(
      "The server returned an invalid response. Refresh to try again.",
    );
  return v as Record<string, unknown>;
}
function text(v: unknown): string {
  if (typeof v !== "string")
    throw new Error("The server returned invalid text. Refresh to try again.");
  return v;
}
function array(v: unknown): unknown[] {
  if (!Array.isArray(v))
    throw new Error(
      "The server returned an invalid list. Refresh to try again.",
    );
  return v;
}
function bool(v: unknown): boolean {
  if (typeof v !== "boolean")
    throw new Error("The server returned an invalid status.");
  return v;
}
function nullable(v: unknown): string | null {
  return v == null ? null : text(v);
}
function date(v: unknown): string {
  const s = text(v);
  if (!Number.isFinite(Date.parse(s)))
    throw new Error("The server returned an invalid date.");
  return s;
}
export function parseCall(value: unknown): Call {
  const v = object(value);
  const outputs: Record<string, string> = {};
  if (v.outputs != null)
    for (const [key, content] of Object.entries(object(v.outputs)))
      outputs[key] = text(content);
  return {
    id: text(v.id),
    title: v.title == null ? "" : text(v.title),
    mode: text(v.mode),
    status: text(v.status),
    started_at: date(v.started_at),
    outputs,
    notes: v.notes == null ? "" : text(v.notes),
  };
}
export function parseCalls(value: unknown) {
  const v = object(value);
  return {
    sessions: array(v.sessions).map(parseCall),
    next_before: nullable(v.next_before),
  };
}
export function parseTranscript(value: unknown): {
  stored: boolean;
  segments: Segment[];
} {
  const v = object(value);
  return {
    stored: bool(v.stored),
    segments: array(v.segments).map((value) => {
      const s = object(value);
      if (
        !["me", "them"].includes(String(s.channel)) ||
        !Number.isSafeInteger(s.seg_id) ||
        typeof s.start_ms !== "number" ||
        !Number.isFinite(s.start_ms) ||
        s.start_ms < 0
      )
        throw new Error("The saved transcript contains an invalid segment.");
      return {
        seg_id: s.seg_id as number,
        channel: s.channel as Segment["channel"],
        text: text(s.text),
        start_ms: s.start_ms,
      };
    }),
  };
}
function application(value: unknown): Application {
  const v = object(value);
  return {
    application_id: text(v.application_id),
    company: text(v.company),
    job_title: text(v.job_title),
  };
}
export function parsePreparation(value: unknown): PreparationData {
  const v = object(value);
  return {
    items: array(v.items).map((value) => {
      const i = object(value),
        m = object(i.meeting);
      return {
        meeting: {
          account_id: text(m.account_id),
          calendar_id: text(m.calendar_id),
          instance_id: text(m.instance_id),
          title: text(m.title),
          start: date(m.start),
          end: date(m.end),
        },
        candidates: array(i.candidates).map(application),
        selected_application_id: nullable(i.selected_application_id),
        linked_session_id: nullable(i.linked_session_id),
        requires_confirmation: bool(i.requires_confirmation),
        overlaps: bool(i.overlaps),
        unfinished_actions: array(i.unfinished_actions).map(text),
        previous: array(i.previous).map((value) => {
          const p = object(value);
          return {
            notes: text(p.notes),
            unfinished_actions: array(p.unfinished_actions).map(text),
          };
        }),
      };
    }),
    connections: array(v.connections).map((value) => {
      const c = object(value);
      return {
        connection_id: text(c.connection_id),
        provider: text(c.provider),
        status: text(c.status),
        last_sync_at: c.last_sync_at == null ? null : date(c.last_sync_at),
        last_error: nullable(c.last_error),
      };
    }),
    applications: array(v.applications).map(application),
    saved_sessions: array(v.saved_sessions).map((value) => {
      const s = object(value);
      return {
        id: text(s.id),
        title: text(s.title),
        started_at: date(s.started_at),
      };
    }),
  };
}
export function parseSources(
  value: unknown,
): { label: string; excerpt: string }[] {
  return array(object(value).sources).map((value) => {
    const s = object(value);
    return { label: text(s.label), excerpt: text(s.excerpt) };
  });
}
export function meetingKey(item: PreparationItem): string {
  return JSON.stringify([
    item.meeting.account_id,
    item.meeting.calendar_id,
    item.meeting.instance_id,
  ]);
}
export function meetingContext(
  item: PreparationItem,
  application: string,
  session: string,
  actions: string,
) {
  const unfinished_actions = [
    ...new Set(
      actions
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
    ),
  ];
  if (
    unfinished_actions.length > 50 ||
    unfinished_actions.some((s) => s.length > 2000)
  )
    throw new Error("Use up to 50 actions, each under 2,000 characters.");
  return {
    calendar_id: item.meeting.calendar_id,
    instance_id: item.meeting.instance_id,
    application_id: application || null,
    session_id: session || null,
    unfinished_actions,
  };
}
