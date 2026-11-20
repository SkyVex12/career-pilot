import React, { useEffect, useState, useRef } from "react";
import {
  AppState,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  View,
  useColorScheme,
} from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import { Api, secureBase, type Tokens } from "./api";
import { secretsFor } from "./secure";
import { Companion } from "./Companion";
import { Ask } from "./Ask";
import { InPerson } from "./InPerson";
import { Practice, type ImportedQuestion } from "./Practice";
import { History, Preparation } from "./History";
import { Button, Input, Row, Theme, Txt, colors, page } from "./ui";
type Profile = { id: string; name: string; me: boolean };
export default function App() {
  const dark = useColorScheme() === "dark",
    c = colors(dark);
  const [server, setServer] = useState(process.env.EXPO_PUBLIC_API_URL ?? ""),
    [gateway, setGateway] = useState(process.env.EXPO_PUBLIC_GATEWAY_URL ?? ""),
    [api, setApi] = useState<Api | null>(null),
    [signed, setSigned] = useState(false),
    [email, setEmail] = useState(""),
    [code, setCode] = useState(""),
    [sent, setSent] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [profiles, setProfiles] = useState<Profile[]>([]),
    [profile, setProfile] = useState(""),
    [tab, setTab] = useState("Companion"),
    [imported, setImported] = useState<ImportedQuestion | undefined>(),
    [foreground, setForeground] = useState(true);
  useEffect(() => {
    const sub = AppState.addEventListener("change", (s) =>
      setForeground(s === "active"),
    );
    return () => sub.remove();
  }, []);
  const authScope = useRef(0);
  async function readProfiles(client: Api, ticket: number) {
    try {
      const d = await client.request<{ items: Profile[] }>("/profiles");
      if (ticket === authScope.current) setProfiles(d.items);
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    }
  }
  async function connect() {
    const ticket = ++authScope.current;
    setBusy(true);
    setError("");
    try {
      const base = secureBase(server, __DEV__);
      const client = new Api(base, await secretsFor(base), fetch, __DEV__);
      if (ticket !== authScope.current) return;
      setApi(client);
      if (!gateway) setGateway(`${base.replace(/^http/, "ws")}/v1/companion`);
      const restored = await client.restore();
      if (ticket !== authScope.current) return;
      setSigned(restored);
      if (restored) await readProfiles(client, ticket);
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    } finally {
      if (ticket === authScope.current) setBusy(false);
    }
  }
  async function start() {
    if (!api) return;
    const ticket = ++authScope.current;
    setBusy(true);
    try {
      await api.request(
        "/auth/email/start",
        { email: email.trim() },
        "POST",
        false,
      );
      if (ticket !== authScope.current) return;
      setSent(true);
      setError("Check your email for the sign-in code.");
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    } finally {
      if (ticket === authScope.current) setBusy(false);
    }
  }
  async function verify() {
    if (!api) return;
    const ticket = ++authScope.current;
    setBusy(true);
    try {
      const tokens = await api.request<Tokens>(
        "/auth/email/verify",
        { email: email.trim(), code: code.trim() },
        "POST",
        false,
      );
      if (ticket !== authScope.current) return;
      await api.accept(tokens);
      if (ticket !== authScope.current) return;
      setCode("");
      setSigned(true);
      setError("");
      await readProfiles(api, ticket);
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    } finally {
      if (ticket === authScope.current) setBusy(false);
    }
  }
  async function signout() {
    if (!api) return;
    const ticket = ++authScope.current;
    setBusy(false);
    setSigned(false);
    setProfiles([]);
    setProfile("");
    setImported(undefined);
    setTab("Companion");
    setCode("");
    setError("");
    const revoked = await api.signout();
    if (!revoked && ticket === authScope.current)
      setError(
        "Signed out on this phone. Network revocation could not complete; revoke this phone from desktop Settings when online.",
      );
  }
  function switchProfile(p: Profile) {
    if (!api) return;
    api.profile = p.me ? "" : p.id;
    setProfile(api.profile);
    setImported(undefined);
    setTab("Companion");
  }
  const group =
    tab === "Ask"
      ? "Work"
      : tab === "History"
        ? "History"
        : ["Practice", "Preparation"].includes(tab)
          ? "Interview prep"
          : "Call";
  return (
    <SafeAreaProvider>
      <Theme.Provider value={c}>
        <SafeAreaView style={{ flex: 1, backgroundColor: c.bg }}>
          <StatusBar style={dark ? "light" : "dark"} />
          <KeyboardAvoidingView
            style={{ flex: 1 }}
            behavior={Platform.OS === "ios" ? "padding" : undefined}
          >
            {!signed || !api ? (
              <ScrollView
                keyboardShouldPersistTaps="handled"
                contentContainerStyle={page}
              >
                <Txt title>Career Pilot</Txt>
                <Txt muted>
                  Your phone accompanies the desktop call. Sign in with the same
                  account.
                </Txt>
                {!api ? (
                  <>
                    <Input
                      placeholder="HTTPS API server"
                      autoCapitalize="none"
                      autoCorrect={false}
                      keyboardType="url"
                      value={server}
                      onChangeText={setServer}
                    />
                    <Input
                      placeholder="Secure gateway URL (optional)"
                      autoCapitalize="none"
                      autoCorrect={false}
                      value={gateway}
                      onChangeText={setGateway}
                    />
                    <Button
                      label="Connect server"
                      disabled={busy || !server}
                      onPress={() => void connect()}
                    />
                  </>
                ) : (
                  <>
                    <Txt muted>{api.base}</Txt>
                    <Input
                      placeholder="Email"
                      autoCapitalize="none"
                      keyboardType="email-address"
                      autoComplete="email"
                      value={email}
                      onChangeText={setEmail}
                    />
                    <Button
                      label={sent ? "Send another code" : "Send sign-in code"}
                      disabled={busy || !email.trim()}
                      onPress={() => void start()}
                    />
                    {sent ? (
                      <>
                        <Input
                          placeholder="Six-digit code"
                          keyboardType="number-pad"
                          autoComplete="one-time-code"
                          value={code}
                          onChangeText={setCode}
                          maxLength={6}
                        />
                        <Button
                          label="Sign in"
                          disabled={busy || code.length !== 6}
                          onPress={() => void verify()}
                        />
                      </>
                    ) : null}
                    <Button
                      label="Change server"
                      secondary
                      onPress={() => {
                        authScope.current++;
                        void api.signout();
                        setBusy(false);
                        setSigned(false);
                        setProfiles([]);
                        setCode("");
                        setApi(null);
                        setSent(false);
                      }}
                    />
                  </>
                )}
                {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
              </ScrollView>
            ) : (
              <>
                <View style={{ padding: 10, gap: 8 }}>
                  <Row>
                    <Txt style={{ fontWeight: "700" }}>Career Pilot</Txt>
                    <Txt muted>{api.email}</Txt>
                    <Button
                      label="Sign out"
                      secondary
                      onPress={() => void signout()}
                    />
                  </Row>
                  <ScrollView horizontal>
                    {profiles.map((p) => (
                      <View key={p.id} style={{ marginRight: 8 }}>
                        <Button
                          label={`${(p.me ? "" : p.id) === profile ? "✓ " : ""}${p.name || "Me"}`}
                          secondary
                          onPress={() => switchProfile(p)}
                        />
                      </View>
                    ))}
                  </ScrollView>
                  {error ? <Txt accessibilityRole="alert">{error}</Txt> : null}
                  <ScrollView horizontal>
                    {[
                      ["Call", "Companion"],
                      ["Interview prep", "Practice"],
                      ["Work", "Ask"],
                      ["History", "History"],
                    ].map(([label, target]) => (
                      <View key={target} style={{ marginRight: 8 }}>
                        <Button
                          label={`${group === label ? "✓ " : ""}${label}`}
                          secondary
                          onPress={() => setTab(target)}
                        />
                      </View>
                    ))}
                  </ScrollView>
                  {group === "Call" ? (
                    <Row>
                      <Button
                        label="Desktop companion"
                        secondary
                        onPress={() => setTab("Companion")}
                      />
                      <Button
                        label="In-person microphone"
                        secondary
                        onPress={() => setTab("In-person")}
                      />
                    </Row>
                  ) : group === "Interview prep" ? (
                    <Row>
                      <Button
                        label="Practice"
                        secondary
                        onPress={() => setTab("Practice")}
                      />
                      <Button
                        label="Meeting preparation"
                        secondary
                        onPress={() => setTab("Preparation")}
                      />
                    </Row>
                  ) : null}
                </View>
                <View key={`${api.base}:${profile}`} style={{ flex: 1 }}>
                  {tab === "Companion" ? (
                    <Companion api={api} url={gateway} />
                  ) : tab === "Ask" ? (
                    <Ask api={api} />
                  ) : tab === "Practice" ? (
                    <Practice api={api} imported={imported} />
                  ) : tab === "Preparation" ? (
                    <Preparation api={api} />
                  ) : tab === "In-person" ? (
                    <InPerson api={api} url={gateway} />
                  ) : (
                    <History
                      api={api}
                      practice={(q) => {
                        setImported(q);
                        setTab("Practice");
                      }}
                    />
                  )}
                </View>
              </>
            )}
            {!foreground ? (
              <View
                accessibilityElementsHidden
                style={{
                  position: "absolute",
                  top: 0,
                  bottom: 0,
                  left: 0,
                  right: 0,
                  backgroundColor: c.bg,
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Txt title>Career Pilot</Txt>
              </View>
            ) : null}
          </KeyboardAvoidingView>
        </SafeAreaView>
      </Theme.Provider>
    </SafeAreaProvider>
  );
}
