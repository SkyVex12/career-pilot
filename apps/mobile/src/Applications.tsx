import React, { useEffect, useRef, useState } from "react";
import { Alert, View } from "react-native";
import * as Crypto from "expo-crypto";
import type { Api } from "./api";
import { HTTP_TEST_BUILD } from "./transport";
import { FloatingNotice } from "./FloatingNotice";
import { HubRuby } from "./HubRuby";
import { pickWorkspaceFile, type WorkspaceFile } from "./workspaceFiles";
import {
  useWorkspaceRequests,
  type WorkspaceRequest,
} from "./useWorkspaceRequests";
import { localDay } from "./workspaceData";
import {
  applicationBody,
  afterResumeDeletion,
  EMPTY_APPLICATION,
  fieldsOf,
  interviewBody,
  pairingChoices,
  parseApplication,
  parseApplications,
  parseInterviews,
  parseInterview,
  parseResume,
  parseResumes,
  parseDocumentStatuses,
  resumeBody,
  resumeStatus,
  ROUNDS,
  STAGES,
  uploadApplicationDocument,
  type Application,
  type ApplicationFields,
  type DocumentTarget,
  type DocumentTransfer,
  type Interview,
  type Resume,
  type ResumeKind,
  type Stage,
} from "./applicationData";
import {
  ActionCard,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  Loading,
  Notice,
  Row,
  Screen,
  ScreenTitle,
  Segments,
  Select,
  Txt,
} from "./ui";

const heading = { fontSize: 18, lineHeight: 25, fontWeight: "600" as const };
const readableDate = (value: string) => new Date(value).toLocaleString();
type ResumeEditor = {
  key: string;
  id?: string;
  kind: ResumeKind;
  applicationId?: string;
};
type ResumeDraft = { label: string; text: string };
type UploadJob = {
  file: WorkspaceFile;
  target: DocumentTarget;
  transfer?: DocumentTransfer;
};
const hash = async (bytes: Uint8Array) =>
  [
    ...new Uint8Array(
      await Crypto.digest(
        Crypto.CryptoDigestAlgorithm.SHA256,
        new Uint8Array(bytes).buffer,
      ),
    ),
  ]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

/** The same application, resume and interview records used by desktop call setup. */
export function Applications({
  api,
  visible = true,
  onPractice,
  onStartInterview,
  onAsk,
  onProfilesChanged,
}: {
  api: Api;
  visible?: boolean;
  onPractice?: (applicationId: string) => void;
  onStartInterview?: (interviewId: string) => void;
  onAsk?: (applicationId: string) => void;
  onProfilesChanged?: () => void;
}) {
  const catalog = useWorkspaceRequests(api),
    details = useWorkspaceRequests(api),
    actions = useWorkspaceRequests(api);
  const [applications, setApplications] = useState<Application[]>([]),
    [resumes, setResumes] = useState<Resume[]>([]),
    [interviews, setInterviews] = useState<Interview[]>([]);
  const [loaded, setLoaded] = useState(false),
    [section, setSection] = useState("applications"),
    [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null),
    [detail, setDetail] = useState<Application | null>(null),
    [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, ApplicationFields>>({
    new: { ...EMPTY_APPLICATION },
  });
  const [resumeEditor, setResumeEditor] = useState<ResumeEditor | null>(null),
    [resumeDetail, setResumeDetail] = useState<Resume | null>(null),
    [resumeEditing, setResumeEditing] = useState(false);
  const [resumeDrafts, setResumeDrafts] = useState<Record<string, ResumeDraft>>(
    {},
  );
  const [scheduling, setScheduling] = useState(false),
    [round, setRound] = useState("technical"),
    [day, setDay] = useState(localDay(new Date(Date.now() + 3_600_000))),
    [time, setTime] = useState("09:00");
  const [success, setSuccess] = useState(""),
    [upload, setUpload] = useState<UploadJob | null>(null);
  const [pendingDocuments, setPendingDocuments] = useState<
    Record<string, string>
  >({});
  const pendingDocumentsRef = useRef<Record<string, string>>({}),
    writing = useRef(false);
  const uploadRef = useRef<UploadJob | null>(null),
    viewRevision = useRef(0),
    pickerRevision = useRef(0);
  const dirtyApplications = useRef(new Set<string>()),
    dirtyResumes = useRef(new Set<string>());
  const [hubOpen, setHubOpen] = useState(false),
    [hubVisited, setHubVisited] = useState(false),
    [hubBusy, setHubBusy] = useState(false);
  const hubWriting = useRef(false);
  const error = actions.error || details.error || catalog.error;
  const busy = actions.busy || hubBusy;

  async function refresh(catalogOnly = false) {
    if (writing.current || hubWriting.current) return;
    const revision = viewRevision.current;
    const result = await catalog.read(async (request) => {
      const [a, r, i, appDetail, versionDetail, documents] = await Promise.all([
        request("/applications"),
        request("/resumes"),
        request("/interviews"),
        !catalogOnly && selected && selected !== "new" && !editing
          ? request(`/applications/${encodeURIComponent(selected)}`)
          : null,
        !catalogOnly && resumeEditor?.id && !resumeEditing
          ? request(`/resumes/${encodeURIComponent(resumeEditor.id)}`)
          : null,
        Object.keys(pendingDocumentsRef.current).length
          ? request("/documents")
          : null,
      ]);
      return {
        applications: parseApplications(a),
        resumes: parseResumes(r),
        interviews: parseInterviews(i),
        detail: appDetail ? parseApplication(appDetail, true) : null,
        resume: versionDetail ? parseResume(versionDetail) : null,
        documents: documents ? parseDocumentStatuses(documents) : [],
      };
    });
    if (!result) return;
    setApplications(result.applications);
    setResumes(result.resumes);
    setInterviews(result.interviews);
    setLoaded(true);
    if (viewRevision.current === revision) {
      if (result.detail) {
        setDetail(result.detail);
        if (!dirtyApplications.current.has(result.detail.id))
          setDrafts((previous) => ({
            ...previous,
            [result.detail!.id]: fieldsOf(result.detail!),
          }));
      }
      if (result.resume) {
        setResumeDetail(result.resume);
        if (!dirtyResumes.current.has(result.resume.id))
          setResumeDrafts((previous) => ({
            ...previous,
            [result.resume!.id]: {
              label: result.resume!.label,
              text: result.resume!.text ?? "",
            },
          }));
      }
    }
    if (result.documents.length) {
      const pending = { ...pendingDocumentsRef.current };
      for (const document of result.documents) {
        if (
          !pending[document.id] ||
          !["ingested", "failed"].includes(document.status)
        )
          continue;
        if (document.status === "failed")
          actions.setError(
            `${pending[document.id]} could not be read. Choose the file again or paste its text.`,
          );
        delete pending[document.id];
      }
      pendingDocumentsRef.current = pending;
      setPendingDocuments(pending);
    }
  }
  useEffect(() => {
    if (visible && catalog.foreground) void refresh();
    else {
      catalog.cancelRead();
      details.cancelRead();
    }
    return () => {
      catalog.cancelRead();
      details.cancelRead();
    };
  }, [api, visible, catalog.foreground]);
  // Android's document picker can temporarily background this app. Its explicit
  // selection stays valid until the user leaves this workspace or its profile.
  useEffect(() => {
    if (!visible) pickerRevision.current++;
    return () => {
      pickerRevision.current++;
    };
  }, [api, visible]);
  const processing =
    resumes.some((r) => r.status === "extracting") ||
    applications.some((a) => !a.pack?.available) ||
    interviews.some((i) => !i.pack?.available) ||
    !!Object.keys(pendingDocuments).length;
  useEffect(() => {
    if (!visible || !catalog.foreground || !processing || busy) return;
    const timer = setTimeout(() => void refresh(), 5000);
    return () => clearTimeout(timer);
  }, [
    visible,
    catalog.foreground,
    processing,
    applications,
    resumes,
    pendingDocuments,
    busy,
  ]);

  async function mutate<T>(
    operation: (request: WorkspaceRequest, signal: AbortSignal) => Promise<T>,
  ) {
    if (writing.current) return undefined;
    writing.current = true;
    viewRevision.current++;
    catalog.cancelRead();
    details.cancelRead();
    try {
      return await actions.mutate(operation);
    } finally {
      writing.current = false;
    }
  }

  async function openApplication(id: string, preserveEditor = false) {
    const revision = ++viewRevision.current;
    setSelected(id);
    setResumeEditor(null);
    setResumeDetail(null);
    setScheduling(false);
    if (!preserveEditor) {
      setDetail(null);
      setEditing(false);
    }
    actions.setError("");
    setSuccess("");
    const result = await details.read(async (request) =>
      parseApplication(
        await request(`/applications/${encodeURIComponent(id)}`),
        true,
      ),
    );
    if (!result || viewRevision.current !== revision) return;
    setDetail(result);
    setDrafts((previous) =>
      previous[id] && dirtyApplications.current.has(id)
        ? previous
        : { ...previous, [id]: fieldsOf(result) },
    );
  }
  function back() {
    viewRevision.current++;
    details.cancelRead();
    actions.setError("");
    setSuccess("");
    if (resumeEditor) {
      setResumeEditor(null);
      setResumeDetail(null);
      return;
    }
    setSelected(null);
    setDetail(null);
    setEditing(false);
    setScheduling(false);
  }
  function newApplication() {
    viewRevision.current++;
    details.cancelRead();
    setSelected("new");
    setDetail(null);
    setEditing(true);
    setResumeEditor(null);
    setScheduling(false);
    setSuccess("");
    actions.setError("");
  }
  function changeField(key: keyof ApplicationFields, value: string) {
    if (!selected) return;
    dirtyApplications.current.add(selected);
    setDrafts((previous) => ({
      ...previous,
      [selected]: {
        ...(previous[selected] ?? EMPTY_APPLICATION),
        [key]: value,
      },
    }));
  }
  async function saveApplication() {
    if (!selected) return;
    const id = selected;
    const result = await mutate(async (request) =>
      parseApplication(
        await request(
          id === "new"
            ? "/applications"
            : `/applications/${encodeURIComponent(id)}`,
          applicationBody(drafts[id]),
          id === "new" ? "POST" : "PATCH",
        ),
        true,
      ),
    );
    if (!result) return;
    const app = result.value;
    setApplications((previous) =>
      previous.some((row) => row.id === app.id)
        ? previous.map((row) => (row.id === app.id ? app : row))
        : [app, ...previous],
    );
    dirtyApplications.current.delete(id);
    dirtyApplications.current.delete(app.id);
    setSelected(app.id);
    setDetail(app);
    setEditing(false);
    setDrafts((previous) => ({
      ...previous,
      [app.id]: fieldsOf(app),
      ...(id === "new" ? { new: { ...EMPTY_APPLICATION } } : {}),
    }));
    setSuccess("Application saved. Interview preparation is updating.");
    void refresh();
  }
  function deleteApplication() {
    if (!detail) return;
    const app = detail;
    Alert.alert(
      `Delete ${app.company}?`,
      "Its tailored resumes and scheduled interviews will also be deleted.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete application",
          style: "destructive",
          onPress: () => {
            void (async () => {
              const result = await mutate((request) =>
                request(
                  `/applications/${encodeURIComponent(app.id)}`,
                  undefined,
                  "DELETE",
                ),
              );
              if (result) {
                setApplications((previous) =>
                  previous.filter((row) => row.id !== app.id),
                );
                setResumes((previous) =>
                  previous
                    .filter((row) => row.application?.id !== app.id)
                    .map((row) => ({
                      ...row,
                      used_by: row.used_by.filter((id) => id !== app.id),
                    })),
                );
                setInterviews((previous) =>
                  previous.filter((row) => row.job_id !== app.id),
                );
                dirtyApplications.current.delete(app.id);
                back();
                setDrafts((previous) => {
                  const next = { ...previous };
                  delete next[app.id];
                  return next;
                });
                setSuccess("Application deleted.");
                void refresh(true);
              }
            })();
          },
        },
      ],
    );
  }
  async function pair(value: string) {
    if (
      !detail ||
      !pairingChoices(detail).some((choice) => choice.value === value)
    )
      return;
    const id = detail.id;
    const result = await mutate(async (request) =>
      parseApplication(
        await request(
          `/applications/${encodeURIComponent(id)}`,
          { resume_version_id: value },
          "PATCH",
        ),
        true,
      ),
    );
    if (result) {
      setDetail(result.value);
      setApplications((previous) =>
        previous.map((row) =>
          row.id === result.value.id ? result.value : row,
        ),
      );
      setSuccess("Resume pairing saved.");
      void refresh();
    }
  }
  async function schedule() {
    if (!detail) return;
    const id = detail.id;
    const result = await mutate(async (request) => {
      const saved = parseInterview(
        await request(
          "/interviews",
          interviewBody(id, round, day, time),
          "POST",
        ),
      );
      if (saved.job_id !== id)
        throw new Error(
          "The server returned an interview for another application. Refresh before continuing.",
        );
      return saved;
    });
    if (result) {
      setInterviews((previous) => [
        result.value,
        ...previous.filter((row) => row.id !== result.value.id),
      ]);
      setScheduling(false);
      setSuccess("Interview scheduled. Its preparation is updating.");
      void refresh();
    }
  }

  async function openResume(version?: Resume, applicationId?: string) {
    const revision = ++viewRevision.current;
    const editor: ResumeEditor = version
      ? {
          key: version.id,
          id: version.id,
          kind: version.kind,
          applicationId: version.application?.id,
        }
      : {
          key: `new:${applicationId ?? "base"}`,
          kind: applicationId ? "tailored" : "base",
          applicationId,
        };
    details.cancelRead();
    setResumeEditor(editor);
    setResumeDetail(null);
    setResumeEditing(!version);
    setSuccess("");
    actions.setError("");
    if (!version) {
      setResumeDrafts((previous) =>
        previous[editor.key]
          ? previous
          : { ...previous, [editor.key]: { label: "", text: "" } },
      );
      return;
    }
    const result = await details.read(async (request) =>
      parseResume(await request(`/resumes/${encodeURIComponent(version.id)}`)),
    );
    if (!result || viewRevision.current !== revision) return;
    setResumeDetail(result);
    setResumeDrafts((previous) =>
      previous[editor.key] && dirtyResumes.current.has(editor.key)
        ? previous
        : {
            ...previous,
            [editor.key]: { label: result.label, text: result.text ?? "" },
          },
    );
  }
  function changeResume(key: keyof ResumeDraft, value: string) {
    if (!resumeEditor) return;
    dirtyResumes.current.add(resumeEditor.key);
    setResumeDrafts((previous) => ({
      ...previous,
      [resumeEditor.key]: {
        ...(previous[resumeEditor.key] ?? { label: "", text: "" }),
        [key]: value,
      },
    }));
  }
  async function saveResume() {
    if (!resumeEditor) return;
    const editor = resumeEditor,
      draft = resumeDrafts[editor.key];
    const result = await mutate(async (request) => {
      if (!editor.id)
        return parseResume(
          await request(
            "/resumes",
            resumeBody(
              draft.label,
              draft.text,
              editor.kind,
              editor.applicationId,
            ),
            "POST",
          ),
        );
      if (!draft.label.trim() || draft.label.trim().length > 200)
        throw new Error("Enter a resume label of 1-200 characters.");
      const body: { label: string; text?: string } = {
        label: draft.label.trim(),
      };
      if (draft.text !== (resumeDetail?.text ?? "")) {
        body.text = resumeBody(
          draft.label,
          draft.text,
          editor.kind,
          editor.applicationId,
        ).text;
      }
      return parseResume(
        await request(
          `/resumes/${encodeURIComponent(editor.id)}`,
          body,
          "PATCH",
        ),
      );
    });
    if (!result) return;
    const saved = result.value;
    setResumes((previous) => [
      saved,
      ...previous.filter(
        (row) =>
          row.id !== saved.id && (saved.kind !== "base" || row.kind !== "base"),
      ),
    ]);
    dirtyResumes.current.delete(editor.key);
    dirtyResumes.current.delete(saved.id);
    setResumeDetail({ ...saved, text: saved.text ?? draft.text });
    setResumeEditor({ ...editor, key: saved.id, id: saved.id });
    setResumeEditing(false);
    setResumeDrafts((previous) => {
      const next = {
        ...previous,
        [saved.id]: { label: saved.label, text: saved.text ?? draft.text },
      };
      if (!editor.id) delete next[editor.key];
      return next;
    });
    setSuccess(
      "Resume saved. Its facts and interview preparation are updating.",
    );
    void refresh();
    if (selected && selected !== "new") {
      const app = await details.read(async (request) =>
        parseApplication(
          await request(`/applications/${encodeURIComponent(selected)}`),
          true,
        ),
      );
      if (app) setDetail(app);
    }
  }
  function deleteResume() {
    if (!resumeDetail) return;
    const version = resumeDetail;
    Alert.alert(
      `Delete ${version.label}?`,
      `This removes its extracted facts too. ${version.used_by.length} application(s) will use their next available resume.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete resume",
          style: "destructive",
          onPress: () => {
            void (async () => {
              const result = await mutate((request) =>
                request(
                  `/resumes/${encodeURIComponent(version.id)}`,
                  undefined,
                  "DELETE",
                ),
              );
              if (result) {
                const affected = new Set([
                  ...version.used_by,
                  ...applications
                    .filter((row) => row.resume?.version_id === version.id)
                    .map((row) => row.id),
                ]);
                setResumes((previous) =>
                  previous.filter((row) => row.id !== version.id),
                );
                setApplications((previous) =>
                  previous.map((row) => afterResumeDeletion(row, version.id)),
                );
                setDetail((previous) =>
                  previous ? afterResumeDeletion(previous, version.id) : null,
                );
                setInterviews((previous) =>
                  previous.map((row) =>
                    affected.has(row.job_id) ? { ...row, pack: null } : row,
                  ),
                );
                dirtyResumes.current.delete(version.id);
                back();
                setResumeDrafts((previous) => {
                  const next = { ...previous };
                  delete next[version.id];
                  return next;
                });
                setSuccess("Resume deleted.");
                void refresh(true);
                if (selected && selected !== "new")
                  void openApplication(selected, true);
              }
            })();
          },
        },
      ],
    );
  }

  async function sendFile(job: UploadJob) {
    uploadRef.current = job;
    setUpload(job);
    const result = await mutate((request, signal) =>
      uploadApplicationDocument({
        request,
        file: job.file,
        target: job.target,
        signal,
        hash,
        allowHttp: HTTP_TEST_BUILD,
        pending: job.transfer,
        progress: (transfer) => {
          const next = { ...job, transfer };
          uploadRef.current = next;
          setUpload(next);
        },
      }),
    );
    if (!result) return;
    if (job.target.kind === "job_description" && job.target.application_id)
      dirtyApplications.current.delete(job.target.application_id);
    if (job.target.kind === "resume" && resumeEditor) {
      const key = resumeEditor.key;
      dirtyResumes.current.delete(key);
      setResumeDrafts((previous) => {
        const next = { ...previous };
        delete next[key];
        return next;
      });
    }
    if (uploadRef.current?.transfer?.documentId) {
      const pending = {
        ...pendingDocumentsRef.current,
        [uploadRef.current.transfer.documentId]: job.file.name,
      };
      pendingDocumentsRef.current = pending;
      setPendingDocuments(pending);
    }
    uploadRef.current = null;
    setUpload(null);
    setSuccess(
      "File uploaded. Reading and interview preparation will update shortly.",
    );
    if (resumeEditor) {
      setResumeEditor(null);
      setResumeDetail(null);
    }
    void refresh();
    if (selected && selected !== "new") void openApplication(selected);
  }
  async function chooseFile(target: "resume" | "job_description") {
    const revision = ++pickerRevision.current;
    try {
      actions.setError("");
      const file = await pickWorkspaceFile(target === "resume" || !!detail);
      if (!file || pickerRevision.current !== revision) return;
      if (file.contentType !== "application/pdf") {
        const value = new TextDecoder("utf-8", { fatal: true }).decode(
          file.bytes,
        );
        if (target === "job_description") {
          if (value.length > 50_000)
            throw new Error(
              "Job descriptions must be at most 50,000 characters.",
            );
          changeField("jd_text", value);
          setEditing(true);
        } else {
          if (value.length > 100_000)
            throw new Error("Resume text must be at most 100,000 characters.");
          changeResume("text", value);
          setResumeEditing(true);
        }
        setSuccess("Text loaded into your draft. Review it before saving.");
        return;
      }
      const editor = resumeEditor;
      const documentTarget: DocumentTarget =
        target === "job_description"
          ? { kind: target, application_id: detail!.id }
          : {
              kind: "resume",
              ...(editor?.id ? { resume_version_id: editor.id } : {}),
              ...(editor?.applicationId
                ? { application_id: editor.applicationId }
                : {}),
              label:
                resumeDrafts[editor!.key]?.label.trim() ||
                file.name.slice(0, 200),
            };
      await sendFile({ file, target: documentTarget });
    } catch (e) {
      actions.setError(e instanceof Error ? e.message : String(e));
    }
  }

  const draft = selected ? drafts[selected] : null,
    resumeDraft = resumeEditor ? resumeDrafts[resumeEditor.key] : null;
  const selectedInterviews = detail
    ? interviews
        .filter((i) => i.job_id === detail.id)
        .sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))
    : [];
  const filtered = applications.filter((app) =>
    query
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .every((word) =>
        `${app.company} ${app.role}`.toLowerCase().includes(word),
      ),
  );
  const base = resumes.find((r) => r.kind === "base");
  const hubAvailable =
    !resumeEditor &&
    !editing &&
    (selected ? !!detail?.career_os : section === "applications");
  function imported(app: Application) {
    viewRevision.current++;
    details.cancelRead();
    catalog.cancelRead();
    setApplications((previous) =>
      previous.some((p) => p.id === app.id)
        ? previous.map((p) => (p.id === app.id ? app : p))
        : [app, ...previous],
    );
    setSelected(app.id);
    setDetail(app);
    setEditing(false);
    setScheduling(false);
    setResumeEditor(null);
    setDrafts((previous) =>
      dirtyApplications.current.has(app.id)
        ? previous
        : { ...previous, [app.id]: fieldsOf(app) },
    );
  }
  function hubBusyChanged(value: boolean) {
    const previous = hubWriting.current;
    hubWriting.current = value;
    setHubBusy(value);
    if (value) {
      catalog.cancelRead();
      details.cancelRead();
      viewRevision.current++;
    } else if (previous) void refresh(true);
  }
  const editorField = (
    key: keyof ApplicationFields,
    label: string,
    multiline = false,
    maxLength?: number,
  ) => (
    <Field label={label} key={key}>
      <Input
        accessibilityLabel={label}
        value={draft?.[key] ?? ""}
        onChangeText={(value) => changeField(key, value)}
        multiline={multiline}
        editable={!busy}
        maxLength={maxLength}
        style={
          multiline
            ? {
                minHeight: key === "jd_text" ? 180 : 100,
                textAlignVertical: "top",
              }
            : undefined
        }
        autoCapitalize={key === "url" ? "none" : "sentences"}
      />
    </Field>
  );
  return (
    <View style={{ flex: 1 }}>
      <Screen>
        <ScreenTitle
          title={
            resumeEditor
              ? resumeEditor.id
                ? (resumeDetail?.label ?? "Resume")
                : resumeEditor.kind === "base"
                  ? "Add base resume"
                  : "Add tailored resume"
              : selected === "new"
                ? "Add application"
                : (detail?.company ?? "Applications & resumes")
          }
          subtitle={
            resumeEditor
              ? "Your experience, ready for every conversation."
              : detail
                ? detail.role
                : "Keep each role, resume and interview together."
          }
          action={
            selected || resumeEditor ? (
              <Button
                label="Back"
                secondary
                compact
                disabled={busy}
                onPress={back}
              />
            ) : (
              <Button
                label="Refresh"
                icon="refresh"
                secondary
                compact
                disabled={catalog.loading || busy}
                onPress={() => void refresh()}
              />
            )
          }
        />
        {error ? (
          <Notice
            tone="error"
            action={
              !busy ? (
                <Button
                  label="Retry loading"
                  secondary
                  compact
                  onPress={() => {
                    void refresh();
                    if (resumeEditor?.id) {
                      const version = resumes.find(
                        (r) => r.id === resumeEditor.id,
                      );
                      if (version) void openResume(version);
                    } else if (selected && selected !== "new")
                      void openApplication(selected, editing);
                  }}
                />
              ) : undefined
            }
          >
            {error}
          </Notice>
        ) : null}
        <View style={{ display: hubAvailable ? "flex" : "none" }}>
          <Card>
            <Row>
              <Txt style={heading}>
                {detail?.career_os
                  ? "Imported from HubRuby"
                  : "Import from HubRuby"}
              </Txt>
              <Button
                label={hubOpen ? "Hide HubRuby" : "Manage HubRuby"}
                secondary
                compact
                disabled={busy}
                onPress={() => {
                  setHubVisited(true);
                  setHubOpen(!hubOpen);
                }}
              />
            </Row>
            <View style={{ display: hubOpen ? "flex" : "none" }}>
              {hubVisited ? (
                <HubRuby
                  api={api}
                  visible={visible && hubAvailable && hubOpen}
                  disabled={actions.busy}
                  application={detail}
                  onImported={imported}
                  onNotice={setSuccess}
                  onBusy={hubBusyChanged}
                  onProfilesChanged={onProfilesChanged}
                />
              ) : null}
            </View>
          </Card>
        </View>
        {Object.keys(pendingDocuments).length ? (
          <Notice>
            Reading {Object.values(pendingDocuments).join(", ")}. Your resume
            and interview preparation will update here.
          </Notice>
        ) : null}
        {upload && !busy ? (
          <Notice
            action={
              <Row>
                <Button
                  label="Retry file upload"
                  compact
                  onPress={() => void sendFile(uploadRef.current ?? upload)}
                />
                <Button
                  label="Discard upload"
                  secondary
                  compact
                  onPress={() => {
                    uploadRef.current = null;
                    setUpload(null);
                  }}
                />
              </Row>
            }
          >
            {upload.file.name}:{" "}
            {upload.transfer?.uploaded
              ? "File received; processing has not been confirmed."
              : "Upload has not completed."}
          </Notice>
        ) : null}
        {resumeEditor ? (
          <>
            {details.loading ? (
              <Loading label="Loading resume" />
            ) : resumeDraft ? (
              <Card>
                {resumeDetail ? (
                  <Row>
                    <Badge label={resumeStatus(resumeDetail.status)} />
                    <Txt muted>
                      {resumeDetail.used_by.length} application(s)
                    </Txt>
                    {resumeDetail.digest ? (
                      <Txt muted>
                        {resumeDetail.digest.facts} extracted facts
                      </Txt>
                    ) : null}
                  </Row>
                ) : null}
                <Field label="Resume label">
                  <Input
                    accessibilityLabel="Resume label"
                    value={resumeDraft.label}
                    onChangeText={(value) => changeResume("label", value)}
                    editable={!busy && resumeEditing}
                    maxLength={200}
                    placeholder="e.g. Software engineer resume"
                  />
                </Field>
                {resumeEditing ? (
                  <Field
                    label="Resume text"
                    hint="Paste your resume or load a PDF, Markdown or text file."
                  >
                    <Input
                      accessibilityLabel="Resume text"
                      value={resumeDraft.text}
                      onChangeText={(value) => changeResume("text", value)}
                      multiline
                      editable={!busy}
                      maxLength={100_000}
                      style={{ minHeight: 230, textAlignVertical: "top" }}
                    />
                  </Field>
                ) : resumeDraft.text ? (
                  <Txt selectable>{resumeDraft.text}</Txt>
                ) : (
                  <Txt muted>
                    This version was uploaded as a file. Its extracted facts are
                    used for interview preparation. Choose Edit to replace the
                    file or paste text.
                  </Txt>
                )}
                <Row>
                  {resumeEditing ? (
                    <>
                      <Button
                        label="Save resume"
                        loading={busy}
                        onPress={() => void saveResume()}
                      />
                      <Button
                        label="Choose resume file"
                        icon="file"
                        secondary
                        disabled={busy}
                        onPress={() => void chooseFile("resume")}
                      />
                    </>
                  ) : (
                    <Button
                      label="Edit text or label"
                      secondary
                      disabled={busy}
                      onPress={() => setResumeEditing(true)}
                    />
                  )}
                  {resumeEditor.id ? (
                    <Button
                      label="Delete resume"
                      danger
                      secondary
                      disabled={busy}
                      onPress={deleteResume}
                    />
                  ) : null}
                </Row>
              </Card>
            ) : null}
          </>
        ) : selected ? (
          <>
            {details.loading && !detail && selected !== "new" ? (
              <Loading label="Loading application" />
            ) : null}
            {editing && draft ? (
              <Card>
                <Txt style={heading}>
                  {selected === "new" ? "Role details" : "Edit application"}
                </Txt>
                {editorField("company", "Company", false, 200)}
                {editorField("role", "Role", false, 200)}
                <Select
                  label="Stage"
                  value={draft.stage}
                  options={[...STAGES]}
                  disabled={busy}
                  onChange={(value) => changeField("stage", value as Stage)}
                />
                {editorField("url", "Job link", false, 2000)}
                {editorField("source_site", "Source", false, 200)}
                {editorField("jd_text", "Job description", true, 50_000)}
                <Button
                  label={
                    detail
                      ? "Load job description file"
                      : "Load text or Markdown"
                  }
                  icon="file"
                  secondary
                  disabled={busy}
                  onPress={() => void chooseFile("job_description")}
                />
                {editorField("note", "Notes", true, 10_000)}
                <Row>
                  <Button
                    label="Save application"
                    loading={busy}
                    onPress={() => void saveApplication()}
                  />
                  {selected !== "new" ? (
                    <Button
                      label="Close editor"
                      secondary
                      disabled={busy}
                      onPress={() => setEditing(false)}
                    />
                  ) : null}
                </Row>
              </Card>
            ) : detail ? (
              <>
                <Row>
                  <Badge
                    label={
                      STAGES.find((s) => s.value === detail.stage)?.label ??
                      detail.stage
                    }
                  />
                  <Badge
                    label={
                      detail.pack?.available
                        ? "Preparation ready"
                        : "Preparation updating"
                    }
                  />
                </Row>
                <Row>
                  <Button
                    label="Edit application"
                    secondary
                    disabled={busy}
                    onPress={() => setEditing(true)}
                  />
                  {onPractice ? (
                    <Button
                      label="Practice this role"
                      onPress={() => onPractice(detail.id)}
                    />
                  ) : null}
                  {onAsk ? (
                    <Button
                      label="Ask about this role"
                      secondary
                      onPress={() => onAsk(detail.id)}
                    />
                  ) : null}
                </Row>
                <Card>
                  <Txt style={heading}>Resume</Txt>
                  <Select
                    label="Resume used for this application"
                    value={
                      detail.resume?.manual ? detail.resume.version_id : "auto"
                    }
                    options={pairingChoices(detail)}
                    disabled={busy || detail.pairing_pending}
                    onChange={(value) => void pair(value)}
                  />
                  {detail.pairing_pending ? (
                    <Txt muted>
                      Resume removed. Refresh to confirm the automatic pairing.
                    </Txt>
                  ) : detail.resume ? (
                    <Txt muted>
                      {detail.resume.label} |{" "}
                      {resumeStatus(detail.resume.status)}
                    </Txt>
                  ) : (
                    <Txt muted>
                      Add a resume to ground answers in your experience.
                    </Txt>
                  )}
                  <Row>
                    <Button
                      label="Add tailored resume"
                      icon="plus"
                      secondary
                      disabled={busy}
                      onPress={() => void openResume(undefined, detail.id)}
                    />
                    {base ? (
                      <Button
                        label="View base resume"
                        secondary
                        disabled={busy}
                        onPress={() => void openResume(base)}
                      />
                    ) : (
                      <Button
                        label="Add base resume"
                        secondary
                        disabled={busy}
                        onPress={() => void openResume()}
                      />
                    )}
                  </Row>
                  {resumes
                    .filter((r) => r.application?.id === detail.id)
                    .map((r) => (
                      <ActionCard
                        key={r.id}
                        label={`Open ${r.label}`}
                        onPress={() => {
                          if (!busy) void openResume(r);
                        }}
                      >
                        <Txt>{r.label}</Txt>
                        <Txt muted>{resumeStatus(r.status)}</Txt>
                      </ActionCard>
                    ))}
                </Card>
                <Card>
                  <Txt style={heading}>Job description</Txt>
                  <Txt selectable>
                    {detail.jd_text || "No job description."}
                  </Txt>
                  {detail.note ? (
                    <>
                      <Txt style={heading}>Notes</Txt>
                      <Txt selectable>{detail.note}</Txt>
                    </>
                  ) : null}
                  {detail.url ? (
                    <Txt selectable muted>
                      {detail.url}
                    </Txt>
                  ) : null}
                </Card>
                <Card>
                  <Row>
                    <Txt style={heading}>Interviews</Txt>
                    <Button
                      label={
                        scheduling ? "Close scheduling" : "Schedule interview"
                      }
                      secondary
                      compact
                      disabled={busy}
                      onPress={() => setScheduling((v) => !v)}
                    />
                  </Row>
                  {scheduling ? (
                    <View style={{ gap: 12 }}>
                      <Select
                        label="Interview round"
                        value={round}
                        options={[...ROUNDS]}
                        disabled={busy}
                        onChange={setRound}
                      />
                      <Field label="Date" hint="YYYY-MM-DD">
                        <Input
                          accessibilityLabel="Interview date"
                          value={day}
                          onChangeText={setDay}
                          placeholder="2026-12-01"
                          maxLength={10}
                          editable={!busy}
                        />
                      </Field>
                      <Field
                        label="Time"
                        hint={`24-hour time in ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`}
                      >
                        <Input
                          accessibilityLabel="Interview time"
                          value={time}
                          onChangeText={setTime}
                          placeholder="09:30"
                          maxLength={5}
                          editable={!busy}
                        />
                      </Field>
                      <Button
                        label="Save interview"
                        loading={busy}
                        onPress={() => void schedule()}
                      />
                    </View>
                  ) : null}
                  {!selectedInterviews.length ? (
                    <Txt muted>No interviews scheduled.</Txt>
                  ) : (
                    selectedInterviews.map((i) => (
                      <View key={i.id} style={{ gap: 5 }}>
                        <Txt style={{ fontWeight: "600" }}>
                          {ROUNDS.find((r) => r.value === i.round)?.label ??
                            i.round}
                        </Txt>
                        <Txt muted>{readableDate(i.starts_at)}</Txt>
                        {onStartInterview ? (
                          <Button
                            label={`Start ${ROUNDS.find((r) => r.value === i.round)?.label ?? i.round} call`}
                            secondary
                            compact
                            onPress={() => onStartInterview(i.id)}
                          />
                        ) : null}
                      </View>
                    ))
                  )}
                </Card>
                <Button
                  label="Delete application"
                  danger
                  secondary
                  disabled={busy}
                  onPress={deleteApplication}
                />
              </>
            ) : null}
          </>
        ) : (
          <>
            <Segments
              value={section}
              options={[
                { value: "applications", label: "Applications" },
                { value: "resumes", label: "Resumes" },
              ]}
              disabled={busy}
              onChange={setSection}
            />
            {catalog.loading && !loaded ? (
              <Loading label="Loading applications and resumes" />
            ) : loaded ? (
              section === "applications" ? (
                <>
                  <Button
                    label="Add application"
                    icon="plus"
                    disabled={busy}
                    onPress={newApplication}
                  />
                  <Input
                    accessibilityLabel="Search applications"
                    placeholder="Search company or role"
                    value={query}
                    onChangeText={setQuery}
                  />
                  {!filtered.length ? (
                    <EmptyState
                      icon="practice"
                      title={
                        applications.length
                          ? "No matching applications"
                          : "Prepare for your next role"
                      }
                      description={
                        applications.length
                          ? "Try another company or job title."
                          : "Add a role to keep its job description, resume and interviews together."
                      }
                    />
                  ) : (
                    filtered.map((app) => (
                      <ActionCard
                        key={app.id}
                        label={`Open ${app.company}, ${app.role}`}
                        onPress={() => {
                          if (!busy) void openApplication(app.id);
                        }}
                      >
                        <Row>
                          <Txt style={heading}>{app.company}</Txt>
                          <Badge
                            label={
                              STAGES.find((s) => s.value === app.stage)
                                ?.label ?? app.stage
                            }
                          />
                        </Row>
                        <Txt>{app.role}</Txt>
                        <Txt muted>
                          {app.pairing_pending
                            ? "Resume pairing updating"
                            : (app.resume?.label ?? "No resume attached")}
                        </Txt>
                      </ActionCard>
                    ))
                  )}
                </>
              ) : (
                <>
                  <Button
                    label={base ? "Replace base resume" : "Add base resume"}
                    icon="plus"
                    disabled={busy}
                    onPress={() => {
                      void openResume(base);
                      if (base) setResumeEditing(true);
                    }}
                  />
                  {!resumes.length ? (
                    <EmptyState
                      icon="file"
                      title="Your experience starts here"
                      description="Add a base resume for every call. Tailored resumes can be added inside an application."
                    />
                  ) : (
                    resumes.map((version) => (
                      <ActionCard
                        key={version.id}
                        label={`Open ${version.label}`}
                        onPress={() => {
                          if (!busy) void openResume(version);
                        }}
                      >
                        <Row>
                          <Txt style={heading}>{version.label}</Txt>
                          <Badge label={resumeStatus(version.status)} />
                        </Row>
                        <Txt muted>
                          {version.kind === "base"
                            ? "Base resume | available to every application"
                            : `${version.application?.company ?? "Application"} | ${version.application?.role ?? "Tailored resume"}`}
                        </Txt>
                      </ActionCard>
                    ))
                  )}
                </>
              )
            ) : null}
          </>
        )}
      </Screen>
      <FloatingNotice message={success} onDismiss={() => setSuccess("")} />
    </View>
  );
}
