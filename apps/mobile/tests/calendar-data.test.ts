import test from "node:test";
import assert from "node:assert/strict";
import {
  clearCalendarPreparation,
  calendarAuthorizationUrl,
  calendarStatusLabel,
  disconnectedCalendar,
  parseCalendarConnections,
  parseCalendarDisconnect,
  parseCalendarSync,
  parseSelectionReceipt,
  selectionBody,
  sameSelection,
  toggleCalendar,
  syncCalendar,
  type CalendarConnection,
  type CalendarProvider,
  type CalendarRequest,
} from "../src/calendarData.ts";

const google: CalendarConnection = {
  provider: "google",
  available: true,
  connected: true,
  status: "active",
  calendars: Array.from({ length: 11 }, (_, i) => ({
    id: `c${i}`,
    name: `Calendar ${i}`,
  })),
  selected_calendars: ["c0"],
  last_sync_at: "2026-11-23T10:00:00Z",
  last_error: null,
};
const outlook: CalendarConnection = {
  ...disconnectedCalendar(google),
  provider: "outlook",
  available: false,
};
const receipt = { event_count: 7, synced_at: "2026-11-23T10:00:00Z" };
const requestFor = (
  handler: (path: string, body?: unknown, method?: string) => Promise<unknown>,
) => handler as CalendarRequest;
function authorization(provider: CalendarProvider) {
  const url = new URL(
    provider === "google"
      ? "https://accounts.google.com/o/oauth2/v2/auth"
      : "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
  );
  url.search = new URLSearchParams({
    client_id: "public-client-id",
    redirect_uri: `https://api.example/v1/calendars/${provider}/callback`,
    response_type: "code",
    scope: "calendar-readonly",
    state: "s".repeat(43),
    code_challenge: "c".repeat(43),
    code_challenge_method: "S256",
  }).toString();
  return url;
}

test("calendar listing retains configured availability, selected calendars and provider errors", () => {
  const parsed = parseCalendarConnections({ items: [outlook, google] });
  assert.deepEqual(
    parsed.map((c) => c.provider),
    ["google", "outlook"],
  );
  assert.deepEqual(parsed[0].selected_calendars, ["c0"]);
  assert.equal(parsed[1].available, false);
  assert.equal(calendarStatusLabel(parsed[1]), "Setup needed");
  assert.equal(
    calendarStatusLabel({ ...google, status: "reauth_required" }),
    "Reconnect needed",
  );
  assert.equal(
    calendarStatusLabel({ ...google, status: "error" }),
    "Sync needs attention",
  );
  assert.equal(
    calendarStatusLabel({ ...outlook, status: "pending" }),
    "Sign-in pending",
  );
});

test("malformed and duplicate provider data cannot silently become an empty calendar list", () => {
  for (const items of [
    null,
    [],
    [google, google],
    [{ ...google, provider: "other" }, outlook],
    [{ ...google, available: "yes" }, outlook],
    [{ ...google, last_sync_at: "yesterday" }, outlook],
    [{ ...google, status: "unknown" }, outlook],
    [
      {
        ...google,
        calendars: [
          { id: "a", name: "A" },
          { id: "a", name: "Again" },
        ],
      },
      outlook,
    ],
    [
      {
        ...google,
        selected_calendars: Array.from({ length: 11 }, (_, i) => `c${i}`),
      },
      outlook,
    ],
  ]) {
    assert.throws(() => parseCalendarConnections({ items }));
  }
});

test("provider authorization uses the exact HTTPS Google or Microsoft sign-in page", () => {
  for (const provider of ["google", "outlook"] as const) {
    const url = authorization(provider);
    assert.equal(
      calendarAuthorizationUrl(provider, { authorization_url: url.href }),
      url.href,
    );
  }
  const tenant = authorization("outlook");
  tenant.pathname = "/organization.example/oauth2/v2.0/authorize";
  assert.equal(
    new URL(
      calendarAuthorizationUrl("outlook", { authorization_url: tenant.href }),
    ).hostname,
    "login.microsoftonline.com",
  );
});

test("provider redirects reject lookalikes, credentials, alternate ports, fragments and wrong paths", () => {
  for (const mutate of [
    (u: URL) => {
      u.hostname = "accounts.google.com.attacker.example";
    },
    (u: URL) => {
      u.hostname = "accounts.google.com.";
    },
    (u: URL) => {
      u.username = "accounts.google.com";
      u.hostname = "evil.example";
    },
    (u: URL) => {
      u.protocol = "http:";
    },
    (u: URL) => {
      u.port = "8443";
    },
    (u: URL) => {
      u.hash = "token";
    },
    (u: URL) => {
      u.pathname = "/redirect";
    },
  ]) {
    const url = authorization("google");
    mutate(url);
    assert.throws(() =>
      calendarAuthorizationUrl("google", { authorization_url: url.href }),
    );
  }
  assert.throws(() =>
    calendarAuthorizationUrl("google", {
      authorization_url: authorization("outlook").href,
    }),
  );
  assert.throws(() =>
    calendarAuthorizationUrl("outlook", {
      authorization_url: "intent://example",
    }),
  );
});

test("authorization requires one-use state and an unambiguous PKCE code flow", () => {
  for (const mutate of [
    (u: URL) => {
      u.searchParams.delete("state");
    },
    (u: URL) => {
      u.searchParams.append("state", "duplicate".repeat(5));
    },
    (u: URL) => {
      u.searchParams.set("state", "short");
    },
    (u: URL) => {
      u.searchParams.set("response_type", "token");
    },
    (u: URL) => {
      u.searchParams.set("code_challenge_method", "plain");
    },
    (u: URL) => {
      u.searchParams.set("code_challenge", "short");
    },
  ]) {
    const url = authorization("google");
    mutate(url);
    assert.throws(() =>
      calendarAuthorizationUrl("google", { authorization_url: url.href }),
    );
  }
});

test("selection is limited to ten authorized calendars and may explicitly clear every calendar", () => {
  const ten = google.calendars.slice(0, 10).map((c) => c.id);
  assert.deepEqual(selectionBody(google, ["c0", "c0"]), {
    calendar_ids: ["c0"],
  });
  assert.deepEqual(selectionBody(google, []), { calendar_ids: [] });
  assert.throws(() => toggleCalendar(google, ten, "c10"), /10/);
  assert.equal(toggleCalendar(google, ten, "c0").length, 9);
  assert.throws(() => selectionBody(google, ["another-account"]));
  assert.throws(() => toggleCalendar(google, [], "missing"));
  assert.equal(sameSelection(["c1", "c0"], ["c0", "c1"]), true);
  assert.equal(sameSelection(["c0", "c0"], ["c0", "c1"]), false);
  assert.deepEqual(
    parseSelectionReceipt({ selected_calendars: ["c1", "c0"] }, ["c0", "c1"]),
    ["c1", "c0"],
  );
  assert.throws(() =>
    parseSelectionReceipt({ selected_calendars: ["c0"] }, ["c1"]),
  );
});

test("save and sync confirms committed selection before making the provider sync request", async () => {
  const calls: { path: string; body: unknown; method: string | undefined }[] =
    [];
  let selected: string[] | undefined;
  const result = await syncCalendar({
    connection: google,
    selected: ["c1"],
    signal: new AbortController().signal,
    request: requestFor(async (path, body, method) => {
      calls.push({ path, body, method });
      if (path.endsWith("/selection")) return { selected_calendars: ["c1"] };
      assert.deepEqual(selected, ["c1"]);
      return receipt;
    }),
    onSelectionSaved: (ids) => {
      selected = ids;
    },
  });
  assert.deepEqual(result, receipt);
  assert.deepEqual(calls, [
    {
      path: "/calendars/google/selection",
      body: { calendar_ids: ["c1"] },
      method: "PATCH",
    },
    { path: "/calendars/google/sync", body: {}, method: "POST" },
  ]);
});

test("a throttled sync reports that selection was saved and retains that acknowledgement", async () => {
  let committed = false;
  await assert.rejects(
    syncCalendar({
      connection: google,
      selected: [],
      signal: new AbortController().signal,
      request: requestFor(async (path) => {
        if (path.endsWith("/selection")) return { selected_calendars: [] };
        throw new Error(
          "Calendar was recently refreshed; try again in five minutes",
        );
      }),
      onSelectionSaved: () => {
        committed = true;
      },
    }),
    /Calendar selection saved.*five minutes/,
  );
  assert.equal(committed, true);
});

test("sync alone preserves unsaved selection and malformed selection receipts stop before sync", async () => {
  const calls: string[] = [];
  await syncCalendar({
    connection: google,
    signal: new AbortController().signal,
    request: requestFor(async (path) => {
      calls.push(path);
      return receipt;
    }),
    onSelectionSaved: () => assert.fail("No selection write requested"),
  });
  assert.deepEqual(calls, ["/calendars/google/sync"]);
  await assert.rejects(
    syncCalendar({
      connection: google,
      selected: ["c1"],
      signal: new AbortController().signal,
      request: requestFor(async (path) => {
        assert.ok(path.endsWith("/selection"));
        return { selected_calendars: [] };
      }),
      onSelectionSaved: () =>
        assert.fail("Invalid acknowledgement cannot publish"),
    }),
    /did not confirm/,
  );
});

test("profile teardown between selection and sync cannot publish into the next profile", async () => {
  const controller = new AbortController();
  await assert.rejects(
    syncCalendar({
      connection: google,
      selected: ["c1"],
      signal: controller.signal,
      request: requestFor(async (path) => {
        assert.ok(path.endsWith("/selection"));
        controller.abort();
        return { selected_calendars: ["c1"] };
      }),
      onSelectionSaved: () =>
        assert.fail("An aborted profile must not publish"),
    }),
    { name: "AbortError" },
  );
});

test("disconnect clears every cached calendar field while preserving deployment availability", () => {
  assert.deepEqual(disconnectedCalendar({ ...google, last_error: "Expired" }), {
    ...google,
    connected: false,
    status: "disconnected",
    calendars: [],
    selected_calendars: [],
    last_sync_at: null,
    last_error: null,
  });
  assert.equal(
    parseCalendarDisconnect({
      disconnected: true,
      provider_revoked: false,
      provider_consent_removal_required: true,
    }).provider_consent_removal_required,
    true,
  );
  assert.throws(() =>
    parseCalendarDisconnect({
      disconnected: false,
      provider_revoked: false,
      provider_consent_removal_required: true,
    }),
  );
  assert.throws(() =>
    parseCalendarDisconnect({
      disconnected: true,
      provider_revoked: "false",
      provider_consent_removal_required: true,
    }),
  );
  assert.deepEqual(parseCalendarSync(receipt), receipt);
  assert.throws(() => parseCalendarSync({ ...receipt, event_count: -1 }));
  assert.throws(() => parseCalendarSync({ ...receipt, synced_at: "later" }));
});

test("calendar disconnect removes only that provider's cached meetings and preserves call/application context", () => {
  const meeting = (account_id: string) => ({
    meeting: {
      account_id,
      calendar_id: "calendar",
      instance_id: "event",
      title: "Meeting",
      start: "2026-11-23T10:00:00Z",
      end: "2026-11-23T10:30:00Z",
    },
    candidates: [],
    selected_application_id: "application",
    linked_session_id: "call",
    requires_confirmation: false,
    overlaps: false,
    unfinished_actions: ["Follow up"],
    previous: [],
  });
  const data = {
    items: [meeting("g"), meeting("m")],
    connections: [
      { connection_id: "g", provider: "google", status: "active" },
      { connection_id: "m", provider: "outlook", status: "active" },
    ],
    applications: [
      {
        application_id: "application",
        company: "Company",
        job_title: "Engineer",
      },
    ],
    saved_sessions: [
      { id: "call", title: "Prior call", started_at: "2026-11-22T10:00:00Z" },
    ],
  };
  const disconnected = clearCalendarPreparation(data, "google", true);
  assert.deepEqual(
    disconnected.items.map((i) => i.meeting.account_id),
    ["m"],
  );
  assert.deepEqual(
    disconnected.connections.map((c) => c.provider),
    ["outlook"],
  );
  assert.equal(disconnected.applications, data.applications);
  assert.equal(disconnected.saved_sessions, data.saved_sessions);
  assert.equal(data.items.length, 2);
  const changedSelection = clearCalendarPreparation(data, "google");
  assert.equal(changedSelection.connections, data.connections);
  assert.deepEqual(
    changedSelection.items.map((i) => i.meeting.account_id),
    ["m"],
  );
});
