import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { Practice } from "../src/Practice";
import { ipc } from "./ipc";
const fixture = () => ({ id: "p1", status: "answering", setup: { topic: "Engineering" }, questions: [{ id: "q1", text: "Describe a tradeoff.", kind: "experience", attempts: [], assistance: [], saved_for_later: false }], current_question_id: "q1", elapsed_seconds: 0, sources: [], spoken_language: "en", voice_available: false, speech_available: false });
function setup() { ipc.api("GET", /^\/practice\/sessions$/, () => ({ sessions: [] })); ipc.api("GET", /^\/ask\/context-options/, () => ({ options: [] })); }
it("requires transcript consent and keeps text practice usable without voice", async () => {
  setup(); ipc.api("POST", /^\/practice\/sessions$/, () => fixture());
  render(<Practice active running={false} />);
  expect(screen.getByRole("button", { name: "Start practice" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /Save practice transcripts/ })); fireEvent.click(screen.getByRole("button", { name: "Start practice" }));
  expect(await screen.findByText("Describe a tradeoff.", { selector: "h2" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Answer with microphone" })).not.toBeInTheDocument();
  expect(screen.getByText(/Text practice/)).toBeVisible();
  expect(screen.getByLabelText(/Your answer/)).toBeEnabled();
  expect(ipc.calls.some(c => c.cmd === "start_session")).toBe(false);
});
it("retains an answer and command ID after a network failure", async () => {
  setup(); ipc.api("POST", /^\/practice\/sessions$/, () => fixture()); const ids: string[] = [];
  ipc.api("POST", /^\/practice\/sessions\/p1\/actions$/, (_p, body) => { ids.push((body as {request_id:string}).request_id); throw new Error("Disconnected"); });
  render(<Practice active running={false} />); fireEvent.click(screen.getByRole("checkbox", { name: /Save practice transcripts/ })); fireEvent.click(screen.getByRole("button", { name: "Start practice" }));
  const answer = await screen.findByLabelText(/Your answer/); fireEvent.change(answer, { target: { value: "We traded latency for consistency." } });
  fireEvent.click(screen.getByRole("button", { name: /submit answer/ })); await screen.findByText(/Disconnected/); await waitFor(() => expect(screen.getByRole("button", { name: /submit answer/ })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: /submit answer/ })); await waitFor(() => expect(ids).toHaveLength(2)); expect(ids[0]).toBe(ids[1]); expect(answer).toHaveValue("We traded latency for consistency.");
});


it("ignores a late start after selecting a saved practice", async () => {
  setup(); const saved = { ...fixture(), id: "saved", status: "paused", setup: { topic: "Saved practice" } };
  let resolveStart!: (value: unknown) => void;
  ipc.api("GET", /^\/practice\/sessions$/, () => ({ sessions: [saved] }));
  ipc.api("GET", /^\/practice\/sessions\/saved$/, () => saved);
  ipc.api("POST", /^\/practice\/sessions$/, () => new Promise(resolve => { resolveStart = resolve; }));
  render(<Practice active running={false} />);
  await screen.findByText(/Saved practice/, { selector: "option" });
  fireEvent.click(screen.getByRole("checkbox", { name: /Save practice transcripts/ }));
  fireEvent.click(screen.getByRole("button", { name: "Start practice" }));
  await waitFor(() => expect(resolveStart).toBeDefined());
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "saved" } });
  await waitFor(() => expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("saved"));
  await act(async () => resolveStart(fixture()));
  expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("saved");
  expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled();
});

it("ignores earlier history and source responses after another selection", async () => {
  setup(); const first = { ...fixture(), status: "paused", questions: [{ ...fixture().questions[0], source: { kind: "transcript", id: "original" } }] };
  const second = { ...fixture(), id: "p2", status: "paused", setup: { topic: "Second session" } };
  let resolveFirst!: (value: unknown) => void, resolveSource!: (value: unknown) => void;
  ipc.api("GET", /^\/practice\/sessions$/, () => ({ sessions: [first, second] }));
  ipc.api("GET", /^\/practice\/sessions\/p1$/, () => new Promise(resolve => { resolveFirst = resolve; }));
  ipc.api("GET", /^\/practice\/sessions\/p2$/, () => second);
  ipc.api("GET", /^\/ask\/sources\/transcript\/original$/, () => new Promise(resolve => { resolveSource = resolve; }));
  render(<Practice active running={false} />);
  await screen.findByText(/Second session/, { selector: "option" });
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "p1" } });
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "p2" } });
  await waitFor(() => expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("p2"));
  await act(async () => resolveFirst(first));
  expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("p2");
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "p1" } });
  await act(async () => resolveFirst(first));
  fireEvent.click(await screen.findByRole("button", { name: "View original transcript" }));
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "p2" } });
  await waitFor(() => expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("p2"));
  await act(async () => resolveSource({ content: "Old source excerpt" }));
  expect(screen.queryByText("Old source excerpt")).not.toBeInTheDocument();
});


it("does not let an old answer response replace the selected session or its draft", async () => {
  setup(); const first = fixture(), second = { ...fixture(), id: "p2", setup: { topic: "Second session" } };
  let resolveAnswer!: (value: unknown) => void;
  ipc.api("GET", /^\/practice\/sessions$/, () => ({ sessions: [first, second] }));
  ipc.api("GET", /^\/practice\/sessions\/p1$/, () => first);
  ipc.api("GET", /^\/practice\/sessions\/p2$/, () => second);
  ipc.api("POST", /^\/practice\/sessions\/p1\/actions$/, (_path, body) => (body as { action: string }).action === "answer" ? new Promise(resolve => { resolveAnswer = resolve; }) : { ...first, status: "paused" });
  render(<Practice active running={false} />);
  await screen.findByText(/Second session/, { selector: "option" });
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "p1" } });
  fireEvent.change(await screen.findByLabelText(/Your answer/), { target: { value: "First answer" } });
  fireEvent.click(screen.getByRole("button", { name: /submit answer/ }));
  await waitFor(() => expect(resolveAnswer).toBeDefined());
  fireEvent.change(screen.getByLabelText("Practice history", { selector: "select" }), { target: { value: "p2" } });
  await waitFor(() => expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("p2"));
  fireEvent.change(screen.getByLabelText(/Your answer/), { target: { value: "New session draft" } });
  await act(async () => resolveAnswer({ ...first, status: "processing" }));
  expect(screen.getByLabelText("Practice history", { selector: "select" })).toHaveValue("p2");
  expect(screen.getByLabelText(/Your answer/)).toHaveValue("New session draft");
});


it("pauses a late start when a real call has already taken priority", async () => {
  setup(); let resolveStart!: (value: unknown) => void;
  ipc.api("POST", /^\/practice\/sessions$/, () => new Promise(resolve => { resolveStart = resolve; }));
  ipc.api("POST", /^\/practice\/sessions\/p1\/actions$/, () => ({ ...fixture(), status: "paused" }));
  const view = render(<Practice active running={false} />);
  fireEvent.click(screen.getByRole("checkbox", { name: /Save practice transcripts/ }));
  fireEvent.click(screen.getByRole("button", { name: "Start practice" }));
  await waitFor(() => expect(resolveStart).toBeDefined());
  view.rerender(<Practice active={false} running />);
  await act(async () => resolveStart(fixture()));
  expect(ipc.apiCalls("POST").find(call => call.path.endsWith("/p1/actions"))?.body).toMatchObject({ action: "pause" });
  expect(await screen.findByRole("button", { name: "Resume" })).toBeDisabled();
});

it("explains retry progress and remaining gaps without hiding coaching qualifications", async () => {
  setup();
  const saved = { ...fixture(), status:"finished", questions:[{...fixture().questions[0], attempts:[{id:"a2",answer:"I measured the outcome.",assistance:[],feedback:{rubric_version:"v1",dimensions:{evidence:3},strengths:[],improvements:[],excerpts:[],uncertainty:[],suggested_wording:""},comparison:{comparable:false,note:"Coaching conditions differ.",summary:"Your evidence is more concrete.",dimensions:{evidence:{previous:1,current:3}},improvements:["Added a measured outcome."],remaining_gaps:["Explain the alternative you rejected."],recurring_gaps:[],evidence:{previous:["I improved it."],current:["I measured the outcome."]}}}]}]};
  ipc.api("GET", /^\/practice\/sessions$/, () => ({sessions:[saved]}));
  ipc.api("GET", /^\/practice\/sessions\/p1$/, () => saved);
  render(<Practice active running={false}/>);
  await screen.findByText(/Engineering/,{selector:"option"});
  fireEvent.change(screen.getByLabelText("Practice history",{selector:"select"}),{target:{value:"p1"}});
  expect(await screen.findByText("Added a measured outcome.")).toBeVisible();
  expect(screen.getByText("Explain the alternative you rejected.")).toBeVisible();
  expect(screen.getByText("Coaching conditions differ.")).toBeVisible();
});
