// Fictional data for the design harness: no real people, companies or accounts.
import type {
  ApplicationDetail,
  ApplicationItem,
  CareerOsItem,
  CompanyDocument,
  Employer,
  InterviewItem,
  MemoryItem,
  Proposal,
  Replay,
  ResumeVersion,
  SessionDetail,
  SessionItem,
  WorkLog,
} from "../../../apps/desktop/src/api";

const now = Date.now();
const iso = (offsetMin: number) => new Date(now + offsetMin * 60_000).toISOString();
const day = (offsetDays: number) => {
  const d = new Date(now + offsetDays * 86_400_000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const pack = (n = 14) => ({ pack_id: `p-${n}`, available: true, prepared_answers: n, built_at: iso(-60) });

const LONG = "Senior Staff Software Engineer, Platform Reliability and Developer Productivity (Remote, Americas)";

export function applications(long: boolean): ApplicationItem[] {
  const base: ApplicationItem[] = [
    app("a1", "Northwind Freight", "Senior Backend Engineer", "interview", "tailored", "Northwind backend v2", pack(18)),
    app("a2", "Lumen Health", "Staff Engineer, Payments", "applied", "base", "Base resume", null),
    app("a3", "Harbor Analytics", "Backend Engineer", "applied", "base", "Base resume", { pack_id: "x", available: false }),
    app("a4", "Quarry Labs", "Platform Engineer", "rejected", "tailored", "Quarry platform", pack(9)),
  ];
  if (!long) return base;
  const more = Array.from({ length: 36 }, (_, i) =>
    app(`l${i}`, i % 3 ? `Company ${i} Holdings International Group` : "Bluewater Logistics and Supply Chain Solutions", i % 2 ? LONG : "Engineer", i % 4 ? "applied" : "interview", "base", "Base resume", pack(i)),
  );
  return [...base, ...more];
}

function app(
  id: string,
  company: string,
  role: string,
  stage: ApplicationItem["stage"],
  kind: "base" | "tailored",
  label: string,
  p: ApplicationItem["pack"],
): ApplicationItem {
  return {
    id,
    company,
    role,
    url: "https://jobs.example.com/123",
    source_site: "LinkedIn",
    stage,
    note: null,
    created_at: iso(-60 * 24 * 5),
    updated_at: iso(-60 * 24),
    resume: { version_id: `r-${id}`, kind, label, status: "extracted", manual: false },
    career_os: id === "a1" ? { application_id: "co-1", pulled_at: iso(-60 * 30), content_hash: null } : null,
    pack: p,
  };
}

const JD =
  "About the role\nWe move 40,000 shipments a day across North America. You will own the services that price, route and track them.\n\nWhat you will do\n- Design and run Python and Go services on Kubernetes\n- Own on-call for the pricing and routing APIs\n- Lead incident reviews and the reliability roadmap\n\nWhat we look for\n- 6+ years building backend systems\n- Postgres at scale, queues, caching\n- Clear writing and calm under pressure";

export function applicationDetail(id: string, long: boolean): ApplicationDetail {
  const a = applications(long).find((x) => x.id === id) ?? applications(long)[0];
  return {
    ...a,
    jd_text: long ? Array.from({ length: 12 }, () => JD).join("\n\n") : JD,
    versions: [
      { id: "rb", kind: "base", label: "Base resume", status: "extracted" },
      { id: `r-${a.id}`, kind: "tailored", label: "Northwind backend v2", status: "extracted" },
    ],
  };
}

export function interviews(long: boolean): InterviewItem[] {
  const list: InterviewItem[] = [
    { id: "i1", job_id: "a1", round: "technical", starts_at: iso(45), pack_id: "p", company: "Northwind Freight", title: "Senior Backend Engineer", pack: pack(18) },
    { id: "i2", job_id: "a1", round: "final", starts_at: iso(60 * 24 * 3), pack_id: null, company: "Northwind Freight", title: "Senior Backend Engineer", pack: null },
    { id: "i3", job_id: "a4", round: "phone_screen", starts_at: iso(-60 * 24 * 6), pack_id: "p", company: "Quarry Labs", title: "Platform Engineer", pack: { pack_id: "p", available: false } },
  ];
  return long ? [...list, ...list.map((i, n) => ({ ...i, id: `${i.id}-${n}`, company: "Bluewater Logistics and Supply Chain Solutions", title: LONG }))] : list;
}

export function resumes(long: boolean): { base: ResumeVersion | null; versions: ResumeVersion[] } {
  const v = (id: string, kind: "base" | "tailored", label: string, status: ResumeVersion["status"], appRef: ResumeVersion["application"]): ResumeVersion => ({
    id,
    kind,
    label,
    source: "file",
    status,
    application: appRef,
    career_os: null,
    file: { content_type: "application/pdf" },
    created_at: iso(-60 * 24 * 10),
    updated_at: iso(-60 * 24 * 2),
    used_by: appRef ? [appRef.id] : ["a2", "a3"],
    digest: status === "extracted" ? { version: "d1", built_at: iso(-60 * 24 * 2), facts: 42 } : null,
  });
  const versions = [
    v("r-a1", "tailored", "Northwind backend v2", "extracted", { id: "a1", company: "Northwind Freight", role: "Senior Backend Engineer" }),
    v("r-a4", "tailored", "Quarry platform", "extracting", { id: "a4", company: "Quarry Labs", role: "Platform Engineer" }),
    v("r-x", "tailored", "Old draft", "failed", { id: "a3", company: "Harbor Analytics", role: "Backend Engineer" }),
  ];
  return {
    base: v("rb", "base", long ? "Alex Rivera resume 2026 final FINAL (with the platform migration rewrite and the new summary).pdf" : "Alex Rivera 2026", "extracted", null),
    versions: long ? [...versions, ...versions.map((x, i) => ({ ...x, id: `${x.id}-${i}`, label: `${x.label} copy ${i} for ${LONG}` }))] : versions,
  };
}

export function sessions(long: boolean): SessionItem[] {
  const s = (id: string, mode: SessionItem["mode"], title: string | null, startedMin: number, mins: number, status: SessionItem["status"], employer: string | null = null): SessionItem => ({
    id,
    mode,
    title,
    platform: "zoom",
    job_id: mode === "interview" ? "a1" : null,
    interview_id: null,
    employer_id: employer,
    store_transcript: true,
    started_at: iso(startedMin),
    ended_at: status === "live" ? null : iso(startedMin + mins),
    billed_seconds: mins * 60,
    me_audio_status: "live",
    status,
  });
  const list = [
    s("s1", "interview", "Northwind technical", -60 * 3, 52, "ready"),
    s("s2", "standup", null, -60 * 20, 14, "ready", "e1"),
    s("s3", "meeting", "Routing design review", -60 * 26, 38, "processing", "e1"),
    s("s4", "interview", null, -60 * 24 * 4, 31, "ready"),
  ];
  if (!long) return list;
  return [
    ...list,
    ...Array.from({ length: 20 }, (_, i) => s(`sl${i}`, i % 2 ? "standup" : "interview", i % 3 ? `Weekly sync with the platform reliability group about ${LONG}` : null, -60 * 24 * (5 + i), 20 + i, "ready", i % 2 ? "e1" : null)),
  ];
}

export function sessionDetail(id: string): SessionDetail {
  const base = sessions(false).find((s) => s.id === id) ?? sessions(false)[0];
  return {
    ...base,
    id,
    status: "ready",
    questions: 6,
    outputs: {
      summary:
        "A 52-minute technical round with the Northwind platform team. They asked about the routing outage, Postgres partitioning and alert design. You covered the rollback order well; the alerting answer ran long.",
      feedback: "Strong: concrete numbers in the incident story. Work on: lead with the decision before the detail in design questions.",
      follow_up_email:
        "Hi Jordan,\n\nThanks for the conversation today. I enjoyed digging into the routing service and how you think about alert fatigue.\n\nBest,\nAlex",
    },
  };
}

export function replay(id: string): Replay {
  return {
    session_id: id,
    stored: true,
    started_at: iso(-60 * 3),
    segments: [
      { channel: "them", seg_id: 1, start_ms: 2000, end_ms: 9000, text: "Thanks for joining. Tell me about a time you handled a production incident under pressure." },
      { channel: "me", seg_id: 1, start_ms: 10000, end_ms: 40000, text: "Sure. Last spring our routing API started timing out during the evening peak." },
      { channel: "them", seg_id: 2, start_ms: 42000, end_ms: 47000, text: "How did you decide what to roll back first?" },
    ],
    questions: [
      { id: "q1", text: "Tell me about a time you handled a production incident under pressure.", type: "behavioral", forced: false, offset_ms: 9200, feedback: "up", fast: { text: FAST1, source: "model", refs: REFS, expanded: false }, deep: { text: DEEP1, source: "model", refs: [], expanded: false } },
      { id: "q2", text: "How did you decide what to roll back first?", type: "technical", forced: false, offset_ms: 47200, feedback: null, fast: { text: FAST2, source: "model", refs: [], expanded: false }, deep: null },
    ],
  };
}

export const REFS = [
  { node_id: "n1", label: "Routing outage, 2025" },
  { node_id: "n2", label: "Northwind JD" },
];

export const FAST1 =
  "- Routing API timed out at evening peak; 30% of quotes failed [n:3f2a]\n- I led the incident: froze deploys, split the team into rollback and diagnosis\n- Root cause: a new index made the planner skip a partition\n- Back to normal in 41 minutes; wrote the review and the runbook";
export const DEEP1 =
  "- Paged at 18:05; error rate on /quote went from 0.2% to 31% in four minutes\n- First call: freeze deploys and roll back the two changes from that afternoon, newest first\n- Second: one engineer on query plans, one on the queue backlog, me on comms\n- Found the index change through pg_stat_statements; reverted it at 18:39\n- After: a canary for schema changes and a dashboard for planner regressions [~]";
export const FAST2 =
  "- Newest change first, unless an older one touches the failing path\n- Checked the deploy log against the first error timestamp\n- Rolled back config before code: faster and safer";
export const DEEP2_PARTIAL =
  "- The deploy log showed two changes in the hour before: a config flag for the quote cache and the schema migration\n- The cache flag was newer, so it went first: one command, no data risk\n- Errors stayed at 30%, which pointed";
export const FAST3_PARTIAL = "- Alert on symptoms users feel: quote latency and error rate\n- Page only when the";

export function employers(long: boolean): Employer[] {
  const list: Employer[] = [
    { id: "e1", name: "Fernhill Logistics", title: "Senior Backend Engineer", team: "Routing", started_on: "2023-04-01", ended_on: null, status: "active", current: true, created_at: iso(-60 * 24 * 200), pending_proposals: 3 },
    { id: "e2", name: "Orbit Retail", title: "Backend Engineer", team: "", started_on: "2020-01-01", ended_on: "2023-03-01", status: "active", current: false, created_at: iso(-60 * 24 * 300) },
  ];
  return long ? [...list, { ...list[0], id: "e3", name: "Bluewater Logistics and Supply Chain Solutions International", current: false, status: "archived" }] : list;
}

export function logs(long: boolean): WorkLog[] {
  const item = (id: string, kind: MemoryItem["kind"], title: string, summary: string, state = "open"): MemoryItem => ({ id, kind, title, summary, body: { state }, day: day(0), status: "active", source: {}, edited: false });
  return [
    {
      id: "l1",
      day: day(0),
      text: "Finished the quote-cache TTL change (PR 412). Paired with Sam on the partition migration plan. Blocked on the staging DB snapshot. Tomorrow: load test the new index.",
      status: "extracted",
      attachments: [{ id: "att1", name: "migration-plan.md", content_type: "text/markdown", size_bytes: 5300 }],
      updated_at: iso(-60),
      items: [item("m1", "work_item", "Quote-cache TTL change", "Merged as PR 412", "done"), item("m2", "commitment", "Load test the new index", "Tomorrow"), item("m3", "person", "Sam", "Pairs on the partition migration")],
    },
    { id: "l2", day: day(-1), text: long ? "x ".repeat(600) : "Reviewed the alerting proposal. Wrote the incident review for the 18:05 outage.", status: "extracting", attachments: [], updated_at: iso(-60 * 24) },
    { id: "l3", day: day(-2), text: "On call. Two pages, both the queue depth alarm.", status: "failed", attachments: [], updated_at: iso(-60 * 48) },
  ];
}

export function companyDocuments(): CompanyDocument[] {
  return [
    { id: "d1", name: "Routing service architecture.md", content_type: "text/markdown", size_bytes: 22000, status: "ingested", created_at: iso(-60 * 24 * 3), pending_proposals: 3, approved_proposals: 11 },
    { id: "d2", name: "Q3 reliability roadmap.pdf", content_type: "application/pdf", size_bytes: 410000, status: "ingesting", created_at: iso(-30), pending_proposals: 0, approved_proposals: 0 },
  ];
}

export function proposals(): Proposal[] {
  const p = (id: string, kind: Proposal["kind"], title: string, summary: string, evidence: string): Proposal => ({ id, employer_id: "e1", company_document_id: "d1", document_name: "Routing service architecture.md", status: "pending", kind, title, summary, evidence, body: {} });
  return [
    p("pr1", "system", "Quote service", "Prices shipments; Go, behind the API gateway", "The quote service (Go) prices every shipment request…"),
    p("pr2", "decision", "Partition by region", "Shipments table split by region since 2024", "We partition shipments by region to keep…"),
    p("pr3", "term", "Lane", "An origin-destination pair with its own rates", "A lane is an origin and destination pair…"),
  ];
}

export function memory(long: boolean): MemoryItem[] {
  const m = (id: string, kind: MemoryItem["kind"], title: string, summary: string): MemoryItem => ({ id, kind, title, summary, body: {}, day: null, status: "active", source: {}, edited: id === "f2" });
  const list = [
    m("f1", "system", "Quote service", "Prices shipments; Go"),
    m("f2", "project", "Partition migration", "Moving shipments to regional partitions by December"),
    m("f3", "person", "Sam Okafor", "Staff engineer, routing; pairs on migrations"),
    m("f4", "commitment", "Load test the new index", "Promised at Tuesday's standup"),
  ];
  return long ? [...list, ...Array.from({ length: 30 }, (_, i) => m(`fl${i}`, "term", `Term ${i}`, LONG))] : list;
}

export function careerOsItems(): CareerOsItem[] {
  const c = (id: string, company: string, role: string, pulled: CareerOsItem["pulled"]): CareerOsItem => ({
    id,
    company,
    role,
    stage: "applied",
    source_site: "LinkedIn",
    url: null,
    created_at: iso(-60 * 24 * 7),
    updated_at: iso(-60 * 24),
    user: { id: "u1", name: "Alex Rivera" },
    profile_id: "me",
    resume: "tailored",
    pulled,
  });
  return [
    c("co-1", "Northwind Freight", "Senior Backend Engineer", { application_id: "a1", pulled_at: iso(-60 * 30), changed: true }),
    c("co-2", "Cedar Systems", "Backend Engineer", null),
    c("co-3", "Lumen Health", "Staff Engineer, Payments", { application_id: "a2", pulled_at: iso(-60 * 30), changed: false }),
  ];
}
