import React, { useEffect, useRef, useState } from "react";
import { Alert, View } from "react-native";
import type { Api } from "./api";
import { ApiError } from "./api";
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
  Segments,
  Txt,
} from "./ui";
import { useWorkspaceRequests } from "./useWorkspaceRequests";
import { pickWorkspaceFile } from "./workspaceFiles";
import { FloatingNotice } from "./FloatingNotice";
import {
  employerBody,
  list,
  localDay,
  parseDocument,
  parseEmployer,
  parseEmployers,
  parseLog,
  parseLogs,
  parseMemory,
  parseProposal,
  parseWorkFile,
  record,
  validDay,
  type Employer,
  type MemoryItem,
  type Proposal,
  type WorkDocument,
  type WorkFile,
  type WorkLog,
} from "./workspaceData";

const strong = { fontSize: 17, lineHeight: 24, fontWeight: "600" as const };
const pathOf = (company: Employer) =>
  `/employers/${encodeURIComponent(company.id)}`;
function confirm(title: string, detail: string, action: () => void) {
  Alert.alert(title, detail, [
    { text: "Cancel", style: "cancel" },
    { text: "Delete", style: "destructive", onPress: action },
  ]);
}
export function WorkContext({
  api,
  visible = true,
  onAsk,
}: {
  api: Api;
  visible?: boolean;
  onAsk?: (id: string) => void;
}) {
  const work = useWorkspaceRequests(api);
  const [companies, setCompanies] = useState<Employer[]>([]);
  const [selected, setSelected] = useState<Employer | null>(null);
  const [adding, setAdding] = useState(false),
    [query, setQuery] = useState("");
  const [formState, setFormState] = useState({ dirty: false, busy: false });
  function leaveForm(action: () => void) {
    if (formState.busy) return;
    if (!adding || !formState.dirty) {
      action();
      return;
    }
    Alert.alert(
      "Discard new company?",
      "The details you entered have not been saved.",
      [
        { text: "Keep editing", style: "cancel" },
        {
          text: "Discard",
          style: "destructive",
          onPress: () => {
            setFormState({ dirty: false, busy: false });
            action();
          },
        },
      ],
    );
  }
  async function refresh(fresh = false) {
    const result = await work.read(async (request, signal) => {
      await api.syncDomain("work", signal, fresh);
      return parseEmployers(await request("/employers"));
    }, fresh);
    if (result) setCompanies(result);
  }
  useEffect(() => {
    if (visible && work.foreground) void refresh();
    else work.cancelRead();
  }, [api, visible, work.foreground]);
  if (selected)
    return (
      <CompanyWorkspace
        key={selected.id}
        api={api}
        company={selected}
        visible={visible}
        onAsk={onAsk}
        onBack={() => {
          setSelected(null);
          void refresh();
        }}
        onChanged={setSelected}
      />
    );
  return (
    <Screen>
      <ScreenTitle
        eyebrow="Work context"
        title="Your companies"
        subtitle="Keep the facts, documents and daily progress your answers draw on."
        action={
          <IconButton
            label="Refresh companies"
            icon="refresh"
            disabled={work.loading}
            onPress={() => void refresh(true)}
          />
        }
      />
      {work.error ? (
        <Notice
          tone="error"
          action={
            <Button
              label="Retry"
              secondary
              compact
              onPress={() => void refresh()}
            />
          }
        >
          {work.error}
        </Notice>
      ) : null}
      <Row>
        <View style={{ flex: 1, minWidth: 180 }}>
          <Input
            placeholder="Find a company"
            value={query}
            onChangeText={setQuery}
          />
        </View>
        <Button
          label={adding ? "Close form" : "Add company"}
          secondary
          compact
          disabled={formState.busy}
          onPress={() => leaveForm(() => setAdding((v) => !v))}
        />
      </Row>
      {adding ? (
        <CompanyForm
          api={api}
          onStateChange={setFormState}
          onSaved={(company) => {
            setFormState({ dirty: false, busy: false });
            setAdding(false);
            setSelected(company);
          }}
        />
      ) : null}
      {work.loading && !companies.length ? (
        <Loading label="Loading companies" />
      ) : null}
      {!work.loading && !companies.length && !adding ? (
        <EmptyState
          icon="work"
          title="Add your work context"
          description="Create a company to save work logs and documents for meetings and Ask."
          action={
            <Button label="Add company" onPress={() => setAdding(true)} />
          }
        />
      ) : null}
      {companies
        .filter((c) =>
          `${c.name} ${c.title} ${c.team}`
            .toLocaleLowerCase()
            .includes(query.toLocaleLowerCase()),
        )
        .map((company) => (
          <ActionCard
            key={company.id}
            label={`Open ${company.name}`}
            onPress={() =>
              leaveForm(() => {
                setAdding(false);
                setSelected(company);
              })
            }
          >
            <Txt style={strong}>{company.name}</Txt>
            <Txt muted>
              {[company.title, company.team].filter(Boolean).join(" · ") ||
                "Company workspace"}
            </Txt>
            {company.status === "archived" ? <Badge label="Archived" /> : null}
          </ActionCard>
        ))}
    </Screen>
  );
}

function CompanyForm({
  api,
  company,
  onSaved,
  onStateChange,
}: {
  api: Api;
  company?: Employer;
  onSaved: (company: Employer) => void;
  onStateChange?: (state: { dirty: boolean; busy: boolean }) => void;
}) {
  const work = useWorkspaceRequests(api);
  const [fields, setFields] = useState({
    name: company?.name ?? "",
    title: company?.title ?? "",
    team: company?.team ?? "",
    started: company?.started_on ?? "",
    ended: company?.ended_on ?? "",
  });
  const baseline = useRef(fields);
  const dirty = JSON.stringify(fields) !== JSON.stringify(baseline.current);
  useEffect(() => {
    onStateChange?.({ dirty, busy: work.busy });
  }, [dirty, work.busy, onStateChange]);
  const change = (key: keyof typeof fields) => (value: string) =>
    setFields((old) => ({ ...old, [key]: value }));
  async function save() {
    const saved = await work.mutate(async (request) =>
      parseEmployer(
        await request(
          company ? pathOf(company) : "/employers",
          employerBody(fields),
          company ? "PATCH" : "POST",
        ),
      ),
    );
    if (saved) {
      baseline.current = fields;
      onStateChange?.({ dirty: false, busy: false });
      onSaved(saved.value);
    }
  }
  return (
    <Card>
      <Txt style={strong}>{company ? "Company details" : "New company"}</Txt>
      <Field label="Company name">
        <Input
          editable={!work.busy}
          value={fields.name}
          onChangeText={change("name")}
          placeholder="Company"
          maxLength={200}
        />
      </Field>
      <Field label="Your role">
        <Input
          editable={!work.busy}
          value={fields.title}
          onChangeText={change("title")}
          placeholder="Role"
          maxLength={200}
        />
      </Field>
      <Field label="Team">
        <Input
          editable={!work.busy}
          value={fields.team}
          onChangeText={change("team")}
          placeholder="Team"
          maxLength={200}
        />
      </Field>
      <Field label="Start date (optional)">
        <Input
          editable={!work.busy}
          value={fields.started}
          onChangeText={change("started")}
          placeholder="YYYY-MM-DD"
          maxLength={10}
        />
      </Field>
      <Field label="End date (optional)">
        <Input
          editable={!work.busy}
          value={fields.ended}
          onChangeText={change("ended")}
          placeholder="YYYY-MM-DD"
          maxLength={10}
        />
      </Field>
      {work.error ? <Notice tone="error">{work.error}</Notice> : null}
      <Button
        label="Save company"
        loading={work.busy}
        disabled={work.busy || !fields.name.trim()}
        onPress={() => void save()}
      />
    </Card>
  );
}

function CompanyWorkspace({
  api,
  company,
  visible,
  onBack,
  onChanged,
  onAsk,
}: {
  api: Api;
  company: Employer;
  visible: boolean;
  onBack: () => void;
  onChanged: (company: Employer) => void;
  onAsk?: (id: string) => void;
}) {
  const work = useWorkspaceRequests(api),
    base = pathOf(company);
  const [pane, setPane] = useState("logs"),
    [logs, setLogs] = useState<WorkLog[]>([]),
    [before, setBefore] = useState<string | null>(null);
  const [day, setDay] = useState(localDay),
    [dateInput, setDateInput] = useState(localDay),
    [logText, setLogText] = useState("");
  const [documents, setDocuments] = useState<WorkFile[]>([]),
    [memory, setMemory] = useState<MemoryItem[]>([]),
    [proposals, setProposals] = useState<Proposal[]>([]);
  const [document, setDocument] = useState<WorkDocument | null>(null),
    [editing, setEditing] = useState<MemoryItem | null>(null);
  const [docName, setDocName] = useState(""),
    [docText, setDocText] = useState(""),
    [confirmName, setConfirmName] = useState("");
  const [message, setMessage] = useState("");
  const [conflictLog, setConflictLog] = useState<WorkLog | null>(null);
  const [formState, setFormState] = useState({ dirty: false, busy: false });
  const drafts = useRef(new Map<string, string>()),
    draftRevisions = useRef(new Map<string, number | undefined>()),
    initial = useRef(true);
  const savedLog = logs.find((log) => log.day === day);
  const editor = useRef({
    day,
    text: logText,
    savedText: savedLog?.text ?? "",
  });
  editor.current = { day, text: logText, savedText: savedLog?.text ?? "" };
  const dirty =
    formState.dirty ||
    logText !== (savedLog?.text ?? "") ||
    drafts.current.size > 0 ||
    !!docName ||
    !!docText ||
    !!editing;
  async function refresh(more = false, fresh = false) {
    if (more) {
      const page = await work.read(async (request) =>
        parseLogs(
          await request(
            `${base}/logs${before ? `?before=${encodeURIComponent(before)}` : ""}`,
          ),
        ),
      );
      if (page) {
        setLogs((old) =>
          [
            ...new Map(
              [...old, ...page.logs].map((log) => [log.day, log]),
            ).values(),
          ].sort((a, b) => b.day.localeCompare(a.day)),
        );
        setBefore(page.next);
      }
      return;
    }
    const baseline = savedLog?.text ?? "";
    const result = await work.read(async (request, signal) => {
      await api.syncDomain("work", signal, fresh);
      const [
        rawLogs,
        rawDocs,
        rawMemory,
        rawProposals,
        activeLog,
        activeDocument,
      ] = await Promise.all([
        request(`${base}/logs`),
        request(`${base}/documents`),
        request(`${base}/memory`),
        request(`/proposals?employer_id=${encodeURIComponent(company.id)}`),
        request(`${base}/logs/${day}`)
          .then(parseLog)
          .catch((e) => {
            if (e instanceof ApiError && e.status === 404) return null;
            throw e;
          }),
        document
          ? request(`${base}/documents/${encodeURIComponent(document.id)}`)
              .then(parseDocument)
              .catch((e) => {
                if (e instanceof ApiError && e.status === 404) return null;
                throw e;
              })
          : null,
      ]);
      return {
        ...parseLogs(rawLogs),
        activeLog,
        activeDocument,
        documents: list(record(rawDocs).documents, parseWorkFile),
        memory: list(record(rawMemory).memory, parseMemory),
        proposals: list(record(rawProposals).proposals, parseProposal),
      };
    }, fresh);
    if (!result) return;
    setLogs((old) =>
      [
        ...new Map(
          [...result.logs, ...(result.activeLog ? [result.activeLog] : [])]
            .filter((log) => log.day !== day || result.activeLog)
            .map((log) => [log.day, log]),
        ).values(),
      ].sort((a, b) => b.day.localeCompare(a.day)),
    );
    setBefore(result.next);
    setDocuments(result.documents);
    setMemory(result.memory);
    setProposals(result.proposals);
    setDocument((old) =>
      old && old.id === document?.id ? result.activeDocument : old,
    );
    setLogText((current) =>
      editor.current.day === day && current === baseline
        ? (result.activeLog?.text ?? "")
        : current,
    );
    initial.current = false;
  }
  const processing =
    logs.some((log) => ["extracting", "pending"].includes(log.status)) ||
    documents.some((doc) =>
      ["ingesting", "indexing", "pending"].includes(doc.status),
    );
  const pollStarted = useRef(0),
    pollFailures = useRef(0);
  async function refreshProcessing() {
    const result = await work.read(async (request) => {
      const status = record(await request(`${base}/status`));
      const states = (value: unknown) =>
        list(value, (item) => {
          const state = record(item);
          if (typeof state.id !== "string" || typeof state.status !== "string")
            throw new Error("Invalid processing status.");
          return { id: state.id, status: state.status };
        });
      const logStates = states(status.work_logs),
        docStates = states(status.documents);
      const changedLogs = logs.filter((log) =>
        logStates.some(
          (state) => state.id === log.id && state.status !== log.status,
        ),
      );
      const docsChanged = documents.some((doc) =>
        docStates.some(
          (state) => state.id === doc.id && state.status !== doc.status,
        ),
      );
      if (!changedLogs.length && !docsChanged)
        return {
          changedLogs: [] as WorkLog[],
          documents: null,
          memory: null,
          proposals: null,
          activeDocument: null,
        };
      const [changed, rawDocs, rawMemory, rawProposals, activeDocument] =
        await Promise.all([
          Promise.all(
            changedLogs.map((log) =>
              request(
                `${base}/logs/${log.day}`,
                undefined,
                "GET",
                "reload",
              ).then(parseLog),
            ),
          ),
          docsChanged
            ? request(`${base}/documents`, undefined, "GET", "reload")
            : null,
          request(`${base}/memory`, undefined, "GET", "reload"),
          request(
            `/proposals?employer_id=${encodeURIComponent(company.id)}`,
            undefined,
            "GET",
            "reload",
          ),
          docsChanged && document
            ? request(
                `${base}/documents/${encodeURIComponent(document.id)}`,
                undefined,
                "GET",
                "reload",
              ).then(parseDocument)
            : null,
        ]);
      return {
        changedLogs: changed,
        documents: rawDocs
          ? list(record(rawDocs).documents, parseWorkFile)
          : null,
        memory: list(record(rawMemory).memory, parseMemory),
        proposals: list(record(rawProposals).proposals, parseProposal),
        activeDocument,
      };
    });
    if (!result) {
      pollFailures.current++;
      return;
    }
    pollFailures.current = 0;
    if (result.changedLogs.length) {
      setLogs((old) =>
        old.map(
          (log) =>
            result.changedLogs.find((changed) => changed.id === log.id) ?? log,
        ),
      );
      const active = result.changedLogs.find(
        (log) => log.day === editor.current.day,
      );
      if (active)
        setLogText((value) =>
          value === editor.current.savedText ? active.text : value,
        );
    }
    if (result.documents) setDocuments(result.documents);
    if (result.memory) setMemory(result.memory);
    if (result.proposals) setProposals(result.proposals);
    if (result.activeDocument)
      setDocument((old) =>
        old?.id === result.activeDocument!.id ? result.activeDocument : old,
      );
  }
  useEffect(() => {
    if (visible && work.foreground) void refresh();
    else work.cancelRead();
  }, [api, visible, work.foreground]);
  useEffect(() => {
    if (!processing) {
      pollStarted.current = 0;
      pollFailures.current = 0;
      return;
    }
    if (!visible || !work.foreground || work.busy || work.loading) return;
    if (!pollStarted.current) pollStarted.current = Date.now();
    if (Date.now() - pollStarted.current > 5 * 60_000) return;
    const timer = setTimeout(
      () => void refreshProcessing(),
      Math.min(30_000, 5000 * 2 ** Math.min(pollFailures.current, 3)),
    );
    return () => clearTimeout(timer);
  }, [
    visible,
    work.foreground,
    processing,
    work.busy,
    work.loading,
    day,
    before,
    document?.id,
    savedLog?.text,
  ]);
  function back() {
    if (!dirty) {
      onBack();
      return;
    }
    Alert.alert(
      "Discard unsaved changes?",
      "Your saved company data stays available.",
      [
        { text: "Keep editing", style: "cancel" },
        { text: "Discard", style: "destructive", onPress: onBack },
      ],
    );
  }
  function useLog(entry: WorkLog | null, nextDay: string) {
    if (work.busy) return;
    work.cancelRead();
    const latest = editor.current;
    if (latest.text !== latest.savedText)
      drafts.current.set(latest.day, latest.text);
    else {
      drafts.current.delete(latest.day);
      draftRevisions.current.delete(latest.day);
    }
    const text = drafts.current.get(nextDay) ?? entry?.text ?? "";
    drafts.current.delete(nextDay);
    setDay(nextDay);
    setDateInput(nextDay);
    setLogText(text);
    setMessage("");
    setConflictLog(null);
  }
  async function openDay() {
    if (!validDay(dateInput)) {
      work.setError("Enter a date as YYYY-MM-DD.");
      return;
    }
    const target = dateInput;
    const result = await work.read(async (request) => {
      try {
        return { entry: parseLog(await request(`${base}/logs/${target}`)) };
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { entry: null };
        throw e;
      }
    });
    if (result) {
      if (result.entry)
        setLogs((old) =>
          [...old.filter((log) => log.day !== target), result.entry!].sort(
            (a, b) => b.day.localeCompare(a.day),
          ),
        );
      useLog(result.entry, target);
    }
  }
  async function saveLog() {
    const expected = draftRevisions.current.has(day)
      ? draftRevisions.current.get(day)
      : savedLog
        ? savedLog.revision
        : -1;
    const result = await work.mutate(async (request) => {
      if (!validDay(day) || !logText.trim() || logText.length > 50000)
        throw new Error("Choose a valid day and enter 1–50,000 characters.");
      try {
        return parseLog(
          await request(
            `${base}/logs/${day}`,
            {
              text: logText,
              ...(expected !== undefined
                ? { expected_revision: expected }
                : {}),
            },
            "PUT",
          ),
        );
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          const latest = await request(
            `${base}/logs/${day}`,
            undefined,
            "GET",
            "reload",
          )
            .then(parseLog)
            .catch(() => null);
          if (latest) setConflictLog(latest);
        }
        throw error;
      }
    });
    if (result) {
      setLogs((old) => [
        result.value,
        ...old.filter((log) => log.day !== result.value.day),
      ]);
      setLogText(result.value.text);
      drafts.current.delete(result.value.day);
      draftRevisions.current.delete(result.value.day);
      setConflictLog(null);
      setMessage("Work log saved.");
    }
  }
  async function upload(attachment = false) {
    const result = await work.mutate(async (_request, signal) => {
      const file = await pickWorkspaceFile(attachment);
      if (!file) return false;
      await api.upload(
        attachment ? `${base}/logs/${day}/attachments` : `${base}/documents`,
        file.bytes,
        file.contentType,
        file.name,
        signal,
        work.profile,
      );
      return true;
    });
    if (result?.value) {
      setMessage(
        attachment
          ? "Attachment added."
          : "Document uploaded. Its content is being indexed.",
      );
      await refresh();
    }
  }
  async function pasteDocument() {
    const result = await work.mutate(async (_request, signal) => {
      if (!docText.trim()) throw new Error("Paste the document text first.");
      const name = docName.trim().replace(/(?:\.md|\.txt)$/i, "");
      if (!name) throw new Error("Give the document a name.");
      return api.upload(
        `${base}/documents`,
        docText,
        "text/markdown",
        `${name}.md`,
        signal,
        work.profile,
      );
    });
    if (result) {
      setDocName("");
      setDocText("");
      setMessage("Document saved. Its content is being indexed.");
      await refresh();
    }
  }
  async function removeFile(file: WorkFile) {
    const result = await work.mutate((request) =>
      request(
        `${base}/documents/${encodeURIComponent(file.id)}`,
        undefined,
        "DELETE",
      ),
    );
    if (result) {
      if (document?.id === file.id) setDocument(null);
      await refresh();
    }
  }
  async function decideProposal(proposal: Proposal, approve: boolean) {
    const result = await work.mutate((request) =>
      request(
        `/proposals/${encodeURIComponent(proposal.id)}/${approve ? "approve" : "reject"}`,
        {},
        "POST",
      ),
    );
    if (result) {
      setProposals((old) => old.filter((item) => item.id !== proposal.id));
      await refresh();
    }
  }
  return (
    <View style={{ flex: 1 }}>
      <Screen>
        <Button
          label="Back to companies"
          secondary
          compact
          disabled={work.busy || formState.busy}
          onPress={back}
        />
        <ScreenTitle
          eyebrow="Work context"
          title={company.name}
          subtitle={[company.title, company.team].filter(Boolean).join(" · ")}
          action={
            <IconButton
              label="Refresh company context"
              icon="refresh"
              disabled={work.loading || work.busy}
              onPress={() => {
                pollStarted.current = 0;
                void refresh(false, true);
              }}
            />
          }
        />
        {onAsk ? (
          <Button
            label="Ask about this company"
            secondary
            onPress={() => onAsk(company.id)}
          />
        ) : null}
        <Segments
          value={pane}
          onChange={setPane}
          options={[
            { value: "logs", label: "Work logs" },
            { value: "documents", label: "Documents" },
            { value: "memory", label: "Memory" },
            { value: "company", label: "Company" },
          ]}
        />
        {work.error ? <Notice tone="error">{work.error}</Notice> : null}
        {work.loading && initial.current ? (
          <Loading label="Loading company context" />
        ) : null}
        {pane === "logs" ? (
          <>
            <Card>
              <Txt style={strong}>Work log · {day}</Txt>
              <Row>
                <View style={{ flex: 1, minWidth: 150 }}>
                  <Input
                    editable={!work.busy}
                    accessibilityLabel="Work log date"
                    placeholder="YYYY-MM-DD"
                    value={dateInput}
                    onChangeText={setDateInput}
                    maxLength={10}
                  />
                </View>
                <Button
                  label="Open date"
                  compact
                  secondary
                  disabled={work.loading || work.busy}
                  onPress={() => void openDay()}
                />
              </Row>
              <Input
                editable={!work.busy}
                accessibilityLabel="Work log"
                placeholder="What did you work on? Decisions, progress, blockers and next steps…"
                value={logText}
                onChangeText={(value) => {
                  if (!draftRevisions.current.has(day))
                    draftRevisions.current.set(
                      day,
                      savedLog ? savedLog.revision : -1,
                    );
                  if (value === (savedLog?.text ?? ""))
                    draftRevisions.current.delete(day);
                  setLogText(value);
                }}
                multiline
                maxLength={50000}
                style={{ minHeight: 160 }}
              />
              {savedLog ? <Badge label={savedLog.status} /> : null}
              {conflictLog?.day === day ? (
                <Card subtle>
                  <Txt style={strong}>This log changed on another device</Txt>
                  <Txt>
                    Your draft is preserved above. Review the latest saved text
                    before choosing which version to keep.
                  </Txt>
                  <Txt selectable>{conflictLog.text}</Txt>
                  <Row>
                    <Button
                      label="Use saved text"
                      secondary
                      compact
                      onPress={() => {
                        setLogText(conflictLog.text);
                        setLogs((old) => [
                          conflictLog,
                          ...old.filter((log) => log.day !== day),
                        ]);
                        drafts.current.delete(day);
                        draftRevisions.current.delete(day);
                        setConflictLog(null);
                        work.setError("");
                      }}
                    />
                    <Button
                      label="Keep my draft"
                      secondary
                      compact
                      onPress={() => {
                        draftRevisions.current.set(day, conflictLog.revision);
                        setLogs((old) => [
                          conflictLog,
                          ...old.filter((log) => log.day !== day),
                        ]);
                        setConflictLog(null);
                        work.setError("");
                      }}
                    />
                  </Row>
                </Card>
              ) : null}
              <Row>
                <Button
                  label={
                    savedLog?.status === "failed"
                      ? "Save and retry"
                      : "Save log"
                  }
                  compact
                  loading={work.busy}
                  disabled={
                    work.busy || !logText.trim() || conflictLog?.day === day
                  }
                  onPress={() => void saveLog()}
                />
                {savedLog ? (
                  <Button
                    label="Add attachment"
                    secondary
                    compact
                    disabled={work.busy || logText !== savedLog.text}
                    onPress={() => void upload(true)}
                  />
                ) : null}
                {savedLog ? (
                  <Button
                    label="Delete log"
                    secondary
                    danger
                    compact
                    disabled={work.busy}
                    onPress={() =>
                      confirm(
                        "Delete this work log?",
                        "Its attachments and extracted facts will also be deleted.",
                        () =>
                          void work
                            .mutate((request) =>
                              request(
                                `${base}/logs/${day}`,
                                undefined,
                                "DELETE",
                              ),
                            )
                            .then((result) => {
                              if (result) {
                                setLogs((old) =>
                                  old.filter((log) => log.day !== day),
                                );
                                setLogText("");
                                drafts.current.delete(day);
                                draftRevisions.current.delete(day);
                                setConflictLog(null);
                                void refresh();
                              }
                            }),
                      )
                    }
                  />
                ) : null}
              </Row>
              {savedLog?.attachments.map((file) => (
                <Row key={file.id}>
                  <Txt style={{ flex: 1 }}>{file.name}</Txt>
                  <Button
                    label="Remove"
                    secondary
                    compact
                    disabled={work.busy}
                    onPress={() =>
                      confirm(
                        "Remove attachment?",
                        file.name,
                        () => void removeFile(file),
                      )
                    }
                  />
                </Row>
              ))}
            </Card>
            {logs.map((log) => (
              <ActionCard
                key={log.id}
                label={`Open work log ${log.day}`}
                onPress={() => useLog(log, log.day)}
              >
                <Txt style={strong}>{log.day}</Txt>
                <Txt numberOfLines={3}>{log.text}</Txt>
                <Badge label={log.status} />
              </ActionCard>
            ))}
            {before ? (
              <Button
                label="Load older logs"
                secondary
                disabled={work.loading || work.busy}
                onPress={() => void refresh(true)}
              />
            ) : null}
          </>
        ) : null}
        {pane === "documents" ? (
          <>
            <Card>
              <Txt style={strong}>Add company knowledge</Txt>
              <Txt muted>
                Markdown and text files, up to 10 MB. A matching file name
                replaces the earlier document after indexing.
              </Txt>
              <Button
                label="Choose a document"
                secondary
                disabled={work.busy}
                onPress={() => void upload()}
              />
              <Field label="Or paste a document">
                <Input
                  editable={!work.busy}
                  placeholder="Document name"
                  value={docName}
                  onChangeText={setDocName}
                  maxLength={250}
                />
              </Field>
              <Input
                editable={!work.busy}
                accessibilityLabel="Company document text"
                placeholder="Paste the document text…"
                multiline
                value={docText}
                onChangeText={setDocText}
                style={{ minHeight: 130 }}
              />
              <Button
                label="Save document"
                disabled={work.busy || !docName.trim() || !docText.trim()}
                onPress={() => void pasteDocument()}
              />
            </Card>
            {document ? (
              <Card>
                <Row>
                  <Txt style={{ ...strong, flex: 1 }}>{document.name}</Txt>
                  <Button
                    label="Close details"
                    compact
                    secondary
                    onPress={() => {
                      work.cancelRead();
                      setDocument(null);
                    }}
                  />
                </Row>
                <Badge label={document.status} />
                {document.error ? (
                  <Notice tone="error">{document.error}</Notice>
                ) : null}
                {document.headings.map((heading, index) => (
                  <Txt key={`${index}-${heading}`}>{heading}</Txt>
                ))}
                {document.facts.map((fact) => (
                  <View key={fact.id} style={{ gap: 4 }}>
                    <Txt style={strong}>{fact.title}</Txt>
                    <Txt>{fact.summary}</Txt>
                  </View>
                ))}
              </Card>
            ) : null}
            {documents.map((file) => (
              <Card key={file.id}>
                <Txt style={strong}>{file.name}</Txt>
                <Badge label={file.status} />
                {file.error ? <Notice tone="error">{file.error}</Notice> : null}
                <Row>
                  <Button
                    label="View details"
                    secondary
                    compact
                    disabled={work.loading}
                    onPress={() =>
                      void work
                        .read(async (request) =>
                          parseDocument(
                            await request(
                              `${base}/documents/${encodeURIComponent(file.id)}`,
                            ),
                          ),
                        )
                        .then((value) => {
                          if (value) setDocument(value);
                        })
                    }
                  />
                  <Button
                    label="Delete document"
                    secondary
                    danger
                    compact
                    disabled={work.busy}
                    onPress={() =>
                      confirm(
                        "Delete document?",
                        "Its extracted company facts will also be removed.",
                        () => void removeFile(file),
                      )
                    }
                  />
                </Row>
              </Card>
            ))}
            {!documents.length ? (
              <Txt muted>No company documents yet.</Txt>
            ) : null}
          </>
        ) : null}
        {pane === "memory" ? (
          <>
            {proposals.length ? (
              <Txt style={strong}>Review suggested facts</Txt>
            ) : null}
            {proposals.map((proposal) => (
              <Card key={proposal.id}>
                <Txt style={strong}>{proposal.title}</Txt>
                <Txt>{proposal.summary}</Txt>
                {proposal.evidence ? (
                  <Txt muted>{proposal.evidence}</Txt>
                ) : null}
                {proposal.document_name ? (
                  <Txt muted>{proposal.document_name}</Txt>
                ) : null}
                <Row>
                  <Button
                    label="Approve"
                    compact
                    disabled={work.busy}
                    onPress={() => void decideProposal(proposal, true)}
                  />
                  <Button
                    label="Reject"
                    compact
                    secondary
                    disabled={work.busy}
                    onPress={() => void decideProposal(proposal, false)}
                  />
                </Row>
              </Card>
            ))}
            {editing ? (
              <Card>
                <Txt style={strong}>Edit company fact</Txt>
                <Field label="Title">
                  <Input
                    editable={!work.busy}
                    value={editing.title}
                    onChangeText={(title) => setEditing({ ...editing, title })}
                    maxLength={300}
                  />
                </Field>
                <Field label="Summary">
                  <Input
                    editable={!work.busy}
                    value={editing.summary}
                    onChangeText={(summary) =>
                      setEditing({ ...editing, summary })
                    }
                    maxLength={1000}
                    multiline
                  />
                </Field>
                <Row>
                  <Button
                    label="Save fact"
                    compact
                    disabled={
                      work.busy ||
                      !editing.title.trim() ||
                      !editing.summary.trim()
                    }
                    onPress={() =>
                      void work
                        .mutate((request) =>
                          request(
                            `/memory/${encodeURIComponent(editing.id)}`,
                            {
                              title: editing.title.trim(),
                              summary: editing.summary.trim(),
                            },
                            "PATCH",
                          ),
                        )
                        .then((result) => {
                          if (result) {
                            setEditing(null);
                            void refresh();
                          }
                        })
                    }
                  />
                  <Button
                    label="Cancel edit"
                    compact
                    secondary
                    disabled={work.busy}
                    onPress={() => setEditing(null)}
                  />
                </Row>
              </Card>
            ) : null}
            {memory.map((item) => (
              <Card key={item.id}>
                <Badge label={item.kind.replaceAll("_", " ")} />
                <Txt style={strong}>{item.title}</Txt>
                <Txt>{item.summary}</Txt>
                <Row>
                  <Button
                    label="Edit fact"
                    secondary
                    compact
                    disabled={work.busy}
                    onPress={() => setEditing(item)}
                  />
                  <Button
                    label="Archive fact"
                    secondary
                    compact
                    disabled={work.busy}
                    onPress={() =>
                      Alert.alert(
                        "Archive this fact?",
                        "It will stop informing new answers.",
                        [
                          { text: "Cancel", style: "cancel" },
                          {
                            text: "Archive",
                            onPress: () =>
                              void work
                                .mutate((request) =>
                                  request(
                                    `/memory/${encodeURIComponent(item.id)}`,
                                    undefined,
                                    "DELETE",
                                  ),
                                )
                                .then((result) => {
                                  if (result) {
                                    setMemory((old) =>
                                      old.filter((fact) => fact.id !== item.id),
                                    );
                                    if (editing?.id === item.id)
                                      setEditing(null);
                                  }
                                }),
                          },
                        ],
                      )
                    }
                  />
                </Row>
              </Card>
            ))}
            {!memory.length && !proposals.length ? (
              <EmptyState
                icon="work"
                title="No company facts yet"
                description="Save work logs or company documents. Their extracted facts appear here for review."
              />
            ) : null}
          </>
        ) : null}
        <View
          style={{ display: pane === "company" ? "flex" : "none", gap: 16 }}
        >
          <CompanyForm
            key={JSON.stringify(company)}
            api={api}
            company={company}
            onStateChange={setFormState}
            onSaved={(value) => {
              onChanged(value);
              setMessage("Company updated.");
            }}
          />
          <Card>
            <Txt style={strong}>
              {company.status === "active"
                ? "Archive company"
                : "Restore company"}
            </Txt>
            <Txt muted>
              Archiving keeps the company's history and documents. Save any
              company edits first.
            </Txt>
            <Button
              label={
                company.status === "active"
                  ? "Archive company"
                  : "Restore company"
              }
              secondary
              disabled={work.busy || formState.dirty || formState.busy}
              onPress={() =>
                void work
                  .mutate(async (request) =>
                    parseEmployer(
                      await request(
                        base,
                        {
                          status:
                            company.status === "active" ? "archived" : "active",
                        },
                        "PATCH",
                      ),
                    ),
                  )
                  .then((result) => {
                    if (result) onChanged(result.value);
                  })
              }
            />
          </Card>
          <Card>
            <Txt style={strong}>Delete company</Txt>
            <Txt muted>
              This deletes the company's logs, documents, memory and saved
              company calls. Type its name to confirm.
            </Txt>
            <Input
              accessibilityLabel="Confirm company name"
              value={confirmName}
              onChangeText={setConfirmName}
              placeholder={company.name}
            />
            <Button
              label="Delete company and its data"
              danger
              disabled={
                work.busy ||
                confirmName.trim().toLocaleLowerCase() !==
                  company.name.trim().toLocaleLowerCase()
              }
              onPress={() =>
                confirm(
                  "Delete company and all its data?",
                  company.name,
                  () =>
                    void work
                      .mutate((request) =>
                        request(
                          `${base}?confirm=${encodeURIComponent(confirmName.trim())}`,
                          undefined,
                          "DELETE",
                        ),
                      )
                      .then((result) => {
                        if (result) onBack();
                      }),
                )
              }
            />
          </Card>
        </View>
      </Screen>
      <FloatingNotice message={message} onDismiss={() => setMessage("")} />
    </View>
  );
}
