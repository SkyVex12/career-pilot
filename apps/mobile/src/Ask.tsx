import React, { useEffect, useRef, useState, useContext } from "react";
import {
  Alert,
  AppState,
  Modal,
  Pressable,
  Platform,
  ScrollView,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { randomUUID } from "expo-crypto";
import { Api } from "./api";
import { RequestScope } from "./requestScope";
import {
  Button,
  Card,
  Input,
  Row,
  Txt,
  page,
  Theme,
  ScreenTitle,
  IconButton,
  Icon,
  EmptyState,
  Loading,
  Notice,
  Select,
  Field,
  Badge,
} from "./ui";
import {
  needsItem,
  makeScope,
  parseConversation,
  parseConversations,
  parseOptions,
  parseSourceDetail,
  type Scope,
  type ScopeKind,
  type Source,
  type SourceDetail,
  type Message,
  type Conversation,
} from "./askData";
const contextChoices = [
  {
    value: "auto",
    label: "Automatic",
    description: "Find relevant career context for this question",
  },
  {
    value: "general",
    label: "General knowledge",
    description: "Answer without your private career records",
  },
  { value: "all", label: "All career context" },
  { value: "company", label: "One company" },
  { value: "application", label: "One application" },
  { value: "document", label: "One document" },
  { value: "call", label: "One saved call" },
];
export function Ask({ api, visible = true }: { api: Api; visible?: boolean }) {
  const c = useContext(Theme);
  const [list, setList] = useState<Conversation[]>([]),
    [current, setCurrent] = useState<Conversation | null>(null);
  const [draft, setDraft] = useState(""),
    [kind, setKind] = useState<ScopeKind>("auto"),
    [choice, setChoice] = useState("");
  const [options, setOptions] = useState<{ id: string; label: string }[]>([]),
    [from, setFrom] = useState(""),
    [to, setTo] = useState("");
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [listLoading, setListLoading] = useState(false),
    [optionsLoading, setOptionsLoading] = useState(false);
  const [source, setSource] = useState<SourceDetail | null>(null),
    [sourceLoading, setSourceLoading] = useState(false);
  const [panel, setPanel] = useState<
      "context" | "conversations" | "manage" | null
    >(null),
    [title, setTitle] = useState("");
  const [foreground, setForeground] = useState(
    AppState.currentState === "active",
  );
  const [requests] = useState(() => ({
    list: new RequestScope(),
    load: new RequestScope(),
    options: new RequestScope(),
    source: new RequestScope(),
    mutation: new RequestScope(),
  }));
  const mutationBusy = useRef(false);
  const alive = useRef(true),
    selected = useRef<string | null>(null),
    scroll = useRef<React.ElementRef<typeof ScrollView>>(null),
    atBottom = useRef(true);
  const operation = useRef<{
    request_id: string;
    question: string;
    scope: Scope;
    conversationId?: string;
  } | null>(null);
  const pending = current?.messages?.find((m) => m.status === "pending");
  function closeSource() {
    requests.source.invalidate();
    setSource(null);
    setSourceLoading(false);
  }
  function changeScope(scope: Scope) {
    setKind(scope.kind);
    setChoice(scope.id ?? "");
    setFrom(scope.from_date ?? "");
    setTo(scope.to_date ?? "");
  }
  async function refresh() {
    setListLoading(true);
    try {
      const result = await requests.list.run((signal) =>
        api
          .request("/ask/conversations", undefined, "GET", true, signal)
          .then(parseConversations),
      );
      if (result) setList(result);
    } catch (e) {
      if (alive.current) setError(String(e));
    } finally {
      if (alive.current) setListLoading(false);
    }
  }
  async function load(id: string, reset = true) {
    closeSource();
    selected.current = id;
    setLoading(true);
    setError("");
    if (reset) {
      setCurrent(null);
      operation.current = null;
      setDraft("");
      atBottom.current = true;
    }
    try {
      const result = await requests.load.run((signal) =>
        api
          .request(
            `/ask/conversations/${encodeURIComponent(id)}`,
            undefined,
            "GET",
            true,
            signal,
          )
          .then((v) => parseConversation(v, true)),
      );
      if (result && selected.current === id) {
        setCurrent(result);
        if (reset) changeScope(result.scope);
      }
    } catch (e) {
      if (alive.current && selected.current === id) setError(String(e));
    } finally {
      if (alive.current && selected.current === id) setLoading(false);
    }
  }
  function newConversation() {
    requests.load.invalidate();
    closeSource();
    selected.current = null;
    setCurrent(null);
    operation.current = null;
    setDraft("");
    setError("");
    setLoading(false);
    changeScope({ kind: "auto" });
    setPanel(null);
    atBottom.current = true;
  }
  useEffect(() => {
    alive.current = true;
    void refresh();
    const subscription = AppState.addEventListener("change", (state) =>
      setForeground(state === "active"),
    );
    return () => {
      alive.current = false;
      subscription.remove();
      Object.values(requests).forEach((r) => r.invalidate());
      selected.current = null;
    };
  }, [api]);
  useEffect(() => {
    let active = true;
    setOptions([]);
    setOptionsLoading(needsItem(kind));
    if (needsItem(kind))
      void requests.options
        .run((signal) =>
          api
            .request(
              `/ask/context-options?kind=${kind}`,
              undefined,
              "GET",
              true,
              signal,
            )
            .then(parseOptions),
        )
        .then((result) => {
          if (active && result) setOptions(result);
        })
        .catch((e) => {
          if (active) setError(String(e));
        })
        .finally(() => {
          if (active) setOptionsLoading(false);
        });
    return () => {
      active = false;
      requests.options.invalidate();
    };
  }, [kind, api]);
  useEffect(() => {
    if (!pending || !current || !foreground || !visible) return;
    const id = current.id,
      scope = new RequestScope();
    let active = true,
      timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await scope.run((signal) =>
          api
            .request(
              `/ask/conversations/${encodeURIComponent(id)}`,
              undefined,
              "GET",
              true,
              signal,
            )
            .then((v) => parseConversation(v, true)),
        );
        if (active && result && selected.current === id) {
          setCurrent(result);
          setError("");
        }
      } catch (e) {
        if (active) setError(`Answer update interrupted. ${e}`);
      } finally {
        if (active) timer = setTimeout(() => void poll(), 1500);
      }
    }
    timer = setTimeout(() => void poll(), 1000);
    return () => {
      active = false;
      clearTimeout(timer);
      scope.invalidate();
    };
  }, [pending?.id, current?.id, foreground, visible, api]);
  async function send() {
    if (!draft.trim() || mutationBusy.current || pending || loading) return;
    mutationBusy.current = true;
    setBusy(true);
    setError("");
    try {
      const body = operation.current ?? {
        request_id: randomUUID(),
        question: draft.trim(),
        scope: makeScope(kind, choice, from, to),
        conversationId: current?.id,
      };
      operation.current = body;
      const result = await requests.mutation.run(async (signal) => {
        let id = body.conversationId;
        if (!id) {
          const conversation = parseConversation(
            await api.request(
              "/ask/conversations",
              { title: body.question.slice(0, 100), scope: body.scope },
              "POST",
              true,
              signal,
            ),
          );
          signal.throwIfAborted();
          body.conversationId = conversation.id;
          id = conversation.id;
          selected.current = id;
          setCurrent(conversation);
        }
        await api.request(
          `/ask/conversations/${encodeURIComponent(id)}/messages`,
          {
            request_id: body.request_id,
            question: body.question,
            scope: body.scope,
          },
          "POST",
          true,
          signal,
        );
        return id;
      });
      if (result && alive.current) {
        operation.current = null;
        setDraft("");
        atBottom.current = true;
        await load(result, false);
        void refresh();
      }
    } catch (e) {
      if (alive.current) setError(String(e));
    } finally {
      mutationBusy.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function control(message: Message, action: "cancel" | "retry") {
    if (!current || mutationBusy.current) return;
    mutationBusy.current = true;
    const id = current.id;
    setBusy(true);
    setError("");
    try {
      const ok = await requests.mutation.run(async (signal) => {
        await api.request(
          `/ask/conversations/${encodeURIComponent(id)}/messages/${encodeURIComponent(message.id)}/${action}`,
          {},
          "POST",
          true,
          signal,
        );
        return true;
      });
      if (ok && selected.current === id) await load(id, false);
    } catch (e) {
      if (alive.current) setError(String(e));
    } finally {
      mutationBusy.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function manage(remove: boolean) {
    if (!current || mutationBusy.current || (!remove && !title.trim())) return;
    mutationBusy.current = true;
    const id = current.id;
    setBusy(true);
    setError("");
    try {
      const ok = await requests.mutation.run(async (signal) => {
        await api.request(
          `/ask/conversations/${encodeURIComponent(id)}`,
          remove ? undefined : { title: title.trim() },
          remove ? "DELETE" : "PATCH",
          true,
          signal,
        );
        return true;
      });
      if (ok && selected.current === id) {
        setPanel(null);
        if (remove) newConversation();
        else await load(id, false);
        void refresh();
      }
    } catch (e) {
      if (alive.current) setError(String(e));
    } finally {
      mutationBusy.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function openSource(item: Source) {
    const id = selected.current;
    setSource(null);
    setSourceLoading(true);
    setError("");
    try {
      const result = await requests.source.run((signal) =>
        api
          .request(
            `/ask/sources/${item.kind}/${encodeURIComponent(item.id)}`,
            undefined,
            "GET",
            true,
            signal,
          )
          .then(parseSourceDetail),
      );
      if (result && id === selected.current) setSource(result);
    } catch (e) {
      if (alive.current && id === selected.current)
        setError(`Source unavailable or access changed. ${e}`);
    } finally {
      if (alive.current && id === selected.current) setSourceLoading(false);
    }
  }
  const contextLabel =
    options.find((o) => o.id === choice)?.label ??
    contextChoices.find((o) => o.value === kind)?.label ??
    "Automatic";
  return (
    <View style={{ flex: 1 }}>
      <ScrollView
        ref={scroll}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode={Platform.OS === "ios" ? "interactive" : "on-drag"}
        contentContainerStyle={page}
        scrollEventThrottle={100}
        onScroll={(e) => {
          const { contentOffset, contentSize, layoutMeasurement } =
            e.nativeEvent;
          atBottom.current =
            contentOffset.y + layoutMeasurement.height >=
            contentSize.height - 100;
        }}
        onContentSizeChange={() => {
          if (atBottom.current)
            scroll.current?.scrollToEnd({ animated: false });
        }}
      >
        <ScreenTitle
          eyebrow="Work"
          title={current?.title ?? "Ask your workspace"}
          subtitle={
            current
              ? undefined
              : "Turn your experience, saved notes, and work context into a useful next step."
          }
          action={
            <Row>
              <IconButton
                label="Saved conversations"
                icon="history"
                disabled={busy}
                onPress={() => {
                  setPanel("conversations");
                  void refresh();
                }}
              />
              <IconButton
                label="New conversation"
                icon="plus"
                disabled={busy}
                onPress={newConversation}
              />
              {current ? (
                <IconButton
                  label="Conversation options"
                  icon="more"
                  disabled={busy}
                  onPress={() => {
                    setTitle(current.title);
                    setPanel("manage");
                  }}
                />
              ) : null}
            </Row>
          }
        />
        {loading ? (
          <Loading label="Opening conversation…" />
        ) : !current?.messages?.length ? (
          <>
            <View style={{ gap: 10, paddingVertical: 4 }}>
              <Txt style={{ fontSize: 17, fontWeight: "600" }}>
                What would help today?
              </Txt>
              {[
                "Help me prepare for my next interview",
                "Find examples of my impact for a resume",
                "Turn my recent call notes into next steps",
              ].map((text) => (
                <Pressable
                  key={text}
                  accessibilityRole="button"
                  onPress={() => setDraft(text)}
                  style={({ pressed }) => ({
                    padding: 16,
                    borderRadius: 14,
                    borderWidth: 1,
                    borderColor: c.line,
                    backgroundColor: pressed ? c.soft : c.card,
                    flexDirection: "row",
                    gap: 12,
                    alignItems: "center",
                  })}
                >
                  <Txt style={{ flex: 1, fontSize: 14 }}>{text}</Txt>
                  <Icon name="arrow" size={18} />
                </Pressable>
              ))}
            </View>
            {list.length ? (
              <Txt muted style={{ fontSize: 12 }}>
                {list.length} saved conversation{list.length === 1 ? "" : "s"} ·
                open the history icon to continue one.
              </Txt>
            ) : null}
          </>
        ) : (
          current.messages.map((message) => (
            <View key={message.id} style={{ gap: 13, paddingBottom: 14 }}>
              <View
                style={{
                  backgroundColor: c.tint,
                  borderRadius: 17,
                  padding: 16,
                  alignSelf: "flex-end",
                  maxWidth: "94%",
                  borderBottomRightRadius: 5,
                }}
              >
                <Txt selectable style={{ fontWeight: "600" }}>
                  {message.question}
                </Txt>
              </View>
              <View style={{ gap: 13, paddingHorizontal: 3 }}>
                <Row>
                  <Icon name="work" size={18} color={c.ruby} />
                  <Txt style={{ fontWeight: "700", fontSize: 12 }}>
                    CAREER PILOT
                  </Txt>
                  {message.status === "pending" ? (
                    <Badge label="Thinking" tone="ruby" />
                  ) : null}
                </Row>
                {message.answer ? (
                  <Txt selectable>{message.answer}</Txt>
                ) : message.status === "pending" ? (
                  <Loading label="Preparing your answer…" />
                ) : (
                  <Notice tone={message.status === "failed" ? "error" : "info"}>
                    {message.error ||
                      (message.status === "cancelled"
                        ? "Generation stopped."
                        : "No answer was returned.")}
                  </Notice>
                )}
                {message.status === "done" && message.sources.length ? (
                  <View style={{ gap: 7 }}>
                    <Txt muted style={{ fontSize: 11, fontWeight: "600" }}>
                      SOURCES
                    </Txt>
                    {message.sources.map((s, i) => (
                      <Button
                        key={`${s.kind}:${s.id}:${i}`}
                        label={`${s.citation ?? `Source ${i + 1}`} · ${s.label}`}
                        compact
                        secondary
                        icon="file"
                        onPress={() => void openSource(s)}
                      />
                    ))}
                  </View>
                ) : null}
                {message.status === "pending" ? (
                  <Button
                    label="Stop generation"
                    compact
                    secondary
                    icon="stop"
                    disabled={busy}
                    onPress={() => void control(message, "cancel")}
                  />
                ) : ["failed", "cancelled"].includes(message.status) ? (
                  <Button
                    label="Retry answer"
                    compact
                    secondary
                    icon="refresh"
                    disabled={busy}
                    onPress={() => void control(message, "retry")}
                  />
                ) : null}
              </View>
            </View>
          ))
        )}
      </ScrollView>
      <View
        style={{
          borderTopWidth: 1,
          borderColor: c.line,
          backgroundColor: c.card,
        }}
      >
        <View style={[page, { paddingTop: 10, paddingBottom: 14, gap: 9 }]}>
          {error ? (
            <Notice
              tone="error"
              action={
                selected.current && !current && !busy ? (
                  <Button
                    label="Retry opening conversation"
                    secondary
                    onPress={() => void load(selected.current!, false)}
                  />
                ) : undefined
              }
            >
              {error}
            </Notice>
          ) : null}
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Context for next question: ${contextLabel}`}
            accessibilityState={{ disabled: busy }}
            disabled={busy}
            onPress={() => setPanel("context")}
            style={{
              flexDirection: "row",
              alignItems: "center",
              gap: 7,
              minHeight: 44,
              alignSelf: "flex-start",
              maxWidth: "100%",
              borderRadius: 10,
              paddingHorizontal: 10,
              backgroundColor: c.soft,
              opacity: busy ? 0.5 : 1,
            }}
          >
            <Icon name="settings" size={16} />
            <Txt
              muted
              numberOfLines={1}
              style={{ fontSize: 13, flexShrink: 1 }}
            >
              Context · {contextLabel}
              {from || to ? " · date filter" : ""}
            </Txt>
            <Icon name="chevron" size={13} />
          </Pressable>
          <View
            style={{ flexDirection: "row", alignItems: "flex-end", gap: 9 }}
          >
            <Input
              multiline
              placeholder={current ? "Ask a follow-up…" : "Ask Career Pilot…"}
              value={draft}
              editable={!busy}
              onChangeText={(v) => {
                setDraft(v);
                operation.current = null;
              }}
              maxLength={8000}
              style={{ flex: 1, maxHeight: 150 }}
            />
            <Button
              label={operation.current && !busy ? "Retry send" : "Send"}
              icon="arrow"
              compact
              loading={busy}
              disabled={!!pending || loading || !draft.trim()}
              onPress={() => void send()}
            />
          </View>
        </View>
      </View>
      <Modal
        visible={panel !== null}
        animationType="slide"
        onRequestClose={() => {
          if (!busy) setPanel(null);
        }}
      >
        <SafeAreaView style={{ flex: 1, backgroundColor: c.bg }}>
          <ScrollView
            keyboardShouldPersistTaps="handled"
            contentContainerStyle={page}
          >
            <ScreenTitle
              title={
                panel === "context"
                  ? "Choose your context"
                  : panel === "manage"
                    ? "Conversation options"
                    : "Saved conversations"
              }
              action={
                <IconButton
                  label="Close options"
                  icon="close"
                  disabled={busy}
                  onPress={() => setPanel(null)}
                />
              }
            />
            {error ? <Notice tone="error">{error}</Notice> : null}
            {panel === "context" ? (
              <>
                <Select
                  label="Use for the next question"
                  value={kind}
                  options={contextChoices}
                  onChange={(value) => {
                    setKind(value as ScopeKind);
                    setChoice("");
                    operation.current = null;
                    setError("");
                  }}
                />
                {needsItem(kind) ? (
                  optionsLoading ? (
                    <Loading label="Loading your context…" />
                  ) : (
                    <Select
                      label={`Choose ${kind === "call" ? "a saved call" : `a ${kind}`}`}
                      value={choice}
                      options={options.map((o) => ({
                        value: o.id,
                        label: o.label,
                      }))}
                      onChange={(value) => {
                        setChoice(value);
                        operation.current = null;
                      }}
                      placeholder={
                        options.length ? "Choose…" : "No available items"
                      }
                    />
                  )
                ) : null}
                <Card>
                  <Txt style={{ fontWeight: "600" }}>
                    Date range <Txt muted>(optional)</Txt>
                  </Txt>
                  <Field label="From">
                    <Input
                      placeholder="YYYY-MM-DD"
                      value={from}
                      onChangeText={(value) => {
                        setFrom(value);
                        operation.current = null;
                      }}
                      maxLength={10}
                      autoCorrect={false}
                    />
                  </Field>
                  <Field label="To">
                    <Input
                      placeholder="YYYY-MM-DD"
                      value={to}
                      onChangeText={(value) => {
                        setTo(value);
                        operation.current = null;
                      }}
                      maxLength={10}
                      autoCorrect={false}
                    />
                  </Field>
                </Card>
                <Notice>
                  {current?.retrieval_notice ??
                    "Answers can use records available to this profile. A specific item narrows the search."}
                </Notice>
                <Txt muted style={{ fontSize: 12 }}>
                  {current?.retention_notice ??
                    "Saved answers and source excerpts remain in this conversation until you delete it."}
                </Txt>
                <Button
                  label="Use this context"
                  onPress={() => {
                    try {
                      makeScope(kind, choice, from, to);
                      setError("");
                      setPanel(null);
                    } catch (e) {
                      setError(String(e));
                    }
                  }}
                />
              </>
            ) : panel === "manage" ? (
              <>
                <Field label="Conversation title">
                  <Input
                    value={title}
                    onChangeText={setTitle}
                    maxLength={160}
                  />
                </Field>
                <Button
                  label="Save title"
                  loading={busy}
                  disabled={!title.trim()}
                  onPress={() => void manage(false)}
                />
                <Card>
                  <Txt muted>
                    Deleting this conversation removes its saved questions,
                    answers and source excerpts.
                  </Txt>
                  <Button
                    label="Delete conversation"
                    secondary
                    danger
                    disabled={busy}
                    onPress={() =>
                      Alert.alert(
                        "Delete conversation?",
                        "This permanently removes its questions, answers and source excerpts.",
                        [
                          { text: "Cancel", style: "cancel" },
                          {
                            text: "Delete",
                            style: "destructive",
                            onPress: () => void manage(true),
                          },
                        ],
                      )
                    }
                  />
                </Card>
              </>
            ) : (
              <>
                <Button
                  label="New conversation"
                  icon="plus"
                  onPress={newConversation}
                />
                {listLoading ? (
                  <Loading label="Loading conversations…" />
                ) : !list.length ? (
                  <EmptyState
                    icon="work"
                    title="Your conversations belong here"
                    description="Ask your first question in Work. You can come back to it whenever you need."
                  />
                ) : (
                  list.map((conversation) => (
                    <Pressable
                      key={conversation.id}
                      accessibilityRole="button"
                      onPress={() => {
                        setPanel(null);
                        void load(conversation.id);
                      }}
                    >
                      <Card style={{ gap: 6 }}>
                        <Txt style={{ fontWeight: "600" }}>
                          {conversation.title}
                        </Txt>
                        <Txt muted style={{ fontSize: 12 }}>
                          {
                            contextChoices.find(
                              (o) => o.value === conversation.scope.kind,
                            )?.label
                          }
                        </Txt>
                      </Card>
                    </Pressable>
                  ))
                )}
              </>
            )}
          </ScrollView>
        </SafeAreaView>
      </Modal>
      <Modal
        visible={!!source || sourceLoading}
        animationType="slide"
        onRequestClose={closeSource}
      >
        <SafeAreaView style={{ flex: 1, backgroundColor: c.bg }}>
          <ScrollView contentContainerStyle={page}>
            <ScreenTitle
              title={source?.label ?? "Opening source"}
              action={
                <IconButton
                  label="Close source"
                  icon="close"
                  onPress={closeSource}
                />
              }
            />
            {sourceLoading ? (
              <Loading label="Checking source access…" />
            ) : (
              <>
                {source?.date ? <Txt muted>{source.date}</Txt> : null}
                {source?.start_ms !== undefined ? (
                  <Badge
                    label={`Transcript · ${Math.floor(source.start_ms / 60000)}:${String(Math.floor(source.start_ms / 1000) % 60).padStart(2, "0")}`}
                  />
                ) : null}
                <Txt selectable>
                  {source?.content ??
                    source?.excerpt ??
                    "Source has no retained excerpt."}
                </Txt>
              </>
            )}
          </ScrollView>
        </SafeAreaView>
      </Modal>
    </View>
  );
}
