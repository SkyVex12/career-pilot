import { list, nullableText, record, text, validDay } from "./workspaceData.ts";
import { withDeadline } from "./requestScope.ts";

export const STAGES = [
  { value: "applied", label: "Applied" },
  { value: "interview", label: "Interviewing" },
  { value: "offer", label: "Offer" },
  { value: "rejected", label: "Rejected" },
] as const;
export const ROUNDS = [
  { value: "phone_screen", label: "Phone screen" },
  { value: "technical", label: "Technical" },
  { value: "system_design", label: "System design" },
  { value: "final", label: "Final" },
] as const;
export type Stage = (typeof STAGES)[number]["value"];
export type ResumeKind = "base" | "tailored";
export type PackState = {
  available: boolean;
  operation_state: "queued" | "running" | "succeeded" | "failed" | "idle";
};
export type ResumeStatus = "saved" | "extracting" | "extracted" | "failed";
export type ResumeChoice = {
  id: string;
  kind: ResumeKind;
  label: string;
  status: ResumeStatus;
};
export type Application = {
  id: string;
  company: string;
  role: string;
  url: string | null;
  source_site: string | null;
  stage: Stage;
  note: string | null;
  jd_text?: string;
  pairing_pending?: boolean;
  career_os: {
    application_id: string;
    pulled_at: string;
    content_hash: string | null;
  } | null;
  resume:
    (Omit<ResumeChoice, "id"> & { version_id: string; manual: boolean }) | null;
  versions: ResumeChoice[];
  pack: PackState | null;
};
export type Resume = ResumeChoice & {
  source: string;
  text?: string | null;
  used_by: string[];
  application: { id: string; company: string; role: string } | null;
  file: { content_type: string } | null;
  digest: { facts: number } | null;
};
export type Interview = {
  id: string;
  job_id: string;
  round: string;
  starts_at: string;
  pack: PackState | null;
};
export type ApplicationFields = {
  company: string;
  role: string;
  url: string;
  source_site: string;
  stage: Stage;
  note: string;
  jd_text: string;
};
export const EMPTY_APPLICATION: ApplicationFields = {
  company: "",
  role: "",
  url: "",
  source_site: "",
  stage: "applied",
  note: "",
  jd_text: "",
};

function kind(value: unknown): ResumeKind {
  if (value !== "base" && value !== "tailored")
    throw new Error("Unrecognized resume kind. Refresh and retry.");
  return value;
}
function status(value: unknown): ResumeStatus {
  if (!["saved", "extracting", "extracted", "failed"].includes(String(value)))
    throw new Error("Unrecognized resume status. Refresh and retry.");
  return value as ResumeStatus;
}
function pack(value: unknown) {
  if (value == null) return null;
  const r = record(value);
  if (typeof r.available !== "boolean")
    throw new Error("Invalid interview preparation status.");
  const state = r.operation_state ?? "idle";
  if (
    !["queued", "running", "succeeded", "failed", "idle"].includes(
      String(state),
    )
  )
    throw new Error("Invalid preparation operation state.");
  return {
    available: r.available,
    operation_state: state as PackState["operation_state"],
  };
}
export function preparationPending(value: { pack: PackState | null }): boolean {
  return (
    value.pack?.operation_state === "queued" ||
    value.pack?.operation_state === "running"
  );
}
export function parseResumeChoice(value: unknown): ResumeChoice {
  const r = record(value);
  return {
    id: text(r.id),
    kind: kind(r.kind),
    label: text(r.label),
    status: status(r.status),
  };
}
export function parseApplication(
  value: unknown,
  detailed = false,
): Application {
  const r = record(value);
  if (!STAGES.some((s) => s.value === r.stage))
    throw new Error("Unrecognized application stage. Refresh and retry.");
  const origin = r.career_os == null ? null : record(r.career_os);
  if (
    origin &&
    (!/^[A-Za-z0-9_.:-]{1,128}$/.test(text(origin.application_id)) ||
      !Number.isFinite(Date.parse(text(origin.pulled_at))))
  )
    throw new Error("Invalid HubRuby application origin.");
  const paired = r.resume == null ? null : record(r.resume);
  if (paired && typeof paired.manual !== "boolean")
    throw new Error("Invalid resume pairing.");
  return {
    id: text(r.id),
    company: text(r.company),
    role: text(r.role),
    url: nullableText(r.url),
    source_site: nullableText(r.source_site),
    stage: r.stage as Stage,
    note: nullableText(r.note),
    career_os: origin
      ? {
          application_id: text(origin.application_id),
          pulled_at: text(origin.pulled_at),
          content_hash: nullableText(origin.content_hash),
        }
      : null,
    ...(detailed ? { jd_text: text(r.jd_text) } : {}),
    versions: r.versions == null ? [] : list(r.versions, parseResumeChoice),
    pack: pack(r.pack),
    resume: paired
      ? {
          version_id: text(paired.version_id),
          kind: kind(paired.kind),
          label: text(paired.label),
          status: status(paired.status),
          manual: paired.manual as boolean,
        }
      : null,
  };
}
export const parseApplications = (value: unknown) =>
  list(record(value).applications, (v) => parseApplication(v));
export function parseResume(value: unknown): Resume {
  const r = record(value),
    app = r.application == null ? null : record(r.application),
    digest = r.digest == null ? null : record(r.digest);
  if (
    digest &&
    (typeof digest.facts !== "number" ||
      !Number.isInteger(digest.facts) ||
      digest.facts < 0)
  )
    throw new Error("Invalid resume details.");
  return {
    ...parseResumeChoice(r),
    source: text(r.source),
    used_by: list(r.used_by, text),
    ...(r.text !== undefined ? { text: nullableText(r.text) } : {}),
    application: app
      ? { id: text(app.id), company: text(app.company), role: text(app.role) }
      : null,
    file:
      r.file == null
        ? null
        : { content_type: text(record(r.file).content_type) },
    digest: digest ? { facts: digest.facts as number } : null,
  };
}
export function parseResumes(value: unknown): Resume[] {
  const r = record(value);
  return [
    ...(r.base == null ? [] : [parseResume(r.base)]),
    ...list(r.versions, parseResume),
  ];
}
export function parseInterview(value: unknown): Interview {
  const r = record(value),
    starts_at = text(r.starts_at);
  if (!Number.isFinite(Date.parse(starts_at)))
    throw new Error("Invalid interview date.");
  return {
    id: text(r.id),
    job_id: text(r.job_id),
    round: text(r.round),
    starts_at,
    pack: pack(r.pack),
  };
}
export function parseInterviews(value: unknown): Interview[] {
  return list(record(value).interviews, parseInterview);
}
/** A committed delete must remove every stale reference even if the next GET fails.
 * The automatic fallback is resolved by the server; do not guess from a stale catalog. */
export function afterResumeDeletion(
  application: Application,
  deleted: string,
): Application {
  const versions = application.versions.filter(
    (version) => version.id !== deleted,
  );
  return application.resume?.version_id === deleted
    ? {
        ...application,
        versions,
        resume: null,
        pairing_pending: true,
        pack: null,
      }
    : { ...application, versions };
}
export function parseDocumentStatuses(
  value: unknown,
): { id: string; status: "uploaded" | "ingesting" | "ingested" | "failed" }[] {
  return list(record(value).documents, (value) => {
    const r = record(value);
    if (
      !["uploaded", "ingesting", "ingested", "failed"].includes(
        String(r.status),
      )
    )
      throw new Error("Unrecognized document processing status.");
    return {
      id: text(r.id),
      status: r.status as "uploaded" | "ingesting" | "ingested" | "failed",
    };
  });
}
export function fieldsOf(app: Application): ApplicationFields {
  return {
    company: app.company,
    role: app.role,
    url: app.url ?? "",
    source_site: app.source_site ?? "",
    stage: app.stage,
    note: app.note ?? "",
    jd_text: app.jd_text ?? "",
  };
}
export function applicationBody(fields: ApplicationFields) {
  const bounded = (
    value: string,
    label: string,
    max: number,
    required = false,
  ) => {
    const result = value.trim();
    if ((required && !result) || result.length > max)
      throw new Error(
        `${label} must contain ${required ? "1" : "0"}-${max.toLocaleString()} characters.`,
      );
    return result;
  };
  const url = bounded(fields.url, "Job link", 2000);
  if (url) {
    let link: URL;
    try {
      link = new URL(url);
    } catch {
      throw new Error("Enter a complete http:// or https:// job link.");
    }
    if (
      !["https:", "http:"].includes(link.protocol) ||
      link.username ||
      link.password
    )
      throw new Error("Enter an HTTP or HTTPS job link without credentials.");
  }
  if (!STAGES.some((stage) => stage.value === fields.stage))
    throw new Error("Choose a valid application stage.");
  return {
    company: bounded(fields.company, "Company", 200, true),
    role: bounded(fields.role, "Role", 200, true),
    url: url || null,
    source_site: bounded(fields.source_site, "Source", 200) || null,
    stage: fields.stage,
    note: bounded(fields.note, "Notes", 10_000) || null,
    jd_text: bounded(fields.jd_text, "Job description", 50_000, true),
  };
}
export function pairingChoices(app: Application) {
  return [
    { value: "auto", label: "Automatic: tailored resume, then base resume" },
    ...app.versions.map((v) => ({
      value: v.id,
      label: `${v.kind === "base" ? "Base" : "Tailored"}: ${v.label}`,
    })),
  ];
}
export function resumeBody(
  label: string,
  value: string,
  kind: ResumeKind,
  applicationId?: string,
) {
  if (label.trim().length > 200)
    throw new Error("Resume labels must be at most 200 characters.");
  const text = value.trim();
  if (!text || text.length > 100_000)
    throw new Error("Paste resume text of 1-100,000 characters.");
  if ((kind === "tailored") !== !!applicationId)
    throw new Error("Choose the application for this tailored resume.");
  return {
    kind,
    label: label.trim() || null,
    text,
    ...(applicationId ? { application_id: applicationId } : {}),
  };
}
export function interviewBody(
  applicationId: string,
  round: string,
  day: string,
  time: string,
) {
  if (!applicationId || !ROUNDS.some((r) => r.value === round))
    throw new Error("Choose an application and interview round.");
  if (!validDay(day) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time))
    throw new Error("Enter a valid date (YYYY-MM-DD) and time (HH:mm).");
  const date = new Date(`${day}T${time}:00`);
  const pad = (n: number) => String(n).padStart(2, "0");
  if (
    !Number.isFinite(date.getTime()) ||
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` !==
      day ||
    `${pad(date.getHours())}:${pad(date.getMinutes())}` !== time
  )
    throw new Error("That local time does not exist. Choose another time.");
  return { job_id: applicationId, round, starts_at: date.toISOString() };
}
export const resumeStatus = (status: ResumeStatus) =>
  ({
    saved: "Saved",
    extracting: "Reading resume",
    extracted: "Ready",
    failed: "Could not read resume",
  })[status];

export type DocumentFile = {
  name: string;
  contentType: string;
  bytes: Uint8Array;
};
export type DocumentTarget = {
  kind: "resume" | "job_description";
  application_id?: string;
  resume_version_id?: string;
  label?: string;
};
export type DocumentTransfer = {
  documentId: string;
  url: string;
  headers: Record<string, string>;
  uploaded: boolean;
};
export type DocumentRequest = <T = unknown>(
  path: string,
  body?: unknown,
  method?: string,
) => Promise<T>;
/** Signed storage requests deliberately never use Api.request or account/profile headers. */
export async function uploadApplicationDocument(options: {
  request: DocumentRequest;
  file: DocumentFile;
  target: DocumentTarget;
  signal: AbortSignal;
  hash: (bytes: Uint8Array) => Promise<string>;
  fetcher?: typeof fetch;
  allowHttp?: boolean;
  pending?: DocumentTransfer;
  progress: (transfer: DocumentTransfer) => void;
}): Promise<unknown> {
  const { file, request, target, signal } = options;
  if (
    !file.bytes.byteLength ||
    file.bytes.byteLength > 10 * 1024 * 1024 ||
    !["application/pdf", "text/plain", "text/markdown"].includes(
      file.contentType,
    )
  )
    throw new Error("Choose a PDF, text or Markdown file of 1 byte to 10 MB.");
  if (target.label && target.label.length > 200)
    throw new Error("Resume labels must be at most 200 characters.");
  signal.throwIfAborted();
  let transfer = options.pending;
  if (!transfer) {
    const sha256 = await options.hash(file.bytes);
    signal.throwIfAborted();
    if (!/^[a-f0-9]{64}$/.test(sha256))
      throw new Error("Could not verify the selected file.");
    const registration = record(
      await request(
        "/documents",
        {
          ...target,
          content_type: file.contentType,
          size_bytes: file.bytes.byteLength,
          sha256,
        },
        "POST",
      ),
    );
    const upload = record(registration.upload),
      headers = record(upload.headers),
      cleanHeaders: Record<string, string> = {};
    if (upload.method !== "PUT")
      throw new Error("The server returned an unsupported upload method.");
    const url = new URL(text(upload.url));
    if (
      url.username ||
      url.password ||
      url.hash ||
      (url.protocol !== "https:" &&
        !(options.allowHttp && url.protocol === "http:"))
    )
      throw new Error("The server returned an unsafe upload address.");
    for (const [name, value] of Object.entries(headers)) {
      if (
        !/^[a-z0-9-]+$/i.test(name) ||
        /^(authorization|cookie|host|x-copilot-profile)$/i.test(name)
      )
        throw new Error("The server returned unsupported upload headers.");
      cleanHeaders[name] = text(value);
    }
    const contentType = Object.entries(cleanHeaders).find(
      ([key]) => key.toLowerCase() === "content-type",
    )?.[1];
    if (contentType !== file.contentType)
      throw new Error("The server returned a different document type.");
    transfer = {
      documentId: text(registration.document_id),
      url: url.href,
      headers: cleanHeaders,
      uploaded: false,
    };
    options.progress(transfer);
  }
  signal.throwIfAborted();
  if (!transfer.uploaded) {
    const response = await withDeadline(
      (bounded) =>
        (options.fetcher ?? fetch)(transfer!.url, {
          method: "PUT",
          headers: transfer!.headers,
          body: new Uint8Array(file.bytes).buffer,
          signal: bounded,
          credentials: "omit",
          redirect: "error",
        }),
      120_000,
      signal,
    );
    if (!response.ok)
      throw new Error(
        `File upload failed (${response.status}). Retry or choose the file again.`,
      );
    signal.throwIfAborted();
    transfer = { ...transfer, uploaded: true };
    options.progress(transfer);
  }
  signal.throwIfAborted();
  return request(
    `/documents/${encodeURIComponent(transfer.documentId)}/ingest`,
    {},
    "POST",
  );
}
