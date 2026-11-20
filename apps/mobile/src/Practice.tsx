import React, { useEffect, useRef, useState } from "react";
import { Alert, AppState, ScrollView, View } from "react-native";
import {
  AudioModule,
  RecordingPresets,
  setAudioModeAsync,
  useAudioPlayer,
  useAudioRecorder,
  useAudioRecorderState,
} from "expo-audio";
import * as FS from "expo-file-system/legacy";
import { randomUUID } from "expo-crypto";
import { Api } from "./api";
import { MediaEpoch } from "./mediaEpoch";
import { Button, Card, Input, Row, Txt, page } from "./ui";
type Feedback = {
  rubric_version: string;
  dimensions: Record<string, number>;
  strengths: string[];
  improvements: string[];
  excerpts: string[];
  uncertainty: string[];
  suggested_wording: string;
};
type Question = {
  id: string;
  text: string;
  kind: string;
  hint?: string;
  sample?: string;
  saved_for_later: boolean;
  attempts: {
    id: string;
    answer: string;
    assistance: string[];
    feedback?: Feedback;
    comparison?: {
      note: string;
      comparable: boolean;
      dimensions: Record<string, { previous: number; current: number }>;
      summary?: string;
      improvements?: string[];
      remaining_gaps?: string[];
      recurring_gaps?: string[];
      evidence?: { previous: string[]; current: string[] };
    };
  }[];
};
type Session = {
  id: string;
  status: string;
  setup: { topic: string; duration_minutes: number; spoken_language?: string };
  questions: Question[];
  current_question_id?: string;
  elapsed_seconds: number;
  error?: string;
  voice_available?: boolean;
  speech_available?: boolean;
  retention_notice?: string;
};
type Entry = { id: string; status: string; setup: Session["setup"] };
export type ImportedQuestion = {
  session_id: string;
  channel: "them" | "me";
  seg_id: number;
};
export function Practice({
  api,
  imported,
}: {
  api: Api;
  imported?: ImportedQuestion;
}) {
  const [list, setList] = useState<Entry[]>([]),
    [session, setSession] = useState<Session | null>(null),
    [topic, setTopic] = useState("General interview preparation"),
    [type, setType] = useState("behavioral"),
    [minutes, setMinutes] = useState("15"),
    [language, setLanguage] = useState(""),
    [languages, setLanguages] = useState<{ id: string; name: string }[]>([]),
    [apps, setApps] = useState<{ id: string; label: string }[]>([]),
    [app, setApp] = useState(""),
    [context, setContext] = useState<
      { label: string; excerpt: string }[] | null
    >(null),
    [reviewed, setReviewed] = useState(false),
    [draft, setDraft] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const recorder = useAudioRecorder(RecordingPresets.HIGH_QUALITY),
    recording = useAudioRecorderState(recorder),
    player = useAudioPlayer(null);
  const media = useRef(new MediaEpoch());
  const files = useRef(new Set<string>()),
    alive = useRef(true),
    currentId = useRef<string | null>(null),
    operation = useRef<{
      request_id: string;
      action: string;
      question_id?: string;
      answer?: string;
    } | null>(null),
    setupId = useRef(randomUUID());
  const q = session?.questions.find(
    (q) => q.id === session.current_question_id,
  );
  async function refresh() {
    try {
      setList(
        (await api.request<{ sessions: Entry[] }>("/practice/sessions"))
          .sessions,
      );
    } catch (e) {
      setError(String(e));
    }
  }
  async function load(id: string) {
    currentId.current = id;
    try {
      const s = await api.request<Session>(`/practice/sessions/${id}`);
      if (alive.current && currentId.current === id) setSession(s);
    } catch (e) {
      setError(String(e));
    }
  }
  async function clearMedia() {
    media.current.stop();
    player.pause();
    await recorder.stop().catch(() => {});
    if (recorder.uri) files.current.add(recorder.uri);
    await Promise.all(
      [...files.current].map((uri) =>
        FS.deleteAsync(uri, { idempotent: true }).catch(() => {}),
      ),
    );
    files.current.clear();
    await setAudioModeAsync({
      allowsRecording: false,
      playsInSilentMode: true,
    }).catch(() => {});
  }
  useEffect(() => {
    alive.current = true;
    void refresh();
    void api
      .request<{ options: { id: string; label: string }[] }>(
        "/ask/context-options?kind=application",
      )
      .then((d) => setApps(d.options))
      .catch((e) => setError(String(e)));
    void api
      .request<{ items: { id: string; name: string }[] }>("/call-languages")
      .then((d) => setLanguages(d.items))
      .catch(() => {});
    const sub = AppState.addEventListener("change", (state) => {
      if (state !== "active") {
        void clearMedia();
        setError(
          "Audio stopped while the app was away. Resume when you are ready.",
        );
      }
    });
    return () => {
      alive.current = false;
      currentId.current = null;
      sub.remove();
      void clearMedia();
    };
  }, [api]);
  useEffect(() => {
    setReviewed(false);
    setContext(null);
    setupId.current = randomUUID();
  }, [topic, type, minutes, language, app]);
  useEffect(() => {
    if (!session || !["preparing", "processing"].includes(session.status))
      return;
    const id = session.id;
    const t = setInterval(() => void load(id), 1500);
    return () => clearInterval(t);
  }, [session?.id, session?.status]);
  useEffect(() => {
    if (recording.isRecording && recording.durationMillis > 240000) {
      void clearMedia();
      setError(
        "Recording reached four minutes. Please use a shorter response or type your answer.",
      );
    }
  }, [recording.durationMillis]);
  async function preview() {
    const setup = setupId.current;
    try {
      const d = await api.request<{
        sources: { label: string; excerpt: string }[];
      }>(`/practice/context${app ? `?application_id=${app}` : ""}`);
      if (setup !== setupId.current || !alive.current) return;
      setContext(d.sources);
      setReviewed(false);
    } catch (e) {
      setError(String(e));
    }
  }
  async function start() {
    setBusy(true);
    try {
      const duration = Number(minutes);
      if (!Number.isInteger(duration) || duration < 3 || duration > 60)
        throw new Error("Choose 3–60 minutes.");
      const s = await api.request<Session>(
        "/practice/sessions",
        {
          request_id: setupId.current,
          topic,
          interview_type: type,
          duration_minutes: duration,
          ...(app ? { application_id: app } : {}),
          ...(language ? { spoken_language: language } : {}),
          ...(imported ? { source_question: imported } : {}),
          retain_audio: false,
        },
        "POST",
      );
      currentId.current = s.id;
      setSession(s);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function act(action: string, question_id = q?.id) {
    if (!session) return;
    setBusy(true);
    try {
      await clearMedia();
      const body = operation.current ?? {
        request_id: randomUUID(),
        action,
        ...(question_id ? { question_id } : {}),
        ...(action === "answer" ? { answer: draft } : {}),
      };
      operation.current = body;
      await api.request(
        `/practice/sessions/${session.id}/actions`,
        body,
        "POST",
      );
      operation.current = null;
      if (action === "answer") setDraft("");
      await load(session.id);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function record() {
    if (busy || recorder.isRecording) return;
    const ticket = media.current.begin();
    setBusy(true);
    try {
      player.pause();
      const permission = await AudioModule.requestRecordingPermissionsAsync();
      if (!permission.granted)
        throw new Error(
          "Microphone permission is unavailable. Type your answer or enable permission in device settings.",
        );
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      )
        return;
      await setAudioModeAsync({
        allowsRecording: true,
        playsInSilentMode: true,
        shouldPlayInBackground: false,
        interruptionMode: "doNotMix",
      });
      await recorder.prepareToRecordAsync();
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      ) {
        await clearMedia();
        return;
      }
      recorder.record();
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function transcribe() {
    if (!session) return;
    setBusy(true);
    try {
      await recorder.stop();
      const uri = recorder.uri;
      if (!uri) throw new Error("No microphone recording was captured.");
      files.current.add(uri);
      const info = await FS.getInfoAsync(uri);
      if (info.exists && info.size > 8 * 1024 * 1024)
        throw new Error("Recording is too large; use a shorter answer.");
      const audio = await FS.readAsStringAsync(uri, {
        encoding: FS.EncodingType.Base64,
      });
      await FS.deleteAsync(uri, { idempotent: true });
      files.current.delete(uri);
      await setAudioModeAsync({
        allowsRecording: false,
        playsInSilentMode: true,
      });
      const result = await api.request<{ text: string; silence: boolean }>(
        `/practice/sessions/${session.id}/transcribe-json`,
        {
          request_id: randomUUID(),
          content_type: "audio/mp4",
          audio_base64: audio,
        },
        "POST",
      );
      if (alive.current) {
        setDraft(result.text);
        setError(
          result.silence
            ? "No speech detected. Try again or type your answer."
            : "Review the transcript, then submit your answer.",
        );
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function speak() {
    if (!q || !session) return;
    setBusy(true);
    try {
      await clearMedia();
      const ticket = media.current.begin();
      const d = await api.request<{ audio_base64: string }>(
        `/practice/sessions/${session.id}/questions/${q.id}/speech-json`,
      );
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      )
        return;
      const uri = `${FS.cacheDirectory}practice-${randomUUID()}.mp3`;
      files.current.add(uri);
      await FS.writeAsStringAsync(uri, d.audio_base64, {
        encoding: FS.EncodingType.Base64,
      });
      if (
        !media.current.current(ticket) ||
        !alive.current ||
        AppState.currentState !== "active"
      ) {
        await FS.deleteAsync(uri, { idempotent: true });
        return;
      }
      player.replace({ uri });
      player.play();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!session) return;
    try {
      await clearMedia();
      await api.request(
        `/practice/sessions/${session.id}`,
        undefined,
        "DELETE",
      );
      currentId.current = null;
      setSession(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }
  return (
    <ScrollView
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={page}
    >
      <Txt title>Interview practice</Txt>
      {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
      {!session ? (
        <>
          <Txt muted>
            One question at a time, grounded in the selected profile. Raw audio
            is discarded after transcription.
          </Txt>
          {imported ? (
            <Txt>Starting from a question in your saved call.</Txt>
          ) : null}
          <Input
            placeholder="Practice topic"
            value={topic}
            onChangeText={setTopic}
          />
          <Row>
            {["recruiter", "behavioral", "technical", "system_design"].map(
              (t) => (
                <Button
                  key={t}
                  label={`${type === t ? "✓ " : ""}${t.replace("_", " ")}`}
                  secondary
                  onPress={() => setType(t)}
                />
              ),
            )}
          </Row>
          <Input
            placeholder="Minutes (3–60)"
            keyboardType="number-pad"
            value={minutes}
            onChangeText={setMinutes}
          />
          <Txt>Spoken language</Txt>
          <Row>
            <Button
              label="Profile default"
              secondary
              onPress={() => setLanguage("")}
            />
            {languages.map((l) => (
              <Button
                key={l.id}
                label={`${language === l.id ? "✓ " : ""}${l.name}`}
                secondary
                onPress={() => setLanguage(l.id)}
              />
            ))}
          </Row>
          <Txt>Application context</Txt>
          <Button
            label="General profile context"
            secondary
            onPress={() => setApp("")}
          />
          {apps.map((a) => (
            <Button
              key={a.id}
              label={`${app === a.id ? "✓ " : ""}${a.label}`}
              secondary
              onPress={() => setApp(a.id)}
            />
          ))}
          <Button
            label="Review context before starting"
            secondary
            onPress={() => void preview()}
          />
          {context?.map((s, i) => (
            <Card key={i}>
              <Txt>{s.label}</Txt>
              <Txt muted>{s.excerpt}</Txt>
            </Card>
          ))}
          {context ? (
            <>
              <Txt muted>
                Correct source records in desktop Interview prep or Work before
                continuing.
              </Txt>
              <Button
                label={
                  reviewed ? "Context reviewed" : "I reviewed this context"
                }
                onPress={() => setReviewed(true)}
              />
            </>
          ) : null}
          <Button
            label="Start practice"
            disabled={!reviewed || busy}
            onPress={() => void start()}
          />
          <Txt title>Previous practice</Txt>
          {list.map((s) => (
            <Button
              key={s.id}
              label={`${s.setup.topic} · ${s.status}`}
              secondary
              onPress={() => void load(s.id)}
            />
          ))}
        </>
      ) : (
        <>
          <Txt>
            {session.setup.topic} · {session.status} ·{" "}
            {Math.floor(session.elapsed_seconds / 60)} /{" "}
            {session.setup.duration_minutes} min
          </Txt>
          <Row>
            <Button
              label="History"
              secondary
              onPress={() => {
                void clearMedia();
                setSession(null);
                currentId.current = null;
                void refresh();
              }}
            />
            <Button
              label="Delete practice"
              secondary
              onPress={() =>
                Alert.alert(
                  "Delete practice?",
                  "This removes saved responses, feedback and generated questions.",
                  [
                    { text: "Cancel" },
                    {
                      text: "Delete",
                      style: "destructive",
                      onPress: () => void remove(),
                    },
                  ],
                )
              }
            />
          </Row>
          {q ? (
            <Card>
              <Txt title>{q.text}</Txt>
              <Txt muted>
                {q.kind === "hypothetical"
                  ? "Hypothetical scenario"
                  : q.kind === "original"
                    ? "Original saved call question"
                    : "Experience question"}
              </Txt>
              {q.hint ? <Txt>Hint: {q.hint}</Txt> : null}
              {q.sample ? <Txt>Sample (coached): {q.sample}</Txt> : null}
            </Card>
          ) : null}
          {session.status === "answering" ? (
            <>
              <Row>
                <Button
                  label="Read question aloud"
                  secondary
                  disabled={
                    !session.speech_available || busy || recording.isRecording
                  }
                  onPress={() => void speak()}
                />
                <Button
                  label="Stop audio"
                  secondary
                  onPress={() => player.pause()}
                />
              </Row>
              {!session.speech_available ? (
                <Txt muted>
                  Voice playback unavailable for this language; text practice is
                  ready.
                </Txt>
              ) : null}
              <Input
                multiline
                placeholder="Your answer"
                value={draft}
                onChangeText={(v) => {
                  setDraft(v);
                  operation.current = null;
                }}
                maxLength={16000}
              />
              <Button
                label={
                  recording.isRecording
                    ? "I'm done — transcribe"
                    : "Answer with microphone"
                }
                disabled={busy || !session.voice_available}
                onPress={() =>
                  void (recording.isRecording ? transcribe() : record())
                }
              />
              <Button
                label="Submit answer"
                disabled={busy || recording.isRecording || !draft.trim()}
                onPress={() => void act("answer")}
              />
              <Row>
                {["skip", "hint", "sample", "pause", "finish"].map((a) => (
                  <Button
                    key={a}
                    label={a}
                    secondary
                    disabled={busy}
                    onPress={() => void act(a)}
                  />
                ))}
              </Row>
            </>
          ) : session.status === "paused" ? (
            <Button
              label="Resume"
              disabled={busy}
              onPress={() => void act("resume")}
            />
          ) : session.status === "failed" ? (
            <Button
              label="Retry generation"
              disabled={busy}
              onPress={() => void act("retry_generation")}
            />
          ) : ["processing", "preparing"].includes(session.status) ? (
            <Button
              label="Pause generation"
              secondary
              onPress={() => void act("pause")}
            />
          ) : null}
          {operation.current ? (
            <Button
              label="Retry same operation"
              onPress={() =>
                void act(
                  operation.current!.action,
                  operation.current!.question_id,
                )
              }
            />
          ) : null}
          {session.error ? <Txt>{session.error}</Txt> : null}
          <Txt title>Questions and feedback</Txt>
          {session.questions.map((item) => (
            <Card key={item.id}>
              <Txt>{item.text}</Txt>
              <Row>
                <Button
                  label={
                    item.saved_for_later ? "Saved for later" : "Save for later"
                  }
                  secondary
                  disabled={busy}
                  onPress={() => void act("save", item.id)}
                />
                <Button
                  label="Retry this question"
                  secondary
                  disabled={
                    busy || ["preparing", "processing"].includes(session.status)
                  }
                  onPress={() => void act("retry", item.id)}
                />
              </Row>
              {item.attempts.map((a, index) => (
                <View key={a.id} style={{ gap: 8 }}>
                  <Txt style={{ fontWeight: "700" }}>
                    Attempt {index + 1} ·{" "}
                    {a.assistance.length
                      ? a.assistance.join(", ")
                      : "Independent"}
                  </Txt>
                  <Txt selectable>{a.answer}</Txt>
                  {a.feedback ? (
                    <>
                      <Txt muted>
                        Rubric {a.feedback.rubric_version}; 0 not demonstrated,
                        1 limited, 2 developing, 3 clear, 4 strong.
                      </Txt>
                      {Object.entries(a.feedback.dimensions).map(
                        ([key, value]) => (
                          <Txt key={key}>
                            {key}: {value}/4
                          </Txt>
                        ),
                      )}
                      <Txt>Strengths: {a.feedback.strengths.join("\n")}</Txt>
                      <Txt>Improve: {a.feedback.improvements.join("\n")}</Txt>
                      <Txt>Evidence: {a.feedback.excerpts.join("\n")}</Txt>
                      <Txt>Limits: {a.feedback.uncertainty.join("\n")}</Txt>
                      <Txt>
                        Suggested wording: {a.feedback.suggested_wording}
                      </Txt>
                      {a.comparison ? (
                        <>
                          <Txt>
                            {a.comparison.note}{" "}
                            {a.comparison.comparable
                              ? "Comparable coaching conditions."
                              : "Coaching differs."}
                          </Txt>
                          <Txt>{a.comparison.summary}</Txt>
                          {a.comparison.improvements?.map((x, i) => (
                            <Txt key={`improve-${i}`}>Improved: {x}</Txt>
                          ))}
                          {a.comparison.remaining_gaps?.map((x, i) => (
                            <Txt key={`gap-${i}`}>Next: {x}</Txt>
                          ))}
                          {a.comparison.recurring_gaps?.map((x, i) => (
                            <Txt key={`repeat-${i}`}>Recurring: {x}</Txt>
                          ))}
                          {a.comparison.evidence?.current.map((x, i) => (
                            <Txt key={`current-${i}`}>
                              Current evidence: {x}
                            </Txt>
                          ))}
                          {a.comparison.evidence?.previous.map((x, i) => (
                            <Txt key={`previous-${i}`}>
                              Previous evidence: {x}
                            </Txt>
                          ))}
                          {Object.entries(a.comparison.dimensions).map(
                            ([key, v]) => (
                              <Txt key={key}>
                                {key}: {v.previous} → {v.current}
                              </Txt>
                            ),
                          )}
                        </>
                      ) : null}
                    </>
                  ) : null}
                </View>
              ))}
            </Card>
          ))}
          <Txt muted>{session.retention_notice}</Txt>
        </>
      )}
    </ScrollView>
  );
}
