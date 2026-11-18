// The shots each screenshot question was asked with (coding mode), for its card: the app keeps
// the last 10 problems' (src-tauri/src/lib.rs `SentShots`) and sends each new one as it goes
// out; a card whose shots this window has not seen (it opened or reloaded mid-call) asks the
// app for them, once.
import { useEffect, useState } from "react";
import { api, inTauri, onShotsSent, type ShotView } from "./api";

const byQuestion = new Map<string, ShotView[]>();
const asked = new Set<string>();
const listeners = new Set<() => void>();
let listening = false;

function changed() {
  listeners.forEach((l) => l());
}

function listen() {
  if (listening || !inTauri) return;
  listening = true;
  void onShotsSent((s) => {
    byQuestion.set(s.question_id, s.shots);
    changed();
  });
}

function fetchFor(questionId: string) {
  if (!inTauri || byQuestion.has(questionId) || asked.has(questionId)) return;
  asked.add(questionId);
  void api.codingSentShots().then(
    (all) => {
      (Array.isArray(all) ? all : []).forEach((s) => byQuestion.set(s.question_id, s.shots));
      changed();
    },
    () => undefined,
  );
}

/** The shots a question was asked with, if it is a screenshot this app sent. */
export function useSentShots(questionId: string): ShotView[] | undefined {
  const [, tick] = useState(0);
  useEffect(() => {
    listen();
    const l = () => tick((n) => n + 1);
    listeners.add(l);
    fetchFor(questionId);
    return () => {
      listeners.delete(l);
    };
  }, [questionId]);
  return byQuestion.get(questionId);
}
