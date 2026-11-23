import test from "node:test";
import assert from "node:assert/strict";
import { parseApplication } from "../src/applicationData.ts";
import {
  hubRubyCode,
  hubRubyDestination,
  hubRubyError,
  hubRubySearchPath,
  parseHubRubyConnection,
  parseHubRubyProfiles,
  parseHubRubyResults,
  parseHubRubyStatus,
  pullHubRubyApplication,
  type HubRubyRequest,
} from "../src/hubRubyData.ts";
const when = "2026-11-23T18:00:00Z";
const connection = {
  available: true,
  connected: true,
  required: false,
  status: "active",
  user: { id: "u1", name: "Alex" },
  account_type: "user",
  connected_at: when,
  last_pull_at: null,
};
const profiles = {
  items: [
    { id: "profile-a", name: "Alex", me: true, career_os_user_id: "u1" },
    { id: "profile-b", name: "Blair", me: false, career_os_user_id: "u2" },
  ],
};
const item = {
  id: "source-app",
  company: "Acme",
  role: "Engineer",
  stage: "interview",
  user: { id: "u1", name: "Alex" },
  profile_id: "profile-a",
  resume: "tailored",
  pulled: null,
};
const app = {
  id: "app-a",
  company: "Acme",
  role: "Engineer",
  stage: "interview",
  jd_text: "Build accessible tools",
  resume: null,
  versions: [],
  pack: null,
  career_os: { application_id: item.id, pulled_at: when, content_hash: "sha" },
  profile_id: "profile-a",
  pulled: "new",
};
const parseItem = (value = item) =>
  parseHubRubyResults({ items: [value], next_cursor: null }).items[0];
const requestFor = (
  handler: (path: string, body?: unknown, method?: string) => Promise<unknown>,
) => handler as HubRubyRequest;
const expected = { profileId: "profile-a", name: "Alex", current: true };
const options = (request: HubRubyRequest) => ({
  request,
  item: parseItem(),
  profile: "",
  expected,
  signal: new AbortController().signal,
});

test("connection availability and revocation remain explicit without exposing tokens", () => {
  assert.equal(
    parseHubRubyConnection({ available: false, connected: false }).available,
    false,
  );
  assert.equal(parseHubRubyConnection(connection).user?.name, "Alex");
  assert.equal(
    parseHubRubyConnection({
      ...connection,
      connected: false,
      status: "revoked",
    }).status,
    "revoked",
  );
  for (const value of [
    { ...connection, connected: "true" },
    { ...connection, status: "pending" },
    { ...connection, account_type: "root" },
    { ...connection, user: null },
    { ...connection, connected_at: "yesterday" },
  ])
    assert.throws(() => parseHubRubyConnection(value));
});

test("profile and result parsing rejects ambiguous owners and malformed remote fields", () => {
  assert.equal(parseHubRubyProfiles(profiles).length, 2);
  assert.throws(() =>
    parseHubRubyProfiles({ items: [profiles.items[0], profiles.items[0]] }),
  );
  assert.throws(() => parseHubRubyProfiles({ items: [profiles.items[1]] }));
  for (const changed of [
    { id: "../../secret" },
    { resume: "anything" },
    { role: "" },
    { pulled: { application_id: "a", pulled_at: when, changed: "true" } },
  ])
    assert.throws(() => parseItem({ ...item, ...changed } as typeof item));
  assert.throws(() => parseHubRubyResults({ items: [item, item] }));
  assert.equal(
    parseHubRubyResults({ items: [item], next_cursor: "opaque/&" }).next,
    "opaque/&",
  );
});

test("search only sends explicit query, stage, authorized source and opaque pagination", () => {
  const url = new URL(
    `https://example.test${hubRubySearchPath("  Acme & Sons  ", "interview", "u1", "next/+=&")}`,
  );
  assert.equal(url.searchParams.get("q"), "Acme & Sons");
  assert.equal(url.searchParams.get("stage"), "interview");
  assert.equal(url.searchParams.get("user_id"), "u1");
  assert.equal(url.searchParams.get("cursor"), "next/+=&");
  assert.equal(url.searchParams.get("limit"), "25");
  assert.throws(() => hubRubySearchPath("a".repeat(201), "", ""));
  assert.throws(() => hubRubySearchPath("", "admin" as never, ""));
  assert.throws(() => hubRubySearchPath("", "", "u1&user_id=u2"));
  assert.deepEqual(hubRubyCode(" abc-def "), { code: "abc-def" });
  assert.throws(() => hubRubyCode("abc"));
  assert.throws(() => hubRubyCode("a".repeat(65)));
});

test("the empty mobile profile selector resolves to the actual Me profile id", () => {
  assert.deepEqual(
    hubRubyDestination(
      parseItem(),
      parseHubRubyProfiles(profiles),
      "",
      parseHubRubyConnection(connection),
    ),
    expected,
  );
  const missingLegacyOwner = parseHubRubyResults({
    items: [{ ...item, user: null }],
    next_cursor: null,
  }).items[0];
  assert.equal(
    hubRubyDestination(
      missingLegacyOwner,
      parseHubRubyProfiles(profiles),
      "",
      parseHubRubyConnection(connection),
    ).profileId,
    "profile-a",
  );
});

test("admin imports target their source owner's profile and cannot masquerade as the selected profile", () => {
  const admin = parseHubRubyConnection({
    ...connection,
    account_type: "admin",
    user: { id: "admin1", name: "Admin" },
  });
  const theirs = parseItem({
    ...item,
    user: { id: "u2", name: "Blair" },
    profile_id: "profile-b",
  });
  assert.deepEqual(
    hubRubyDestination(theirs, parseHubRubyProfiles(profiles), "", admin),
    { profileId: "profile-b", name: "Blair", current: false },
  );
  assert.equal(
    hubRubyDestination(
      theirs,
      parseHubRubyProfiles(profiles),
      "profile-b",
      admin,
    ).current,
    true,
  );
  assert.throws(
    () =>
      hubRubyDestination(
        parseItem(),
        parseHubRubyProfiles(profiles),
        "profile-b",
        admin,
      ),
    /another profile/,
  );
  assert.throws(
    () =>
      hubRubyDestination(
        parseItem({ ...item, profile_id: "profile-b" }),
        parseHubRubyProfiles(profiles),
        "",
        admin,
      ),
    /owner/,
  );
  assert.throws(
    () =>
      hubRubyDestination(
        theirs,
        parseHubRubyProfiles(profiles),
        "",
        parseHubRubyConnection(connection),
      ),
    /linked HubRuby account/,
  );
  assert.throws(
    () =>
      hubRubyDestination(
        theirs,
        parseHubRubyProfiles({ items: [profiles.items[0]] }),
        "",
        admin,
      ),
    /owner/,
  );
});

test("pull revalidates connection and ownership, sends one POST and validates its receipt", async () => {
  const calls: string[] = [];
  const result = await pullHubRubyApplication(
    options(
      requestFor(async (path, body, method) => {
        calls.push(`${method ?? "GET"} ${path}`);
        if (path === "/career-os/connection") return connection;
        if (path === "/profiles") return profiles;
        assert.equal(body, undefined);
        return app;
      }),
    ),
  );
  assert.deepEqual(calls, [
    "GET /career-os/connection",
    "GET /profiles",
    "POST /career-os/applications/source-app/pull",
  ]);
  assert.equal(result.application.id, "app-a");
  assert.equal(result.again, false);
  assert.deepEqual(result.destination, expected);
  assert.equal(result.application.career_os?.application_id, item.id);
});

test("revoked access or changed source destination prevents the write", async () => {
  for (const [conn, homes] of [
    [{ ...connection, connected: false, status: "revoked" }, profiles],
    [connection, { items: [{ ...profiles.items[0], id: "new-profile" }] }],
  ]) {
    let posted = false;
    await assert.rejects(
      pullHubRubyApplication(
        options(
          requestFor(async (path, body, method) => {
            if (method === "POST") posted = true;
            return path === "/career-os/connection" ? conn : homes;
          }),
        ),
      ),
    );
    assert.equal(posted, false);
  }
});

test("abort after profile load prevents importing into a view that has closed", async () => {
  const abort = new AbortController();
  let posted = false;
  const request = requestFor(async (path, body, method) => {
    if (method === "POST") posted = true;
    if (path === "/career-os/connection") return connection;
    abort.abort();
    return profiles;
  });
  await assert.rejects(
    pullHubRubyApplication({ ...options(request), signal: abort.signal }),
    { name: "AbortError" },
  );
  assert.equal(posted, false);
});

test("a mismatched import acknowledgement never enters the current application UI", async () => {
  for (const bad of [
    { ...app, profile_id: "profile-b" },
    { ...app, career_os: { ...app.career_os, application_id: "different" } },
    { ...app, pulled: "unknown" },
  ]) {
    const request = requestFor(async (path) =>
      path === "/career-os/connection"
        ? connection
        : path === "/profiles"
          ? profiles
          : bad,
    );
    await assert.rejects(
      pullHubRubyApplication(options(request)),
      /did not match/,
    );
  }
  const request = requestFor(async (path) =>
    path === "/career-os/connection"
      ? connection
      : path === "/profiles"
        ? profiles
        : { ...app, id: "another-app", pulled: "again" },
  );
  const known = {
    ...parseItem(),
    pulled: { application_id: "app-a", pulled_at: when, changed: true },
  };
  await assert.rejects(
    pullHubRubyApplication({ ...options(request), item: known }),
    /did not match/,
  );
});

test("update status, origin and only-sign-in disconnection errors remain actionable", () => {
  assert.equal(
    parseHubRubyStatus({ changed: true, pulled_at: when, updated_at: when })
      .changed,
    true,
  );
  assert.throws(() => parseHubRubyStatus({ changed: true, pulled_at: null }));
  assert.throws(() =>
    parseApplication({
      ...app,
      career_os: { ...app.career_os, application_id: "bad/path" },
    }),
  );
  assert.match(
    hubRubyError('409: {"code":"only_sign_in_method"}'),
    /Add another sign-in/,
  );
  assert.equal(hubRubyError("Connection timed out"), "Connection timed out");
});
