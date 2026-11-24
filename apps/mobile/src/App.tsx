import React, { useEffect, useState, useRef } from "react";
import {
  AppState,
  KeyboardAvoidingView,
  Linking,
  Platform,
  Pressable,
  ScrollView,
  View,
  useColorScheme,
} from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import { Api, type Tokens } from "./api";
import { HTTP_TEST_BUILD } from "./transport";
import { checkClientCapabilities } from "./clientCapabilities";
import { gatewayOverride, resolveConnection } from "./connectionConfig";
import { loadAuthConfig, type AuthConfig } from "./authConfig";
import {
  beginCareerOs,
  type CareerOsAttempt,
  type CareerOsOutcome,
} from "./careerOs";
import { secretsFor } from "./secure";
import { responsePersistence } from "./responsePersistence";
import { type ImportedQuestion } from "./Practice";
import { Workspace } from "./Workspace";
import {
  Button,
  Card,
  Field,
  Icon,
  Input,
  Notice,
  Row,
  Theme,
  Txt,
  colors,
  page,
} from "./ui";
import { AppShell, Brand } from "./AppShell";
import {
  readConnectionPreference,
  saveConnectionPreference,
  type ConnectionPreference,
} from "./preferences";
type Profile = { id: string; name: string; me: boolean };
export default function App() {
  const dark = useColorScheme() === "dark",
    c = colors(dark);
  const [server, setServer] = useState(process.env.EXPO_PUBLIC_API_URL ?? ""),
    [gateway, setGateway] = useState(""),
    [gatewayInput, setGatewayInput] = useState(() =>
      gatewayOverride(
        process.env.EXPO_PUBLIC_API_URL ?? "",
        process.env.EXPO_PUBLIC_GATEWAY_URL ?? "",
      ),
    ),
    [gatewayExpanded, setGatewayExpanded] = useState(!!gatewayInput),
    [api, setApi] = useState<Api | null>(null),
    [signed, setSigned] = useState(false),
    [email, setEmail] = useState(""),
    [code, setCode] = useState(""),
    [sent, setSent] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [serverNotice, setServerNotice] = useState<string | null>(null),
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
    void checkClientCapabilities(client, authAbort.current?.signal)
      .then((result) => {
        if (ticket === authScope.current) setServerNotice(result.notice);
      })
      .catch(() => {
        /* Cancelled by account/server change. */
      });
    try {
      const d = await client.request<{ items: Profile[] }>("/profiles");
      if (
        !Array.isArray(d.items) ||
        d.items.some(
          (p) =>
            !p ||
            typeof p.id !== "string" ||
            typeof p.name !== "string" ||
            typeof p.me !== "boolean",
        )
      )
        throw new Error(
          "The server returned an unsupported profile response. Update the server and try again.",
        );
      if (ticket === authScope.current) {
        setProfiles(d.items);
        if (client.profile && !d.items.some((p) => p.id === client.profile)) {
          client.profile = "";
          setProfile("");
          setImported(undefined);
          setTab("Companion");
        }
      }
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    }
  }
  async function connect(preference?: ConnectionPreference) {
    const ticket = ++authScope.current;
    cancelBrowser();
    setServerNotice(null);
    const controller = new AbortController();
    authAbort.current = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    setBusy(true);
    setError("");
    try {
      const { api: base, gateway: live } = resolveConnection(
        preference?.api ?? server,
        preference
          ? gatewayOverride(preference.api, preference.gateway)
          : gatewayInput,
        __DEV__,
      );
      const persistence = await responsePersistence(base).catch(() => undefined);
      const client = new Api(base, await secretsFor(base), fetch, __DEV__, HTTP_TEST_BUILD, 120000, persistence);
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
      void saveConnectionPreference({ api: base, gateway: live });
      if (restored) await readProfiles(client, ticket);
    } catch (e) {
      if (ticket === authScope.current) setError(String(e));
    } finally {
      clearTimeout(timeout);
      if (authAbort.current === controller) authAbort.current = null;
      if (ticket === authScope.current) setBusy(false);
    }
  }
  useEffect(() => {
    let active = true;
    void readConnectionPreference().then((preference) => {
      if (active && preference) {
        setServer(preference.api);
        const override = gatewayOverride(preference.api, preference.gateway);
        setGatewayInput(override);
        setGatewayExpanded(!!override);
        void connect(preference);
      }
    });
    return () => {
      active = false;
    };
  }, []);
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
      setError("");
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
    setServerNotice(null);
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
                keyboardDismissMode={
                  Platform.OS === "ios" ? "interactive" : "on-drag"
                }
                contentContainerStyle={[
                  page,
                  {
                    flexGrow: 1,
                    justifyContent: "center",
                    maxWidth: 520,
                    paddingVertical: 28,
                    gap: 24,
                  },
                ]}
              >
                <View style={{ gap: 24 }}>
                  <Brand />
                  <View style={{ gap: 10 }}>
                    <Txt title style={{ fontSize: 32, lineHeight: 39 }}>
                      {!api
                        ? "Your workspace,\nwherever you are."
                        : "Sign in to CoRuby"}
                    </Txt>
                    <Txt muted>
                      {!api
                        ? "Follow a live call, practice an interview, and keep your work context close."
                        : "Sign in to the same account you use on your computer."}
                    </Txt>
                  </View>
                </View>
                <Row style={{ flexWrap: "nowrap", gap: 12 }}>
                  {["Workspace", "Sign in"].map((label, index) => (
                    <Row
                      key={label}
                      style={{ flex: 1, flexWrap: "nowrap", gap: 8 }}
                    >
                      <View
                        style={{
                          width: 25,
                          height: 25,
                          borderRadius: 13,
                          alignItems: "center",
                          justifyContent: "center",
                          backgroundColor:
                            (api ? 1 : 0) === index ? c.ruby : c.soft,
                        }}
                      >
                        {api && index === 0 ? (
                          <Icon name="check" size={14} color={c.success} />
                        ) : (
                          <Txt
                            style={{
                              fontSize: 12,
                              fontWeight: "700",
                              color:
                                (api ? 1 : 0) === index ? c.onRuby : c.muted,
                            }}
                          >
                            {index + 1}
                          </Txt>
                        )}
                      </View>
                      <Txt
                        style={{
                          fontSize: 13,
                          fontWeight: "600",
                          color: (api ? 1 : 0) === index ? c.text : c.muted,
                        }}
                      >
                        {label}
                      </Txt>
                    </Row>
                  ))}
                </Row>
                <Card>
                  {!api ? (
                    <>
                      <Txt style={{ fontWeight: "700", fontSize: 18 }}>
                        Workspace connection
                      </Txt>
                      <Field
                        label="Workspace address"
                        hint={
                          HTTP_TEST_BUILD
                            ? "Use the server address from your computer. This test build also accepts HTTP."
                            : "Enter the HTTPS API address for your workspace."
                        }
                      >
                        <Input
                          placeholder={
                            HTTP_TEST_BUILD
                              ? "http://your-server"
                              : "https://your-server"
                          }
                          autoCapitalize="none"
                          autoCorrect={false}
                          keyboardType="url"
                          returnKeyType="go"
                          onSubmitEditing={() => {
                            if (server.trim() && !busy) void connect();
                          }}
                          value={server}
                          editable={!busy}
                          onChangeText={(value) => {
                            setServer(value);
                            if (gatewayInput.trim()) setGatewayExpanded(true);
                          }}
                        />
                      </Field>
                      <Pressable
                        accessibilityRole="button"
                        accessibilityState={{ expanded: gatewayExpanded }}
                        onPress={() => setGatewayExpanded((value) => !value)}
                        style={{
                          minHeight: 44,
                          flexDirection: "row",
                          alignItems: "center",
                          gap: 8,
                        }}
                      >
                        <Icon name="settings" size={17} />
                        <Txt muted style={{ fontSize: 13, flex: 1 }}>
                          Advanced connection settings
                        </Txt>
                        <View
                          style={{
                            transform: [
                              { rotate: gatewayExpanded ? "180deg" : "0deg" },
                            ],
                          }}
                        >
                          <Icon name="chevron" size={16} />
                        </View>
                      </Pressable>
                      {gatewayExpanded ? (
                        <Field
                          label="Live gateway"
                          hint={
                            gatewayInput.trim()
                              ? "Custom gateway for live calls. Clear this field to use the workspace address."
                              : "Optional. Leave blank to use the workspace address."
                          }
                        >
                          <Input
                            placeholder="Automatic"
                            autoCapitalize="none"
                            autoCorrect={false}
                            keyboardType="url"
                            value={gatewayInput}
                            editable={!busy}
                            onChangeText={setGatewayInput}
                          />
                        </Field>
                      ) : null}
                      {HTTP_TEST_BUILD &&
                      /(?:127\.0\.0\.1|localhost)/i.test(server) ? (
                        <Notice>
                          On Android, localhost is this device. To reach your
                          computer, use its network address.
                        </Notice>
                      ) : null}
                      <Button
                        label={busy ? "Connecting…" : "Connect workspace"}
                        icon="arrow"
                        loading={busy}
                        disabled={!server.trim()}
                        onPress={() => void connect()}
                      />
                      {busy ? (
                        <Button
                          label="Cancel connection"
                          secondary
                          onPress={() => {
                            authScope.current++;
                            cancelBrowser();
                            setBusy(false);
                            setError("");
                            setGatewayExpanded(!!gatewayInput.trim());
                          }}
                        />
                      ) : null}
                    </>
                  ) : (
                    <>
                      {authConfig?.career_os ? (
                        <>
                          <Button
                            label={
                              browserPending
                                ? "Restart browser sign-in"
                                : "Continue with HubRuby"
                            }
                            icon="arrow"
                            loading={busy}
                            onPress={() => void openCareerOs()}
                          />
                          {browserPending ? (
                            <>
                              <Notice>
                                After you approve in your browser, it will show
                                a cannot-connect page at 127.0.0.1. Copy that
                                complete address and paste it here to finish.
                                Keep it private.
                              </Notice>
                              <Field label="Sign-in callback">
                                <Input
                                  placeholder="Paste the complete browser address"
                                  value={callback}
                                  onChangeText={setCallback}
                                  autoCapitalize="none"
                                  autoCorrect={false}
                                  secureTextEntry
                                />
                              </Field>
                              <Button
                                label="Finish sign-in"
                                disabled={busy || !callback.trim()}
                                onPress={() => void finishCareerOs()}
                              />
                              <Button
                                label="Cancel"
                                secondary
                                onPress={() => {
                                  authScope.current++;
                                  cancelBrowser();
                                  setBusy(false);
                                }}
                              />
                            </>
                          ) : null}
                        </>
                      ) : null}
                      {accountChoice ? (
                        <Notice>
                          To use your existing desktop account, add HubRuby
                          under desktop Settings → Account → Sign-in, then
                          restart sign-in here. Your accounts will stay separate
                          until you link them.
                        </Notice>
                      ) : null}
                      {authConfig?.email ? (
                        <>
                          {authConfig.career_os ? (
                            <Txt
                              muted
                              style={{ textAlign: "center", fontSize: 12 }}
                            >
                              or use an email code
                            </Txt>
                          ) : null}
                          <Field label="Email address">
                            <Input
                              placeholder="you@example.com"
                              autoCapitalize="none"
                              keyboardType="email-address"
                              autoComplete="email"
                              value={email}
                              onChangeText={setEmail}
                            />
                          </Field>
                          <Button
                            label={
                              sent ? "Send another code" : "Send sign-in code"
                            }
                            secondary={!!authConfig.career_os}
                            disabled={busy || !email.trim()}
                            onPress={() => void start()}
                          />
                          {sent ? (
                            <>
                              <Notice>
                                Check your email for the sign-in code.
                              </Notice>
                              <Field label="Verification code">
                                <Input
                                  placeholder={`${authConfig.email.code_length}-digit code`}
                                  keyboardType="number-pad"
                                  autoComplete="one-time-code"
                                  value={code}
                                  onChangeText={setCode}
                                  maxLength={authConfig.email.code_length}
                                />
                              </Field>
                              <Button
                                label="Sign in"
                                loading={busy}
                                disabled={
                                  code.length !== authConfig.email.code_length
                                }
                                onPress={() => void verify()}
                              />
                            </>
                          ) : null}
                        </>
                      ) : !browserPending ? (
                        <Txt muted style={{ fontSize: 12, lineHeight: 19 }}>
                          {authConfig?.career_os
                            ? "Use Google on desktop? First add HubRuby to that same account in desktop Settings → Account → Sign-in."
                            : "This server has no sign-in method supported by this build. Update its authentication configuration."}
                        </Txt>
                      ) : null}
                      <View
                        style={{
                          borderTopWidth: 1,
                          borderColor: c.line,
                          paddingTop: 12,
                          gap: 10,
                        }}
                      >
                        <Txt muted numberOfLines={1} style={{ fontSize: 12 }}>
                          {api.base}
                        </Txt>
                        <Button
                          label="Change server"
                          secondary
                          onPress={() => {
                            authScope.current++;
                            cancelBrowser();
                            setAuthConfig(null);
                            void api.signout().catch(() => {});
                            setBusy(false);
                            setSigned(false);
                            setProfiles([]);
                            setCode("");
                            setApi(null);
                            setSent(false);
                            setGatewayExpanded(!!gatewayInput.trim());
                          }}
                        />
                      </View>
                    </>
                  )}
                  {error ? <Notice tone="error">{error}</Notice> : null}
                </Card>
                {HTTP_TEST_BUILD ? (
                  <Txt
                    muted
                    style={{
                      fontSize: 11,
                      lineHeight: 17,
                      textAlign: "center",
                    }}
                  >
                    HTTP TEST · HTTP traffic includes unencrypted sign-in and
                    call content. Use a test account and test content.
                  </Txt>
                ) : null}
              </ScrollView>
            ) : (
              <AppShell
                profiles={profiles}
                profile={profile}
                email={api.email}
                server={api.base}
                tab={tab}
                error={error}
                serverNotice={serverNotice}
                onProfile={switchProfile}
                onTab={setTab}
                onSignout={() =>
                  void signout().catch((e) => setError(String(e)))
                }
              >
                <Workspace
                  key={`${api.base}:${profile}`}
                  api={api}
                  url={gateway}
                  tab={tab}
                  onTab={setTab}
                  onProfilesChanged={() =>
                    void readProfiles(api, authScope.current)
                  }
                  imported={imported}
                  onPractice={(q) => {
                    setImported(q);
                    setTab("Practice");
                  }}
                />
              </AppShell>
            )}
            {!foreground ? (
              <View
                accessibilityElementsHidden
                importantForAccessibility="no-hide-descendants"
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
                <Brand />
              </View>
            ) : null}
          </KeyboardAvoidingView>
        </SafeAreaView>
      </Theme.Provider>
    </SafeAreaProvider>
  );
}
