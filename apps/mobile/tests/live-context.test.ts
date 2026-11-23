import test from "node:test";
import assert from "node:assert/strict";
import {
  command,
  emptyLive,
  event,
  languageAcknowledgement,
} from "../src/liveState.ts";

test("language acknowledgement stays correlated across relay receipts and unsolicited changes", () => {
  const requestId = "language-request";
  const receipt = {
    type: "companion.command.result",
    command_id: requestId,
    result: "executed",
  };
  const rejection = {
    type: "session.language",
    request_id: requestId,
    spoken_language: "en",
    error: "Language temporarily unavailable",
  };
  for (const order of [
    [receipt, rejection],
    [rejection, receipt],
  ]) {
    const results = order
      .map((message) => languageAcknowledgement(message, requestId))
      .filter(Boolean);
    assert.deepEqual(results, [{ error: rejection.error }]);
  }
  assert.equal(languageAcknowledgement(rejection, "newer-request"), null);
  assert.equal(languageAcknowledgement(rejection, null), null);
  assert.equal(
    languageAcknowledgement(
      { type: "session.language", spoken_language: "es" },
      null,
    ),
    null,
  );
  assert.deepEqual(
    languageAcknowledgement({ ...rejection, error: undefined }, requestId),
    { error: null },
  );
});
test("private context notices survive snapshot replay and stale updates cannot replace them", () => {
  const state = event(emptyLive(), {
    type: "companion.snapshot",
    seq: 8,
    messages: [
      {
        type: "status",
        seq: 7,
        service: "memory",
        message: "Using private brief version 3",
      },
    ],
  });
  assert.equal(state.memory, "Using private brief version 3");
  assert.equal(
    event(state, {
      type: "status",
      seq: 6,
      service: "memory",
      message: "Old version",
    }).memory,
    state.memory,
  );
});

test("live language follows acknowledged events and survives folded reconnect snapshots", () => {
  const current = event(emptyLive(), {
    type: "session.language",
    seq: 4,
    spoken_language: "vi",
  });
  assert.equal(current.spokenLanguage, "vi");
  const stale = event(current, {
    type: "session.language",
    seq: 3,
    spoken_language: "en",
  });
  assert.equal(stale.spokenLanguage, "vi");
  const reconnected = event(current, {
    type: "companion.snapshot",
    seq: 9,
    messages: [
      { type: "session.language", seq: 8, spoken_language: "ar" },
      {
        type: "question.detected",
        seq: 5,
        question_id: "q",
        text: "Earlier question",
      },
    ],
  });
  assert.equal(reconnected.spokenLanguage, "ar");
  assert.equal(reconnected.items[0].question, "Earlier question");
  assert.throws(
    () =>
      event(reconnected, {
        type: "session.language",
        seq: 10,
        spoken_language: {},
      }),
    /Invalid session language/,
  );
  assert.throws(
    () =>
      event(reconnected, {
        type: "session.language",
        seq: 10,
        spoken_language: "English",
      }),
    /Invalid session language/,
  );
});

test("ready messages advertise live switching without guessing older server support", () => {
  assert.equal(
    event(emptyLive(), { type: "companion.ready" }).languageSwitching,
    false,
  );
  const ready = event(emptyLive(), {
    type: "companion.ready",
    spoken_language: "es",
    language_switching: true,
  });
  assert.equal(ready.spokenLanguage, "es");
  assert.equal(ready.languageSwitching, true);
  const snapshot = event(ready, {
    type: "companion.snapshot",
    seq: 1,
    messages: [],
  });
  assert.equal(snapshot.spokenLanguage, "es");
  assert.equal(snapshot.languageSwitching, true);
  const resumed = event(snapshot, {
    type: "session.ready",
    seq: 2,
    spoken_language: "ar",
    language_switching: true,
  });
  assert.equal(resumed.spokenLanguage, "ar");
  assert.throws(
    () =>
      event(resumed, { type: "companion.ready", language_switching: "yes" }),
    /Invalid language switching/,
  );
});

test("companion language commands retain retry identity and rejection keeps the authoritative language", () => {
  const request = command("language-request", "set_language", "vi");
  assert.deepEqual(request, {
    type: "companion.command",
    command_id: "language-request",
    kind: "set_language",
    spoken_language: "vi",
  });
  assert.equal(
    JSON.parse(JSON.stringify(request)).command_id,
    "language-request",
  );
  assert.throws(
    () => command("language-request", "set_language", "English"),
    /supported spoken language/,
  );
  const rejected = event(emptyLive(), {
    type: "session.language",
    seq: 3,
    request_id: "language-request",
    spoken_language: "es",
    error: "Language unavailable",
  });
  assert.equal(rejected.spokenLanguage, "es");
  assert.throws(
    () =>
      event(rejected, {
        type: "session.language",
        seq: 4,
        spoken_language: "es",
        error: {},
      }),
    /Invalid language change response/,
  );
});
