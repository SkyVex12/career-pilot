import React, { useEffect, useRef, useState } from "react";
import { Alert, AppState, View } from "react-native";
import type { Api } from "./api";
import { STAGES, type Application, type Stage } from "./applicationData";
import {
  useWorkspaceRequests,
  type WorkspaceRequest,
} from "./useWorkspaceRequests";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Loading,
  Notice,
  Row,
  Select,
  Txt,
} from "./ui";
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
  type HubRubyConnection,
  type HubRubyItem,
  type HubRubyProfile,
} from "./hubRubyData";

const heading = { fontSize: 16, lineHeight: 23, fontWeight: "600" as const };
/** Account link + deliberate owner-bound imports, embedded in Interview prep. */
export function HubRuby({
  api,
  visible,
  disabled = false,
  application,
  onImported,
  onNotice,
  onBusy,
  onProfilesChanged,
}: {
  api: Api;
  visible: boolean;
  disabled?: boolean;
  application?: Application | null;
  onImported: (application: Application) => void;
  onNotice: (message: string) => void;
  onBusy?: (busy: boolean) => void;
  onProfilesChanged?: () => void;
}) {
  const reads = useWorkspaceRequests(api),
    writes = useWorkspaceRequests(api);
  const [connection, setConnection] = useState<HubRubyConnection | null>(null);
  const [profiles, setProfiles] = useState<HubRubyProfile[]>([]);
  const [code, setCode] = useState(""),
    [query, setQuery] = useState(""),
    [stage, setStage] = useState<Stage | "">(""),
    [source, setSource] = useState("");
  const [items, setItems] = useState<HubRubyItem[] | null>(null),
    [next, setNext] = useState<string | null>(null);
  const [operation, setOperation] = useState("");
  const [status, setStatus] = useState<ReturnType<
    typeof parseHubRubyStatus
  > | null>(null);
  const writing = useRef(false),
    alive = useRef(true),
    visibleRef = useRef(visible),
    disabledRef = useRef(disabled);
  const applicationRef = useRef(application);
  applicationRef.current = application;
  const callbacks = useRef({ onImported, onNotice, onBusy, onProfilesChanged });
  callbacks.current = { onImported, onNotice, onBusy, onProfilesChanged };
  visibleRef.current = visible;
  disabledRef.current = disabled;
  const current = profiles.find((p) =>
    reads.profile ? p.id === reads.profile : p.me,
  );
  const blocked = disabled || writes.busy || reads.loading;
  const canAct = () =>
    alive.current &&
    visibleRef.current &&
    !disabledRef.current &&
    api.profile === reads.profile &&
    AppState.currentState !== "background" &&
    AppState.currentState !== "inactive";

  function clearResults() {
    reads.cancelRead();
    setItems(null);
    setNext(null);
    setStatus(null);
  }
  async function refresh() {
    if (writing.current || !visibleRef.current) return;
    const result = await reads.read(async (request) => {
      const connection = parseHubRubyConnection(
        await request("/career-os/connection"),
      );
      const profiles =
        connection.available && connection.connected
          ? parseHubRubyProfiles(await request("/profiles"))
          : [];
      return { connection, profiles };
    });
    if (!result) return;
    setConnection(result.connection);
    setProfiles(result.profiles);
    setItems(null);
    setNext(null);
    setStatus(null);
    if (!result.connection.connected) setSource("");
  }
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, [api]);
  useEffect(() => {
    if (visible && reads.foreground) void refresh();
    else reads.cancelRead();
    return () => reads.cancelRead();
  }, [api, visible, reads.foreground]);
  useEffect(() => {
    setStatus(null);
  }, [application?.id, application?.career_os?.pulled_at]);
  useEffect(() => {
    callbacks.current.onBusy?.(writes.busy);
    return () => callbacks.current.onBusy?.(false);
  }, [writes.busy]);

  async function mutate<T>(
    name: string,
    task: (request: WorkspaceRequest, signal: AbortSignal) => Promise<T>,
  ) {
    if (!canAct() || writing.current) return undefined;
    writing.current = true;
    reads.cancelRead();
    setOperation(name);
    try {
      return await writes.mutate(task);
    } finally {
      writing.current = false;
      if (alive.current) setOperation("");
    }
  }
  async function connect() {
    const result = await mutate("connect", async (request) =>
      parseHubRubyConnection(
        await request("/career-os/connect", hubRubyCode(code), "POST"),
      ),
    );
    if (!result) return;
    setConnection(result.value);
    setCode("");
    setItems(null);
    setNext(null);
    setSource("");
    callbacks.current.onProfilesChanged?.();
    callbacks.current.onNotice(
      "HubRuby connected. Search and choose an application to import.",
    );
    void refresh();
  }
  function disconnect() {
    if (!canAct() || !connection) return;
    Alert.alert(
      "Disconnect HubRuby?",
      `Imported applications and resumes stay in their profiles. You will need a new one-time code to import again.${connection.required ? " This server requires a HubRuby link before using the account." : ""}`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Disconnect",
          style: "destructive",
          onPress: () => {
            void (async () => {
              const result = await mutate("disconnect", (request) =>
                request("/career-os/connection", undefined, "DELETE"),
              );
              if (result) {
                setConnection((previous) =>
                  previous
                    ? {
                        ...previous,
                        connected: false,
                        status: null,
                        user: null,
                        account_type: null,
                        connected_at: null,
                        last_pull_at: null,
                      }
                    : null,
                );
                setCode("");
                setProfiles([]);
                clearResults();
                callbacks.current.onProfilesChanged?.();
                callbacks.current.onNotice(
                  "HubRuby disconnected. Imported applications were kept.",
                );
              }
              // A failed response may have arrived after the revoke succeeded.
              void refresh();
            })();
          },
        },
      ],
    );
  }
  async function search(more = false) {
    if (!canAct() || writing.current || !connection?.connected) return;
    const cursor = more ? next : null;
    if (more && !cursor) return;
    const result = await reads.read(async (request) => {
      const profiles = parseHubRubyProfiles(await request("/profiles"));
      const current = profiles.find((p) =>
        reads.profile ? p.id === reads.profile : p.me,
      );
      if (!current)
        throw new Error(
          "The selected profile is no longer available. Choose another profile.",
        );
      const owner = current.me ? source : current.career_os_user_id;
      if (!current.me && !owner)
        throw new Error("This profile is not linked to a HubRuby user.");
      if (owner && !profiles.some((p) => p.career_os_user_id === owner))
        throw new Error(
          "That source owner is no longer available. Refresh the connection.",
        );
      const results = parseHubRubyResults(
        await request(hubRubySearchPath(query, stage, owner ?? "", cursor)),
      );
      // Reject mismatched source/profile rows before rendering an import control.
      for (const item of results.items) {
        hubRubyDestination(item, profiles, reads.profile, connection);
        if (
          owner &&
          item.user?.id !== owner &&
          !(
            connection.account_type === "user" &&
            !item.user &&
            connection.user?.id === owner
          )
        )
          throw new Error(
            "HubRuby returned an application for a different owner. Refresh before importing.",
          );
      }
      return { profiles, ...results };
    });
    if (!result) return;
    setProfiles(result.profiles);
    setItems((previous) =>
      more
        ? [
            ...(previous ?? []),
            ...result.items.filter(
              (item) => !previous?.some((p) => p.id === item.id),
            ),
          ]
        : result.items,
    );
    setNext(result.next && result.next !== cursor ? result.next : null);
  }
  function confirmPull(item: HubRubyItem) {
    if (!canAct() || blocked || !connection) return;
    try {
      const destination = hubRubyDestination(
        item,
        profiles,
        reads.profile,
        connection,
      );
      Alert.alert(
        item.pulled ? "Refresh imported application?" : "Import application?",
        `${item.company} · ${item.role}\nDestination: ${destination.name}${destination.current ? " (current profile)" : " (switch profile to view it)"}.\n\n${item.pulled ? "This replaces the imported role fields, job description and imported resume with HubRuby's latest version. Scheduled interviews stay; unsaved local drafts stay separate." : "Copies this application, its job description and its available resume. Nothing syncs automatically."}`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: item.pulled ? "Refresh import" : "Import",
            onPress: () => {
              void (async () => {
                const result = await mutate(
                  `pull:${item.id}`,
                  (request, signal) =>
                    pullHubRubyApplication({
                      request,
                      item,
                      profile: reads.profile,
                      expected: destination,
                      signal,
                    }),
                );
                if (result) {
                  setItems(null);
                  setNext(null);
                  setStatus(null);
                  callbacks.current.onProfilesChanged?.();
                  if (result.value.destination.current)
                    callbacks.current.onImported(result.value.application);
                  callbacks.current.onNotice(
                    `${result.value.again ? "Updated" : "Imported"} ${result.value.application.company} · ${result.value.application.role}.${result.value.destination.current ? " Interview preparation is updating." : ` Switch to ${result.value.destination.name}'s profile to view it.`}`,
                  );
                }
                void refresh();
              })();
            },
          },
        ],
      );
    } catch (e) {
      writes.setError(String(e));
    }
  }
  function refreshImported() {
    if (!application?.career_os || !current || !connection) return;
    confirmPull({
      id: application.career_os.application_id,
      company: application.company,
      role: application.role,
      stage: application.stage,
      user: current.career_os_user_id
        ? { id: current.career_os_user_id, name: current.name }
        : null,
      profile_id: current.id,
      resume: application.resume?.kind ?? "none",
      pulled: {
        application_id: application.id,
        pulled_at: application.career_os.pulled_at,
        changed: true,
      },
    });
  }
  async function checkImported() {
    if (!application?.career_os || !canAct() || writing.current) return;
    const app = application;
    const result = await reads.read(async (request) =>
      parseHubRubyStatus(
        await request(
          `/career-os/applications/${encodeURIComponent(app.career_os!.application_id)}/status`,
        ),
      ),
    );
    if (
      result &&
      applicationRef.current?.id === app.id &&
      applicationRef.current?.career_os?.pulled_at === app.career_os?.pulled_at
    )
      setStatus(result);
  }

  return (
    <View style={{ gap: 12 }}>
      {writes.error || reads.error ? (
        <Notice tone="error">
          {hubRubyError(writes.error || reads.error)}
        </Notice>
      ) : null}
      {!connection && reads.loading ? (
        <Loading label="Loading HubRuby connection" />
      ) : null}
      <Row>
        <Txt style={heading}>HubRuby</Txt>
        <Button
          label="Refresh connection"
          icon="refresh"
          secondary
          compact
          disabled={blocked}
          onPress={() => void refresh()}
        />
      </Row>
      {connection?.available === false ? (
        <Txt muted>HubRuby is not configured on this server.</Txt>
      ) : connection ? (
        connection.connected ? (
          <>
            <Row>
              <Badge label="Connected" />
              <Txt>{connection.user?.name || connection.user?.id}</Txt>
            </Row>
            {connection.account_type !== "user" ? (
              <Txt muted>
                Your admin link can import applications for its authorized
                users. Each application stays in its owner's profile.
              </Txt>
            ) : null}
            {application?.career_os ? (
              <Card>
                <Txt style={heading}>This application's import</Txt>
                <Txt muted>
                  Last imported{" "}
                  {new Date(application.career_os.pulled_at).toLocaleString()}
                </Txt>
                {status ? (
                  <Txt>
                    {status.changed
                      ? "Updated in HubRuby since your last import."
                      : "The imported copy matches HubRuby."}
                  </Txt>
                ) : null}
                <Row>
                  <Button
                    label="Check for updates"
                    secondary
                    compact
                    disabled={blocked}
                    onPress={() => void checkImported()}
                  />
                  <Button
                    label="Refresh imported application"
                    secondary
                    compact
                    disabled={
                      disabled ||
                      reads.loading ||
                      (writes.busy &&
                        operation !==
                          `pull:${application.career_os.application_id}`)
                    }
                    loading={
                      operation ===
                      `pull:${application.career_os.application_id}`
                    }
                    onPress={refreshImported}
                  />
                </Row>
              </Card>
            ) : null}
            <Txt muted>
              Search, then choose an application to copy with its job
              description and resume. Nothing syncs automatically.
            </Txt>
            <Field label="Company or role">
              <Input
                accessibilityLabel="Search HubRuby applications"
                value={query}
                editable={!writes.busy && !disabled}
                maxLength={200}
                placeholder="Company or role"
                onChangeText={(value) => {
                  clearResults();
                  setQuery(value);
                }}
                returnKeyType="search"
                onSubmitEditing={() => void search()}
              />
            </Field>
            <Select
              label="Application stage"
              value={stage}
              options={[{ value: "", label: "Any stage" }, ...STAGES]}
              disabled={disabled || writes.busy}
              onChange={(value) => {
                clearResults();
                setStage(value as Stage | "");
              }}
            />
            {current?.me && connection.account_type !== "user" ? (
              <Select
                label="Application owner"
                value={source}
                options={[
                  { value: "", label: "All authorized users" },
                  ...profiles
                    .filter((p) => p.career_os_user_id)
                    .map((p) => ({
                      value: p.career_os_user_id!,
                      label: p.name || p.career_os_user_id!,
                    })),
                ]}
                disabled={disabled || writes.busy}
                onChange={(value) => {
                  clearResults();
                  setSource(value);
                }}
              />
            ) : current ? (
              <Txt muted>
                Importing for {current.name || "the current profile"}.
              </Txt>
            ) : null}
            <Button
              label="Search HubRuby"
              icon="search"
              disabled={blocked}
              onPress={() => void search()}
            />
            {reads.loading ? <Loading label="Loading HubRuby details" /> : null}
            {items?.length === 0 ? (
              <Txt muted>
                No matching applications. Try another company, role or stage.
              </Txt>
            ) : null}
            {items?.map((item) => (
              <Card key={item.id}>
                <Txt style={heading}>{item.company}</Txt>
                <Txt>{item.role}</Txt>
                <Txt muted>
                  {item.user?.name || item.user?.id || connection.user?.name} ·{" "}
                  {STAGES.find((s) => s.value === item.stage)?.label ??
                    item.stage}{" "}
                  ·{" "}
                  {item.resume === "none"
                    ? "No resume"
                    : `${item.resume === "base" ? "Base" : "Tailored"} resume`}
                </Txt>
                {item.pulled ? (
                  <Badge
                    label={
                      item.pulled.changed
                        ? "Changed in HubRuby"
                        : "Already imported"
                    }
                  />
                ) : null}
                {!item.pulled || item.pulled.changed ? (
                  <Button
                    label={
                      item.pulled
                        ? `Refresh ${item.company} import`
                        : `Import ${item.company}`
                    }
                    disabled={
                      disabled ||
                      reads.loading ||
                      (writes.busy && operation !== `pull:${item.id}`)
                    }
                    loading={operation === `pull:${item.id}`}
                    onPress={() => confirmPull(item)}
                  />
                ) : null}
              </Card>
            ))}
            {next ? (
              <Button
                label="More applications"
                secondary
                disabled={blocked}
                onPress={() => void search(true)}
              />
            ) : null}
            <Button
              label="Disconnect HubRuby"
              danger
              secondary
              disabled={blocked}
              loading={operation === "disconnect"}
              onPress={disconnect}
            />
          </>
        ) : (
          <>
            {connection.status === "revoked" ? (
              <Notice>
                HubRuby no longer accepts this link. Create a new one-time code
                to reconnect.
              </Notice>
            ) : null}
            <Txt muted>
              In HubRuby, open your profile and create a CoRuby code. The code
              is valid for 10 minutes. Connecting allows read-only application
              imports when you request them.
            </Txt>
            <Field label="One-time code">
              <Input
                accessibilityLabel="HubRuby one-time code"
                value={code}
                onChangeText={setCode}
                maxLength={64}
                secureTextEntry
                autoCapitalize="none"
                autoCorrect={false}
                editable={!blocked}
                placeholder="Paste your code"
              />
            </Field>
            <Button
              label="Connect HubRuby"
              disabled={
                disabled ||
                reads.loading ||
                (writes.busy && operation !== "connect") ||
                code.trim().length < 6
              }
              loading={operation === "connect"}
              onPress={() => void connect()}
            />
            {connection.status === "revoked" ? (
              <Button
                label="Remove revoked link"
                danger
                secondary
                disabled={blocked}
                onPress={disconnect}
              />
            ) : null}
          </>
        )
      ) : null}
    </View>
  );
}
