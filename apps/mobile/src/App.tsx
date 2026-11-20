import React, { useEffect, useState, useRef } from "react";
import {
  AppState,
  KeyboardAvoidingView,
  Linking,
  Platform,
  ScrollView,
  View,
  useColorScheme,
} from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import { Api, secureBase, type Tokens } from "./api";
import { HTTP_TEST_BUILD } from "./transport";
import { gatewayURL } from "./liveState";
import { loadAuthConfig, type AuthConfig } from "./authConfig";
import {
  beginCareerOs,
  type CareerOsAttempt,
  type CareerOsOutcome,
} from "./careerOs";
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
  const authAbort = useRef<AbortController | null>(null);
  const careerAttempt = useRef<CareerOsAttempt | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [browserPending, setBrowserPending] = useState(false);
  const [callback, setCallback] = useState("");
  const [accountChoice, setAccountChoice] = useState<CareerOsOutcome | null>(
    null,
  );
  function cancelBrowser() {
    careerAttempt.current?.cancel();
    careerAttempt.current = null;
    authAbort.current?.abort();
    authAbort.current = null;
    setBrowserPending(false);
    setCallback("");
    setAccountChoice(null);
  }
  useEffect(
    () => () => {
      authScope.current++;
      authAbort.current?.abort();
      careerAttempt.current?.cancel();
    },
    [],
  );
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
    cancelBrowser();
    const controller = new AbortController();
    authAbort.current = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    setBusy(true);
    setError("");
    try {
      const base = secureBase(server, __DEV__);
      const live = gatewayURL(
        gateway.trim() || `${base.replace(/^http/, "ws")}/v1/companion`,
      );
      const client = new Api(base, await secretsFor(base), fetch, __DEV__);
      if (ticket !== authScope.current) return;
      const config = await loadAuthConfig(client, controller.signal);
      if (ticket !== authScope.current) return;
      const restored = await client.restore();
      if (ticket !== authScope.current) return;
      setApi(client);
      setAuthConfig(config);
      setServer(base);
      setGateway(live);
      setSigned(restored);
      if (restored) await readProfiles(client, ticket);
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    } finally {
      clearTimeout(timeout);
      if (authAbort.current === controller) authAbort.current = null;
      if (ticket === authScope.current) setBusy(false);
    }
  }
  async function openCareerOs() {
    if (!api || !authConfig?.career_os) return;
    const ticket = ++authScope.current;
    cancelBrowser();
    setBusy(true);
    setError("");
    try {
      const attempt = await beginCareerOs(authConfig.career_os);
      if (ticket !== authScope.current) {
        attempt.cancel();
        return;
      }
      careerAttempt.current = attempt;
      setBrowserPending(true);
      await Linking.openURL(attempt.authorizeUrl);
    } catch (e) {
      if (ticket === authScope.current) {
        cancelBrowser();
        setError(String(e));
      }
    } finally {
      if (ticket === authScope.current) setBusy(false);
    }
  }
  async function finishCareerOs() {
    const attempt = careerAttempt.current,
      client = api;
    if (!attempt || !client) return;
    const ticket = ++authScope.current;
    const controller = new AbortController();
    authAbort.current = controller;
    const timeout = setTimeout(() => controller.abort(), 20000);
    setBusy(true);
    setError("");
    try {
      const outcome = await attempt.exchange(
        client,
        callback,
        controller.signal,
      );
      if (ticket !== authScope.current) return;
      careerAttempt.current = null;
      setCallback("");
      setBrowserPending(false);
      if (outcome.status === "signed_in") {
        await client.accept(outcome);
        if (ticket !== authScope.current) return;
        setSigned(true);
        setAccountChoice(null);
        await readProfiles(client, ticket);
      } else setAccountChoice(outcome);
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    } finally {
      clearTimeout(timeout);
      if (authAbort.current === controller) authAbort.current = null;
      if (ticket === authScope.current) setBusy(false);
    }
  }
  async function start() {
    if (!api || !authConfig?.email) return;
    cancelBrowser();
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
    cancelBrowser();
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
                <Txt title>
                  {HTTP_TEST_BUILD ? "Career Pilot HTTP Test" : "Career Pilot"}
                </Txt>
                <Txt muted>
                  Your phone accompanies the desktop call. Sign in with the same
                  account.
                </Txt>
                {HTTP_TEST_BUILD ? (
                  <Txt muted>
                    HTTP test build. HTTP/WS traffic, including sign-in and call
                    content, is unencrypted. Use a test account and test
                    content.
                  </Txt>
                ) : null}
                {!api ? (
                  <>
                    <Input
                      placeholder={
                        HTTP_TEST_BUILD
                          ? "API server (HTTP or HTTPS)"
                          : "HTTPS API server"
                      }
                      autoCapitalize="none"
                      autoCorrect={false}
                      keyboardType="url"
                      value={server}
                      editable={!busy}
                      onChangeText={setServer}
                    />
                    <Input
                      placeholder={
                        HTTP_TEST_BUILD
                          ? "Gateway URL (optional; derived from API server)"
                          : "Secure gateway URL (optional)"
                      }
                      autoCapitalize="none"
                      autoCorrect={false}
                      value={gateway}
                      editable={!busy}
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
                    {authConfig?.career_os && (
                      <>
                        <Button
                          label={
                            browserPending
                              ? "Restart Career-OS sign-in"
                              : "Sign in with Career-OS"
                          }
                          disabled={busy}
                          onPress={() => void openCareerOs()}
                        />
                        {browserPending && (
                          <>
                            <Txt muted>
                              Sign in and approve in your browser. It will then
                              show a cannot-connect page at 127.0.0.1. That is
                              expected for this manual sign-in: copy the whole
                              address from the browser bar and paste it below.
                              Do not share that address.
                            </Txt>
                            <Input
                              placeholder="Paste complete callback address"
                              value={callback}
                              onChangeText={setCallback}
                              autoCapitalize="none"
                              autoCorrect={false}
                              secureTextEntry
                            />
                            <Button
                              label="Finish Career-OS sign-in"
                              disabled={busy || !callback.trim()}
                              onPress={() => void finishCareerOs()}
                            />
                            <Button
                              label="Cancel browser sign-in"
                              secondary
                              onPress={() => {
                                authScope.current++;
                                cancelBrowser();
                                setBusy(false);
                              }}
                            />
                          </>
                        )}
                      </>
                    )}
                    {accountChoice && (
                      <>
                        <Txt muted>
                          To keep the same desktop account, sign in on desktop
                          and add Career-OS under Settings, then restart
                          Career-OS sign-in here. This app will not create a
                          separate account from this choice.
                        </Txt>
                        {accountChoice.status === "choose_account" &&
                          accountChoice.accounts.map((a, i) => (
                            <Txt muted key={a.id ?? i}>
                              {a.email || "Existing account"}:{" "}
                              {a.methods.join(", ")}
                            </Txt>
                          ))}
                        {accountChoice.status === "existing_or_new" &&
                          accountChoice.account && (
                            <Txt muted>
                              {accountChoice.account.email ||
                                "Existing account"}
                              : {accountChoice.account.methods.join(", ")}
                            </Txt>
                          )}
                      </>
                    )}
                    {!authConfig?.email && (
                      <Txt muted>
                        Email codes are not enabled on this server.{" "}
                        {authConfig?.career_os
                          ? "Use Career-OS above. If your desktop account uses Google, add Career-OS to that account in desktop Settings first."
                          : "This build does not support this server's browser sign-in provider yet."}
                      </Txt>
                    )}
                    {authConfig?.email && (
                      <>
                        <Input
                          placeholder="Email"
                          autoCapitalize="none"
                          keyboardType="email-address"
                          autoComplete="email"
                          value={email}
                          onChangeText={setEmail}
                        />
                        <Button
                          label={
                            sent ? "Send another code" : "Send sign-in code"
                          }
                          disabled={busy || !email.trim()}
                          onPress={() => void start()}
                        />
                        {sent ? (
                          <>
                            <Input
                              placeholder={`${authConfig.email.code_length}-digit code`}
                              keyboardType="number-pad"
                              autoComplete="one-time-code"
                              value={code}
                              onChangeText={setCode}
                              maxLength={authConfig.email.code_length}
                            />
                            <Button
                              label="Sign in"
                              disabled={
                                busy ||
                                code.length !== authConfig.email.code_length
                              }
                              onPress={() => void verify()}
                            />
                          </>
                        ) : null}
                      </>
                    )}
                    <Button
                      label="Change server"
                      secondary
                      onPress={() => {
                        authScope.current++;
                        cancelBrowser();
                        setAuthConfig(null);
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
                    <Txt style={{ fontWeight: "700" }}>
                      {HTTP_TEST_BUILD
                        ? "Career Pilot · HTTP TEST"
                        : "Career Pilot"}
                    </Txt>
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
                <Txt title>
                  {HTTP_TEST_BUILD ? "Career Pilot HTTP Test" : "Career Pilot"}
                </Txt>
              </View>
            ) : null}
          </KeyboardAvoidingView>
        </SafeAreaView>
      </Theme.Provider>
    </SafeAreaProvider>
  );
}
