// Settings (docs/design/ui-proposal.md, "Settings"): General, Account, Audio, Overlay & hotkeys,
// Advanced. Toggles, choices and devices save the moment they change, and a recorded hotkey
// when it is valid; free-text fields (a development build's URLs and token) keep a Save button.
import { useEffect, useState } from "react";
import { api, type Settings, type SettingsView, type UpdateState } from "./api";
import { cloudUrlsFor } from "./urls";
import { ago, settingsLine } from "./updates";
import { CareerOsSettings } from "./CareerOs";
import { DevicePickers } from "./CallScreen";
import { HOTKEYS, clashWith } from "./hotkeys";
import { accountOf } from "./profiles";
import { HotkeyInput } from "./ui/HotkeyInput";
import { Banner, Button, Card, ConfirmDialog, Field, Input, PageHeader, SegmentedControl, Select, Switch, useTheme, useToast } from "./ui";

type Section = "general" | "account" | "audio" | "overlay" | "advanced";
const SECTIONS: [Section, string][] = [
  ["general", "General"],
  ["account", "Account"],
  ["audio", "Audio"],
  ["overlay", "Overlay & hotkeys"],
  ["advanced", "Advanced"],
];

export function SettingsScreen({
  view,
  running,
  onSaved,
  update,
  check,
  section: initial,
}: {
  view: SettingsView;
  running: boolean;
  onSaved: () => void;
  update: UpdateState;
  check: { busy: boolean; error: string | null; start: () => void };
  /** The section to open on (the Career-OS lock opens Account). */
  section?: Section;
}) {
  const [section, setSection] = useState<Section>(initial ?? "general");
  useEffect(() => {
    if (initial) setSection(initial);
  }, [initial]);
  const toast = useToast();
  const settings = view.settings;

  /** Saves one change at once, with the rest of the settings as they are. */
  const save = async (patch: Partial<Settings>, done = "Saved.") => {
    try {
      await api.saveSettings({ ...settings, ...patch }, null);
      onSaved();
      toast({ tone: "success", text: done });
    } catch (e) {
      toast({ tone: "error", text: String(e) });
      throw e;
    }
  };

  return (
    <>
      <PageHeader title="Settings" />
      <div className="settings">
        <nav className="settings-nav" aria-label="Settings sections">
          <ul>
            {SECTIONS.map(([s, label]) => (
              <li key={s}>
                <button type="button" className="settings-link" aria-current={section === s ? "true" : undefined} onClick={() => setSection(s)}>
                  {label}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="settings-body">
          {section === "general" && <General view={view} save={save} update={update} check={check} />}
          {section === "account" && <Account view={view} onSaved={onSaved} />}
          {section === "audio" && (
            <Card title="Audio">
              <div className="ui-stack">
                <DevicePickers settings={settings} disabled={running} onSaved={onSaved} />
                {running && <p className="ui-field-hint">Devices change between calls.</p>}
                <Switch
                  label="Echo cancellation"
                  hint="Removes the call's sound from the microphone. Needed without headphones."
                  checked={settings.echo_cancellation}
                  onChange={(on) => save({ echo_cancellation: on }, running ? "Saved. It applies to the next call." : "Saved.").catch(() => undefined)}
                />
              </div>
            </Card>
          )}
          {section === "overlay" && <Hotkeys view={view} save={save} />}
          {section === "advanced" && <Advanced view={view} save={save} onSaved={onSaved} />}
        </div>
      </div>
    </>
  );
}

type Save = (patch: Partial<Settings>, done?: string) => Promise<void>;

function General({ view, save, update, check }: { view: SettingsView; save: Save; update: UpdateState; check: { busy: boolean; error: string | null; start: () => void } }) {
  const [theme, setTheme] = useTheme();
  const updates = view.updates;
  const line = settingsLine(update);
  return (
    <>
      <Card title="Appearance">
        <Field label="Theme" hint="System follows Windows. The overlay stays dark.">
          <SegmentedControl
            label="Theme"
            value={theme}
            onChange={setTheme}
            options={[
              ["system", "System"],
              ["light", "Light"],
              ["dark", "Dark"],
            ]}
          />
        </Field>
      </Card>
      <Card title="Updates">
        <div className="ui-stack">
          <div className="ui-row-inline">
            <span>Version {view.version}</span>
            {updates.enabled && (
              <Button size="sm" onClick={check.start} loading={check.busy} disabled={update.state === "downloading" || update.state === "installing"}>
                {check.busy ? "Checking…" : "Check now"}
              </Button>
            )}
          </div>
          {updates.enabled ? (
            <p className={check.error || update.state === "check_failed" ? "ui-field-error" : "ui-field-hint"} aria-live="polite">
              {check.busy
                ? "Asking the release server for a newer version…"
                : (check.error ?? line ?? `Last checked ${updates.checked_at ? ago(updates.checked_at) : "not yet"}. Checks at start and every four hours.`)}
            </p>
          ) : (
            <p className="ui-field-hint">A development build: it does not update itself.</p>
          )}
          {updates.enabled && (
            <Field label="Install updates" hint="Never during a call. Automatic updates install when you have not used the app for two minutes; it then restarts and says “Updated to …”.">
              <Select value={view.settings.update_mode} onChange={(e) => save({ update_mode: e.target.value as Settings["update_mode"] }).catch(() => undefined)}>
                <option value="auto">By themselves</option>
                <option value="ask">Ask me first</option>
              </Select>
            </Field>
          )}
        </div>
      </Card>
    </>
  );
}

function Account({ view, onSaved }: { view: SettingsView; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  const settings = view.settings;
  const browser = Boolean(view.release) || settings.environment === "cloud";
  const signIn = async () => {
    setBusy(true);
    setError(null);
    try {
      const email = await api.signIn();
      onSaved();
      toast({ tone: "success", text: `Signed in${email ? ` as ${email}` : ""}.` });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const signOut = async () => {
    try {
      await api.signOut();
      onSaved();
      toast({ tone: "success", text: "Signed out." });
    } catch (e) {
      toast({ tone: "error", text: String(e) });
    }
  };
  return (
    <>
      <Card title="Sign-in">
        {browser ? (
          view.signed_in_as !== null ? (
            <div className="ui-row-inline">
              <span>Signed in{view.signed_in_as ? ` as ${view.signed_in_as}` : ""}.</span>
              <Button size="sm" onClick={signOut}>
                Sign out
              </Button>
            </div>
          ) : (
            <div className="ui-stack-tight">
              <p className="ui-text-2">Opens your browser at the sign-in page. The sign-in is kept in the Windows Credential Manager and renews itself, also during a call.</p>
              <div className="ui-row-inline">
                <Button variant="primary" loading={busy} disabled={!settings.cloud_api_url} onClick={signIn}>
                  {busy ? "Waiting for the browser…" : "Sign in with your browser"}
                </Button>
                {!settings.cloud_api_url && <span className="ui-field-hint">Set the cloud URLs under Advanced first.</span>}
              </div>
              {error && <p className="ui-field-error">{error}</p>}
            </div>
          )
        ) : (
          <p className="ui-text-2">
            A development build on the local stack signs in with the dev token ({view.dev_token_set ? "set" : "not set"}). Change it under Advanced.
          </p>
        )}
      </Card>
      <Card title="Career-OS">
        <CareerOsSettings key={accountOf(view)} ready={view.ready} />
      </Card>
      {view.ready && <DeleteAccount />}
    </>
  );
}

function Hotkeys({ view, save }: { view: SettingsView; save: Save }) {
  const settings = view.settings;
  return (
    <Card title="Hotkeys">
      <p className="ui-field-hint hotkeys-intro">They work everywhere, also while the overlay is hidden or clicks pass through it. Press Change, then the keys.</p>
      <div className="hotkey-list">
        {HOTKEYS.map(({ key, action, label, hint, fallback }) => {
          const invalid = view.hotkey_review.invalid.find((i) => i.action === action);
          return (
            <HotkeyInput
              key={key}
              label={label}
              hint={hint}
              value={String(settings[key] ?? "")}
              clearable={!fallback}
              error={invalid ? `Not a valid key (${invalid.error}).` : null}
              onChange={async (accel) => {
                const clash = accel ? clashWith(settings, accel, key) : null;
                if (clash) throw `“${clash.label}” already uses that key. Pick another, or change that one first.`;
                await save({ [key]: accel } as Partial<Settings>, accel ? "Hotkey saved." : "Hotkey cleared.");
              }}
            />
          );
        })}
      </div>
    </Card>
  );
}

function Advanced({ view, save, onSaved }: { view: SettingsView; save: Save; onSaved: () => void }) {
  const settings = view.settings;
  const release = view.release;
  const [draft, setDraft] = useState(settings);
  const [token, setToken] = useState("");
  const [host, setHost] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  useEffect(() => setDraft(settings), [settings]);
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setDraft((d) => ({ ...d, [k]: v }));
  const fields: (keyof Settings)[] = ["environment", "gateway_url", "api_url", "cloud_gateway_url", "cloud_api_url"];
  const dirty = Boolean(token) || fields.some((k) => draft[k] !== settings[k]);
  const saveConnection = async () => {
    setBusy(true);
    try {
      const next = { ...settings };
      for (const k of fields) (next as Record<string, unknown>)[k] = draft[k];
      await api.saveSettings(next, token || null);
      setToken("");
      onSaved();
      toast({ tone: "success", text: "Saved." });
    } catch (e) {
      toast({ tone: "error", text: String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Card title="Developer">
        <Switch
          label="Debug panel"
          hint="Audio levels, latency per question and the connection log. Also Ctrl+Shift+D."
          checked={settings.show_debug}
          onChange={(on) => save({ show_debug: on }).catch(() => undefined)}
        />
      </Card>
      {!release && (
        <Card
          title="Connection (development build)"
          footer={
            <Button variant="primary" disabled={!dirty} loading={busy} onClick={saveConnection}>
              Save
            </Button>
          }
        >
          <div className="ui-stack">
            {view.warnings.map((w) => (
              <Banner key={w} tone="warning">
                {w}
              </Banner>
            ))}
            <Field label="Environment">
              <SegmentedControl
                label="Environment"
                value={draft.environment}
                onChange={(v) => set("environment", v)}
                options={[
                  ["local", "Local (dev token)"],
                  ["cloud", "Cloud (browser sign-in)"],
                ]}
              />
            </Field>
            {draft.environment === "local" ? (
              <>
                <Field label="Gateway URL">
                  <Input value={draft.gateway_url} onChange={(e) => set("gateway_url", e.target.value)} spellCheck={false} />
                </Field>
                <Field label="API URL" hint="The Copilot API's /v1 root, for example http://127.0.0.1:8080/v1. The same token signs in to both.">
                  <Input value={draft.api_url} onChange={(e) => set("api_url", e.target.value)} spellCheck={false} />
                </Field>
                <Field label="Dev token" hint="Kept in the Windows Credential Manager, not in the settings file. Local only: the services refuse it in the cloud.">
                  <Input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={view.dev_token_set ? "Saved (type to replace)" : "COPILOT_DEV_TOKEN from the .env"} autoComplete="off" />
                </Field>
              </>
            ) : (
              <>
                <Field label="Cloud host" hint="The VM's address (terraform output external_ip); fills both URLs.">
                  <div className="ui-row-inline">
                    <Input value={host} onChange={(e) => setHost(e.target.value)} spellCheck={false} />
                    <Button
                      onClick={() => {
                        const urls = cloudUrlsFor(host);
                        if (urls) setDraft((d) => ({ ...d, ...urls }));
                      }}
                    >
                      Fill URLs
                    </Button>
                  </div>
                </Field>
                <Field label="Gateway URL">
                  <Input value={draft.cloud_gateway_url} onChange={(e) => set("cloud_gateway_url", e.target.value)} spellCheck={false} />
                </Field>
                <Field label="API URL">
                  <Input value={draft.cloud_api_url} onChange={(e) => set("cloud_api_url", e.target.value)} spellCheck={false} />
                </Field>
              </>
            )}
          </div>
        </Card>
      )}
    </>
  );
}

/** The whole account, behind a typed confirmation. */
function DeleteAccount() {
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  return (
    <div className="ui-card account-danger">
      <div className="ui-card-body ui-row-inline account-danger-row">
        <div className="ui-stack-tight">
          <h3 className="ui-card-title">Delete account</h3>
          <p className="ui-field-hint">Deletes every call, transcript, document and memory fact, and your encryption keys. This cannot be undone.</p>
        </div>
        <Button variant="danger-ghost" onClick={() => setConfirming(true)}>
          Delete my account…
        </Button>
      </div>
      {message && (
        <div className="ui-card-body">
          <Banner tone="info">{message}</Banner>
        </div>
      )}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Delete your account?"
        typed="delete"
        confirmLabel="Delete everything"
        onConfirm={async () => {
          await api.deleteAccount();
          setMessage("Your account is being deleted: calls, documents, memory and keys. Local recordings are gone.");
        }}
      >
        Every call, transcript, document and memory fact is deleted, with your encryption keys. This cannot be undone.
      </ConfirmDialog>
    </div>
  );
}
