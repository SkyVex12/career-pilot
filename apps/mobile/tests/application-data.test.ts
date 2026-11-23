import test from "node:test";
import assert from "node:assert/strict";
import {
  applicationBody,
  afterResumeDeletion,
  parseInterview,
  EMPTY_APPLICATION,
  fieldsOf,
  interviewBody,
  pairingChoices,
  parseApplication,
  parseApplications,
  parseResume,
  parseResumes,
  parseInterviews,
  parseDocumentStatuses,
  resumeBody,
  uploadApplicationDocument,
  type DocumentTransfer,
  type DocumentRequest,
  type DocumentFile,
} from "../src/applicationData.ts";

const base = {
  id: "base",
  kind: "base",
  label: "Base resume",
  status: "extracted",
};
const tailored = {
  id: "own",
  kind: "tailored",
  label: "Role resume",
  status: "extracting",
};
const application = {
  id: "a",
  company: "Acme",
  role: "Engineer",
  stage: "interview",
  url: null,
  source_site: null,
  note: "Ask about the team",
  jd_text: "Build accessible applications",
  resume: {
    version_id: "own",
    kind: "tailored",
    label: "Role resume",
    status: "extracting",
    manual: true,
  },
  versions: [base, tailored],
  pack: { available: false },
};
const resume = {
  ...base,
  source: "text",
  application: null,
  used_by: ["a"],
  text: "Résumé: 日本語",
  digest: { facts: 12 },
  file: null,
};
const fields = {
  ...EMPTY_APPLICATION,
  company: " Acme ",
  role: " Engineer ",
  jd_text: " Responsibilities ",
};
const file: DocumentFile = {
  name: "resume.pdf",
  contentType: "application/pdf",
  bytes: new Uint8Array([0, 128, 255, 10]),
};
const hash = "a".repeat(64);
const registration = {
  document_id: "doc",
  upload: {
    method: "PUT",
    url: "https://storage.example/resume?signature=opaque",
    headers: { "Content-Type": "application/pdf" },
  },
};
const controller = () => new AbortController();
const requestFor = (
  handler: (path: string, body?: unknown, method?: string) => Promise<unknown>,
) => handler as DocumentRequest;

test("application details retain only the server-authorized resume pairing choices", () => {
  const parsed = parseApplication(application, true);
  assert.deepEqual(
    pairingChoices(parsed).map((choice) => choice.value),
    ["auto", "base", "own"],
  );
  assert.equal(parsed.resume?.manual, true);
  assert.equal(parsed.jd_text, application.jd_text);
  assert.deepEqual(fieldsOf(parsed), {
    company: "Acme",
    role: "Engineer",
    url: "",
    source_site: "",
    stage: "interview",
    note: application.note,
    jd_text: application.jd_text,
  });
  assert.equal(parseApplications({ applications: [application] })[0].id, "a");
  assert.equal(
    parseApplication({ ...application, versions: undefined }).versions.length,
    0,
  );
});

test("malformed nested response fields fail before entering mobile rendering", () => {
  assert.throws(() => parseApplications({ applications: null }));
  assert.throws(() =>
    parseApplication({ ...application, stage: "unsupported" }),
  );
  assert.throws(() =>
    parseApplication({
      ...application,
      resume: { ...application.resume, manual: "true" },
    }),
  );
  assert.throws(() =>
    parseApplication({
      ...application,
      versions: [{ ...base, status: "unknown" }],
    }),
  );
  assert.throws(() =>
    parseApplication({ ...application, pack: { available: "yes" } }),
  );
  assert.throws(() => parseApplication({ ...application, jd_text: {} }, true));
  assert.throws(() => parseResume({ ...resume, digest: { facts: -1 } }));
  assert.throws(() => parseResume({ ...resume, text: {} }));
  assert.throws(() => parseResumes({ base: resume, versions: {} }));
  assert.throws(() =>
    parseInterviews({
      interviews: [
        {
          id: "i",
          job_id: "a",
          round: "technical",
          starts_at: "tomorrow",
          pack: null,
        },
      ],
    }),
  );
});

test("resume metadata preserves Unicode, extraction state, and null original file text", () => {
  assert.equal(parseResume(resume).text, "Résumé: 日本語");
  const uploaded = {
    ...resume,
    id: "file",
    kind: "tailored",
    source: "file",
    text: null,
    application: { id: "a", company: "Acme", role: "Engineer" },
  };
  assert.equal(parseResumes({ base: resume, versions: [uploaded] }).length, 2);
  assert.equal(parseResume(uploaded).text, null);
  assert.equal(parseResumes({ base: null, versions: [] }).length, 0);
});

test("document processing receipts distinguish unfinished, completed and failed extraction", () => {
  assert.deepEqual(
    parseDocumentStatuses({
      documents: [
        { id: "pending", status: "ingesting" },
        { id: "done", status: "ingested" },
        { id: "bad", status: "failed" },
      ],
    }),
    [
      { id: "pending", status: "ingesting" },
      { id: "done", status: "ingested" },
      { id: "bad", status: "failed" },
    ],
  );
  assert.throws(() =>
    parseDocumentStatuses({ documents: [{ id: "a", status: "unknown" }] }),
  );
});

test("application writes validate required content, stage, bounds and credential-free links", () => {
  assert.deepEqual(applicationBody(fields), {
    company: "Acme",
    role: "Engineer",
    jd_text: "Responsibilities",
    url: null,
    source_site: null,
    stage: "applied",
    note: null,
  });
  for (const key of ["company", "role", "jd_text"] as const)
    assert.throws(() => applicationBody({ ...fields, [key]: "  " }));
  assert.throws(() => applicationBody({ ...fields, note: "x".repeat(10_001) }));
  assert.throws(() => applicationBody({ ...fields, company: "x".repeat(201) }));
  assert.throws(() =>
    applicationBody({ ...fields, stage: "unknown" as never }),
  );
  for (const url of [
    "javascript:alert(1)",
    "https://user:password@example.com",
    "example.com/job",
  ])
    assert.throws(() => applicationBody({ ...fields, url }));
  assert.equal(
    applicationBody({ ...fields, url: "https://example.com/jobs?id=7" }).url,
    "https://example.com/jobs?id=7",
  );
});

test("resume writes cannot accidentally create tailored content in another scope", () => {
  assert.deepEqual(resumeBody("  Personal  ", " Résumé ", "base"), {
    kind: "base",
    label: "Personal",
    text: "Résumé",
  });
  assert.deepEqual(resumeBody("", "Experience", "tailored", "a"), {
    kind: "tailored",
    label: null,
    text: "Experience",
    application_id: "a",
  });
  assert.throws(() => resumeBody("", "Experience", "base", "a"));
  assert.throws(() => resumeBody("", "Experience", "tailored"));
  assert.throws(() => resumeBody("", " ", "base"));
  assert.throws(() => resumeBody("x".repeat(201), "Experience", "base"));
  assert.throws(() => resumeBody("", "x".repeat(100_001), "base"));
});

test("scheduled interviews convert the selected local time to an unambiguous ISO time", () => {
  const result = interviewBody("a", "system_design", "2028-02-29", "09:30");
  assert.deepEqual(result, {
    job_id: "a",
    round: "system_design",
    starts_at: new Date("2028-02-29T09:30:00").toISOString(),
  });
  for (const [day, time] of [
    ["2026-02-29", "09:30"],
    ["2026-12-01", "24:01"],
    ["2026-12-01", "9:30"],
    ["2026-12-01", "09:60"],
  ])
    assert.throws(() => interviewBody("a", "technical", day, time));
  assert.throws(() => interviewBody("", "technical", "2026-12-01", "09:30"));
  assert.throws(() => interviewBody("a", "unsupported", "2026-12-01", "09:30"));
  assert.equal(
    parseInterviews({
      interviews: [{ id: "i", ...result, pack: { available: false } }],
    })[0].starts_at,
    result.starts_at,
  );
});

test("PDF upload signs exact bytes and never forwards account or profile credentials to storage", async () => {
  const requests: {
    path: string;
    body: unknown;
    method: string | undefined;
  }[] = [];
  const transfers: DocumentTransfer[] = [];
  const result = await uploadApplicationDocument({
    file,
    target: { kind: "resume", application_id: "a", label: "Résumé" },
    signal: controller().signal,
    hash: async (bytes) => {
      assert.deepEqual(bytes, file.bytes);
      return hash;
    },
    request: requestFor(async (path, body, method) => {
      requests.push({ path, body, method });
      return path === "/documents" ? registration : { status: "ingesting" };
    }),
    fetcher: (async (url, init) => {
      assert.equal(url, registration.upload.url);
      assert.equal(init?.method, "PUT");
      assert.deepEqual(init?.headers, { "Content-Type": "application/pdf" });
      assert.equal(init?.credentials, "omit");
      assert.equal(init?.redirect, "error");
      assert.deepEqual(new Uint8Array(init?.body as ArrayBuffer), file.bytes);
      return new Response(null, { status: 200 });
    }) as typeof fetch,
    progress: (state) => transfers.push(state),
  });
  assert.deepEqual(result, { status: "ingesting" });
  assert.deepEqual(requests, [
    {
      path: "/documents",
      body: {
        kind: "resume",
        application_id: "a",
        label: "Résumé",
        content_type: file.contentType,
        size_bytes: 4,
        sha256: hash,
      },
      method: "POST",
    },
    { path: "/documents/doc/ingest", body: {}, method: "POST" },
  ]);
  assert.deepEqual(
    transfers.map((t) => t.uploaded),
    [false, true],
  );
});

test("retry after an uncertain ingestion receipt keeps the same document and does not upload twice", async () => {
  let registered = 0,
    uploads = 0,
    ingested = 0;
  let pending: DocumentTransfer | undefined;
  const request = requestFor(async (path) => {
    if (path === "/documents") {
      registered++;
      return registration;
    }
    ingested++;
    if (ingested === 1) throw new Error("Connection interrupted after upload");
    return { status: "ingesting" };
  });
  const options = {
    file,
    target: { kind: "resume" as const },
    signal: controller().signal,
    request,
    hash: async () => hash,
    progress: (transfer: DocumentTransfer) => {
      pending = transfer;
    },
    fetcher: (async () => {
      uploads++;
      return new Response(null, { status: 200 });
    }) as typeof fetch,
  };
  await assert.rejects(
    uploadApplicationDocument(options),
    /Connection interrupted/,
  );
  assert.equal(pending?.uploaded, true);
  await uploadApplicationDocument({ ...options, pending });
  assert.deepEqual(
    { registered, uploads, ingested },
    { registered: 1, uploads: 1, ingested: 2 },
  );
});

test("failed storage upload retains its registration for retry and cannot start ingestion", async () => {
  let registrations = 0,
    ingestions = 0,
    pending: DocumentTransfer | undefined;
  const request = requestFor(async (path) => {
    if (path === "/documents") {
      registrations++;
      return registration;
    }
    ingestions++;
    return {};
  });
  const options = {
    file,
    target: { kind: "resume" as const },
    signal: controller().signal,
    request,
    hash: async () => hash,
    progress: (transfer: DocumentTransfer) => {
      pending = transfer;
    },
    fetcher: (async () => new Response(null, { status: 503 })) as typeof fetch,
  };
  await assert.rejects(uploadApplicationDocument(options), /503/);
  assert.equal(pending?.uploaded, false);
  assert.equal(ingestions, 0);
  await uploadApplicationDocument({
    ...options,
    pending,
    fetcher: (async () => new Response(null, { status: 200 })) as typeof fetch,
  });
  assert.equal(registrations, 1);
  assert.equal(ingestions, 1);
});

test("untrusted signed upload locations and sensitive headers cannot receive resume bytes", async () => {
  for (const upload of [
    { ...registration.upload, url: "http://storage.example/file" },
    {
      ...registration.upload,
      url: "https://password:secret@storage.example/file",
    },
    { ...registration.upload, url: "https://storage.example/file#fragment" },
    { ...registration.upload, method: "POST" },
    {
      ...registration.upload,
      headers: { Authorization: "secret", "Content-Type": "application/pdf" },
    },
    {
      ...registration.upload,
      headers: {
        "x-copilot-profile": "other",
        "Content-Type": "application/pdf",
      },
    },
    { ...registration.upload, headers: { "Content-Type": "text/plain" } },
  ]) {
    let fetched = false;
    await assert.rejects(
      uploadApplicationDocument({
        file,
        target: { kind: "resume" },
        signal: controller().signal,
        hash: async () => hash,
        request: requestFor(async () => ({ ...registration, upload })),
        progress: () => {},
        fetcher: (async () => {
          fetched = true;
          return new Response();
        }) as typeof fetch,
      }),
    );
    assert.equal(fetched, false);
  }
});

test("cleartext storage is allowed only for an explicit HTTP test build", async () => {
  let fetched = false;
  await uploadApplicationDocument({
    file,
    target: { kind: "resume" },
    signal: controller().signal,
    hash: async () => hash,
    allowHttp: true,
    request: requestFor(async (path) =>
      path === "/documents"
        ? {
            ...registration,
            upload: {
              ...registration.upload,
              url: "http://127.0.0.1:9000/file?signature=opaque",
            },
          }
        : {},
    ),
    progress: () => {},
    fetcher: (async () => {
      fetched = true;
      return new Response();
    }) as typeof fetch,
  });
  assert.equal(fetched, true);
});

test("cancelled file selection cannot register or ingest data after profile teardown", async () => {
  const abort = controller();
  let requests = 0;
  await assert.rejects(
    uploadApplicationDocument({
      file,
      target: { kind: "resume" },
      signal: abort.signal,
      hash: async () => {
        abort.abort();
        return hash;
      },
      request: requestFor(async () => {
        requests++;
        return registration;
      }),
      progress: () => {},
    }),
    { name: "AbortError" },
  );
  assert.equal(requests, 0);
  const abortAfterPut = controller();
  await assert.rejects(
    uploadApplicationDocument({
      file,
      target: { kind: "resume" },
      signal: abortAfterPut.signal,
      hash: async () => hash,
      request: requestFor(async () => {
        requests++;
        return registration;
      }),
      progress: () => {},
      fetcher: (async () => {
        abortAfterPut.abort();
        return new Response();
      }) as typeof fetch,
    }),
    { name: "AbortError" },
  );
  assert.equal(requests, 1);
});

test("empty, oversized, unsupported and unhashable files fail before storage registration", async () => {
  for (const invalid of [
    { ...file, bytes: new Uint8Array() },
    { ...file, bytes: new Uint8Array(10 * 1024 * 1024 + 1) },
    { ...file, contentType: "application/octet-stream" },
  ]) {
    let requested = false;
    await assert.rejects(
      uploadApplicationDocument({
        file: invalid,
        target: { kind: "resume" },
        signal: controller().signal,
        hash: async () => hash,
        request: requestFor(async () => {
          requested = true;
          return registration;
        }),
        progress: () => {},
      }),
    );
    assert.equal(requested, false);
  }
  await assert.rejects(
    uploadApplicationDocument({
      file,
      target: { kind: "resume" },
      signal: controller().signal,
      hash: async () => "invalid",
      request: requestFor(async () => {
        throw new Error("Should not register");
      }),
      progress: () => {},
    }),
    /verify/,
  );
});

test("a committed resume deletion removes stale detail and picker references before refresh", () => {
  const previous = parseApplication(application, true);
  const updated = afterResumeDeletion(previous, "own");
  assert.equal(updated.resume, null);
  assert.equal(updated.pairing_pending, true);
  assert.equal(updated.pack, null);
  assert.deepEqual(
    updated.versions.map((row) => row.id),
    ["base"],
  );
  assert.equal(previous.resume?.version_id, "own");
  const unrelated = afterResumeDeletion(previous, "someone-else");
  assert.equal(unrelated.resume, previous.resume);
  assert.equal(unrelated.pairing_pending, undefined);
  // A subsequent authoritative read clears the pending marker and resolves fallback.
  assert.equal(parseApplication(application).pairing_pending, undefined);
});

test("a schedule receipt is usable immediately without depending on another catalog read", () => {
  const saved = parseInterview({
    id: "new-interview",
    job_id: "a",
    round: "technical",
    starts_at: "2026-11-24T10:00:00Z",
    pack: { available: false },
  });
  assert.equal(saved.id, "new-interview");
  assert.equal(saved.job_id, "a");
  assert.equal(saved.pack?.available, false);
  assert.throws(() => parseInterview({ ...saved, starts_at: "tomorrow" }));
  assert.throws(() => parseInterview({ ...saved, id: null }));
});
