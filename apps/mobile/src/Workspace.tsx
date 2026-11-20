import React, { useEffect, useState } from "react";
import { View } from "react-native";
import { Api } from "./api";
import { Companion } from "./Companion";
import { Ask } from "./Ask";
import { Practice, type ImportedQuestion } from "./Practice";
import { History, Preparation } from "./History";
import { InPerson } from "./InPerson";
import { ScreenBoundary } from "./ScreenBoundary";
const retained = ["Companion", "Ask", "Practice"];
/** Scoped to one signed-in account/profile by App's key. Views retain drafts while
 * hidden, but their explicit visibility lifecycle releases audio and live polling. */
export function Workspace({
  api,
  url,
  tab,
  imported,
  onPractice,
}: {
  api: Api;
  url: string;
  tab: string;
  imported?: ImportedQuestion;
  onPractice: (q: ImportedQuestion) => void;
}) {
  const [visited, setVisited] = useState<string[]>([]);
  const tabs =
    retained.includes(tab) && !visited.includes(tab)
      ? [...visited, tab]
      : visited;
  useEffect(() => {
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
            ) : name === "Ask" ? (
              <Ask api={api} visible={name === tab} />
            ) : (
              <Practice api={api} imported={imported} visible={name === tab} />
            )}
          </ScreenBoundary>
        </View>
      ))}
      {!retained.includes(tab) ? (
        <ScreenBoundary key={tab}>
          {tab === "Preparation" ? (
            <Preparation api={api} />
          ) : tab === "In-person" ? (
            <InPerson api={api} url={url} />
          ) : (
            <History api={api} practice={onPractice} />
          )}
        </ScreenBoundary>
      ) : null}
    </View>
  );
}
