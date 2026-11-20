import React, { useEffect, useRef, useState, useContext } from "react";
import { Alert, Modal, ScrollView, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { randomUUID } from "expo-crypto";
import { Api } from "./api";
import { Button, Card, Input, Row, Txt, page, Theme } from "./ui";
type Scope = {
  kind: string;
  id?: string;
  from_date?: string;
  to_date?: string;
};
type Source = {
  kind: string;
  id: string;
  label: string;
  citation?: string;
  start_ms?: number;
};
type Message = {
  id: string;
  question: string;
  answer: string;
  status: string;
  error?: string;
  sources: Source[];
};
type Conversation = {
  id: string;
  title: string;
  scope: Scope;
  messages?: Message[];
  retrieval_notice?: string;
  retention_notice?: string;
};
export function Ask({ api }: { api: Api }) {
  const c = useContext(Theme);
  const [list, setList] = useState<Conversation[]>([]),
    [current, setCurrent] = useState<Conversation | null>(null),
    [draft, setDraft] = useState(""),
    [kind, setKind] = useState("auto"),
    [choice, setChoice] = useState(""),
    [options, setOptions] = useState<{ id: string; label: string }[]>([]),
    [from, setFrom] = useState(""),
    [to, setTo] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [source, setSource] = useState<{
      label: string;
      content?: string;
      excerpt?: string;
      start_ms?: number;
      date?: string;
    } | null>(null);
  const operation = useRef<{
      request_id: string;
      question: string;
      scope: Scope;
    } | null>(null),
    selected = useRef<string | null>(null),
    sourceEpoch = useRef(0);
  function closeSource() {
    sourceEpoch.current++;
    setSource(null);
  }
  async function refresh() {
    try {
      setList(
        (
          await api.request<{ conversations: Conversation[] }>(
            "/ask/conversations",
          )
        ).conversations,
      );
    } catch (e) {
      setError(String(e));
    }
  }
  async function load(id: string) {
    closeSource();
    selected.current = id;
    try {
      const c = await api.request<Conversation>(`/ask/conversations/${id}`);
      if (selected.current !== id) return;
      setCurrent(c);
      setKind(c.scope.kind);
      setChoice(c.scope.id ?? "");
      setFrom(c.scope.from_date ?? "");
      setTo(c.scope.to_date ?? "");
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    void refresh();
    return () => {
      sourceEpoch.current++;
      setSource(null);
      selected.current = null;
    };
  }, [api]);
  useEffect(() => {
    let alive = true;
    setOptions([]);
    if (["company", "application", "document", "call"].includes(kind))
      void api
        .request<{ options: { id: string; label: string }[] }>(
          `/ask/context-options?kind=${kind}`,
        )
        .then((d) => {
          if (alive) setOptions(d.options);
        })
        .catch((e) => setError(String(e)));
    return () => {
      alive = false;
    };
  }, [kind, api]);
  const pending = current?.messages?.find((m) => m.status === "pending");
  useEffect(() => {
    if (!pending || !current) return;
    const id = current.id;
    const t = setInterval(() => {
      void api
        .request<Conversation>(`/ask/conversations/${id}`)
        .then((d) => {
          if (selected.current === id) setCurrent(d);
        })
        .catch((e) => setError(String(e)));
    }, 1500);
    return () => clearInterval(t);
  }, [pending?.id, current?.id, api]);
  function scope(): Scope {
    if (
      ["company", "application", "document", "call"].includes(kind) &&
      !choice
    )
      throw new Error("Choose the context first.");
    for (const value of [from, to])
      if (value && !/^\d{4}-\d{2}-\d{2}$/.test(value))
        throw new Error("Use YYYY-MM-DD dates.");
    return {
      kind,
      ...(choice ? { id: choice } : {}),
      ...(from ? { from_date: from } : {}),
      ...(to ? { to_date: to } : {}),
    };
  }
  async function send() {
    if (!draft.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const body = operation.current ?? {
        request_id: randomUUID(),
        question: draft.trim(),
        scope: scope(),
      };
      operation.current = body;
      let c = current;
      if (!c) {
        c = await api.request<Conversation>(
          "/ask/conversations",
          { title: body.question.slice(0, 100), scope: body.scope },
          "POST",
        );
        selected.current = c.id;
        setCurrent(c);
      }
      await api.request(`/ask/conversations/${c.id}/messages`, body, "POST");
      operation.current = null;
      setDraft("");
      await load(c.id);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function control(m: Message, action: string) {
    try {
      await api.request(
        `/ask/conversations/${current!.id}/messages/${m.id}/${action}`,
        {},
        "POST",
      );
      await load(current!.id);
    } catch (e) {
      setError(String(e));
    }
  }
  async function remove() {
    if (!current) return;
    try {
      await api.request(
        `/ask/conversations/${current.id}`,
        undefined,
        "DELETE",
      );
      sourceEpoch.current++;
      setSource(null);
      selected.current = null;
      setCurrent(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }
  async function open(s: Source) {
    const ticket = ++sourceEpoch.current;
    const conversation = selected.current;
    setSource(null);
    try {
      const result = await api.request<NonNullable<typeof source>>(
        `/ask/sources/${s.kind}/${encodeURIComponent(s.id)}`,
      );
      if (ticket === sourceEpoch.current && conversation === selected.current)
        setSource(result);
    } catch (e) {
      if (ticket === sourceEpoch.current)
        setError(`Source unavailable or access changed. ${e}`);
    }
  }
  return (
    <View style={{ flex: 1 }}>
      <ScrollView
        keyboardShouldPersistTaps="handled"
        contentContainerStyle={page}
      >
        <Txt title>Ask Career Pilot</Txt>
        <Txt muted>
          Ask generally or use your saved career context. Sources stay visible
          alongside each answer.
        </Txt>
        <Row>
          <Button
            label="New conversation"
            secondary
            onPress={() => {
              sourceEpoch.current++;
              setSource(null);
              selected.current = null;
              setCurrent(null);
              operation.current = null;
              setDraft("");
            }}
          />
          {current ? (
            <Button
              label="Delete conversation"
              secondary
              onPress={() =>
                Alert.alert(
                  "Delete conversation?",
                  "This deletes its saved questions, answers and source excerpts.",
                  [
                    { text: "Cancel", style: "cancel" },
                    {
                      text: "Delete",
                      style: "destructive",
                      onPress: () => void remove(),
                    },
                  ],
                )
              }
            />
          ) : null}
        </Row>
        {!current
          ? list.map((c) => (
              <Button
                key={c.id}
                label={c.title}
                secondary
                onPress={() => void load(c.id)}
              />
            ))
          : null}
        <Txt>Context for the next question</Txt>
        <Row>
          {[
            "auto",
            "general",
            "all",
            "company",
            "application",
            "document",
            "call",
          ].map((k) => (
            <Button
              key={k}
              label={`${kind === k ? "✓ " : ""}${k}`}
              secondary
              onPress={() => {
                setKind(k);
                setChoice("");
                operation.current = null;
              }}
            />
          ))}
        </Row>
        {options.map((o) => (
          <Button
            key={o.id}
            label={`${choice === o.id ? "✓ " : ""}${o.label}`}
            secondary
            onPress={() => {
              setChoice(o.id);
              operation.current = null;
            }}
          />
        ))}
        <Input
          placeholder="From date YYYY-MM-DD (optional)"
          value={from}
          onChangeText={setFrom}
        />
        <Input
          placeholder="To date YYYY-MM-DD (optional)"
          value={to}
          onChangeText={setTo}
        />
        {current?.retrieval_notice ? (
          <Txt muted>{current.retrieval_notice}</Txt>
        ) : null}
        {current?.messages?.map((m) => (
          <Card key={m.id}>
            <Txt style={{ fontWeight: "700" }}>{m.question}</Txt>
            <Txt selectable>
              {m.answer ||
                (m.status === "pending"
                  ? "Preparing answer…"
                  : m.error || m.status)}
            </Txt>
            {m.sources.map((s, i) => (
              <Button
                key={`${s.kind}:${s.id}:${i}`}
                label={`${s.citation ?? `Source ${i + 1}`}: ${s.label}`}
                secondary
                onPress={() => void open(s)}
              />
            ))}
            {m.status === "pending" ? (
              <Button
                label="Stop generation"
                secondary
                onPress={() => void control(m, "cancel")}
              />
            ) : ["failed", "cancelled"].includes(m.status) ? (
              <Button
                label="Retry answer"
                secondary
                onPress={() => void control(m, "retry")}
              />
            ) : null}
          </Card>
        ))}
        {current?.retention_notice ? (
          <Txt muted>{current.retention_notice}</Txt>
        ) : null}
        {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
        <Input
          multiline
          placeholder="Ask a question or follow-up"
          value={draft}
          onChangeText={(v) => {
            setDraft(v);
            operation.current = null;
          }}
          maxLength={8000}
        />
        <Button
          label={
            busy
              ? "Sending…"
              : operation.current
                ? "Retry same request"
                : "Send"
          }
          disabled={busy || !!pending || !draft.trim()}
          onPress={() => void send()}
        />
      </ScrollView>
      <Modal
        visible={!!source}
        animationType="slide"
        onRequestClose={closeSource}
      >
        <SafeAreaView style={{ flex: 1, backgroundColor: c.bg }}>
          <ScrollView contentContainerStyle={page}>
            <Button label="Close source" onPress={closeSource} />
            <Txt title>{source?.label}</Txt>
            {source?.date ? <Txt muted>{source.date}</Txt> : null}
            {source?.start_ms !== undefined ? (
              <Txt muted>
                Transcript offset: {Math.floor(source.start_ms / 1000)} seconds
              </Txt>
            ) : null}
            <Txt selectable>
              {source?.content ??
                source?.excerpt ??
                "Source has no retained excerpt."}
            </Txt>
          </ScrollView>
        </SafeAreaView>
      </Modal>
    </View>
  );
}
