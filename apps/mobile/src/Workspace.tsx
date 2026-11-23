import React, { useEffect, useState } from "react";
import { View } from "react-native";
import { Api } from "./api";
import { Companion } from "./Companion";
import { Assist } from "./Assist";
import { Ask, type AskEntry } from "./Ask";
import { WorkContext } from "./WorkContext";
import { PersonalAI } from "./PersonalAI";
import { Applications } from "./Applications";
import { Practice, type ImportedQuestion } from "./Practice";
import { History, Preparation } from "./History";
import { InPerson } from "./InPerson";
import { ScreenBoundary } from "./ScreenBoundary";
const retained = [
  "Companion",
  "Assist",
  "Ask",
  "WorkContext",
  "PersonalAI",
  "Practice",
  "Preparation",
  "Applications",
];
/** Scoped to one signed-in account/profile by App's key. Views retain drafts while
 * hidden, but their explicit visibility lifecycle releases audio and live polling. */
export function Workspace({
  api,
  url,
  tab,
  imported,
  onPractice,
  onTab,
  onProfilesChanged,
}: {
  api: Api;
  url: string;
  tab: string;
  imported?: ImportedQuestion;
  onPractice: (q: ImportedQuestion) => void;
  onTab: (tab: string) => void;
  onProfilesChanged: () => void;
}) {
  const [visited, setVisited] = useState<string[]>([]);
  const [askEntry, setAskEntry] = useState<AskEntry | null>(null);
  const [applicationEntry, setApplicationEntry] = useState<{
    id: string;
    n: number;
  } | null>(null);
  const [interviewId, setInterviewId] = useState<string | undefined>();
  function ask(kind: "company" | "application" | "call", id: string) {
    setAskEntry((old) => ({ scope: { kind, id }, n: (old?.n ?? 0) + 1 }));
    onTab("Ask");
  }
  const tabs =
    retained.includes(tab) && !visited.includes(tab)
      ? [...visited, tab]
      : visited;
  useEffect(() => {
    if (tab !== "In-person") setInterviewId(undefined);
    if (retained.includes(tab))
      setVisited((previous) =>
        previous.includes(tab) ? previous : [...previous, tab],
      );
  }, [tab]);
  return (
    <View style={{ flex: 1 }}>
      {tabs.map((name) => (
        <View
          key={name}
          style={{ flex: 1, display: name === tab ? "flex" : "none" }}
          accessibilityElementsHidden={name !== tab}
          importantForAccessibility={
            name === tab ? "auto" : "no-hide-descendants"
          }
        >
          <ScreenBoundary>
            {name === "Companion" ? (
              <Companion api={api} url={url} visible={name === tab} />
            ) : name === "Assist" ? (
              <Assist api={api} url={url} visible={name === tab} />
            ) : name === "Ask" ? (
              <Ask api={api} visible={name === tab} entry={askEntry} />
            ) : name === "WorkContext" ? (
              <WorkContext
                api={api}
                visible={name === tab}
                onAsk={(id) => ask("company", id)}
              />
            ) : name === "PersonalAI" ? (
              <PersonalAI api={api} visible={name === tab} />
            ) : name === "Applications" ? (
              <Applications
                api={api}
                visible={name === tab}
                onProfilesChanged={onProfilesChanged}
                onAsk={(id) => ask("application", id)}
                onPractice={(id) => {
                  setApplicationEntry((old) => ({ id, n: (old?.n ?? 0) + 1 }));
                  onTab("Practice");
                }}
                onStartInterview={(id) => {
                  setInterviewId(id);
                  onTab("In-person");
                }}
              />
            ) : name === "Preparation" ? (
              <Preparation api={api} visible={name === tab} />
            ) : (
              <Practice
                api={api}
                imported={imported}
                applicationEntry={applicationEntry}
                visible={name === tab}
              />
            )}
          </ScreenBoundary>
        </View>
      ))}
      {!retained.includes(tab) ? (
        <ScreenBoundary key={tab}>
          {tab === "In-person" ? (
            <InPerson api={api} url={url} initialInterviewId={interviewId} />
          ) : (
            <History
              api={api}
              practice={onPractice}
              onAsk={(id) => ask("call", id)}
            />
          )}
        </ScreenBoundary>
      ) : null}
    </View>
  );
}
