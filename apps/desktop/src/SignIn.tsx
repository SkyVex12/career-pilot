// Sign in to a deployed stage: "Continue with Google" (the browser sign-in) or "Continue with
// email" (the address, then the six-digit code mailed to it). Used by the Session tab's setup
// and by Settings, Account. Both ways start the stage's server first if it sleeps; the app's
// server note shows that while it happens.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type CareerOsAccount, type CareerOsOutcome } from "./api";
import { cleanCode, clock, failureOf, failureText, needsNewCode, type CodeSent } from "./emailCode";
import { Button, Field, Input } from "./ui";

type Step = "choose" | "address" | "code" | "accounts" | "match" | "prove";

function careerText(e: unknown): string {
  const f = failureOf(e);
  switch (f.code) {
    case "career_os_unavailable":
      return "Career-OS isn't answering. Google and email still work.";
    case "access_denied":
      return "You cancelled sign-in on Career-OS.";
    case "account_disabled":
      return "This Career-OS account is disabled.";
    case "linked_to_another":
      return "That account is already linked to a different Career-OS account.";
    case "identity_elsewhere":
      return "This Career-OS account already signs in to a different account here.";
    case "wrong_account":
      return "That sign-in was for a different account than the one you chose.";
    case "proof_required":
      return "Sign in to that account with Google or email first, then try again.";
    case "ticket_expired":
      return "That choice expired. Start Career-OS sign-in again.";
    case "not_offered":
      return "This server doesn't offer sign-in with Career-OS.";
    case "invalid_grant":
    case "invalid_assertion":
      return "Career-OS didn't accept that sign-in. Try again.";
    default:
      return failureText(f);
  }
}

function methodNames(methods: string[]): string {
  return methods
    .map((m) => (m === "google" ? "Google" : m === "email" ? "email code" : m === "career_os" ? "Career-OS" : m))
    .join(", ");
}

/** The sentence above the buttons. Google only keeps the period the setup screen's tests look for. */
export function signInWays(
  email: boolean | null,
  career: boolean | null,
  ending = "You stay signed in on this computer.",
): string {
  const ways = ["your Google account"];
  if (email !== false) ways.push("a code we email you");
  if (career !== false) ways.push("your Career-OS account");
  if (ways.length === 1) return `With your Google account. ${ending}`;
  return `With ${ways.slice(0, -1).join(", ")} or ${ways[ways.length - 1]}. ${ending}`;
}

export function SignIn({
  browserName,
  emailOffered,
  careerOsOffered,
  disabled,
  disabledHint,
  onSignedIn,
}: {
  /** Who the browser sign-in is with: "Google", or null for a generic browser sign-in. */
  browserName: string | null;
  /** Whether the stage signs in by email; null while the app has not asked it yet. */
  emailOffered: boolean | null;
  /** Whether the stage signs in with Career-OS; null while the app has not asked it yet. */
  careerOsOffered?: boolean | null;
  /** No stage to sign in to yet (a development build without its cloud URLs). */
  disabled?: boolean;
  disabledHint?: React.ReactNode;
  onSignedIn: (email: string) => void;
}) {
  const [step, setStep] = useState<Step>("choose");
  const [busy, setBusy] = useState<"browser" | "send" | "verify" | "career" | null>(null);
  /** A Career-OS sign-in that is waiting for the person to choose. */
  const [pending, setPending] = useState<Extract<CareerOsOutcome, { status: "choose_account" | "existing_or_new" }> | null>(null);
  const [chosenId, setChosenId] = useState<string | null>(null);
  /** The email steps are proving a chosen account, not starting a fresh email sign-in. */
  const [proving, setProving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<CodeSent | null>(null);
  /** When another code may be asked for (ms since the epoch), and the clock that counts to it. */
  const [resendAt, setResendAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const codeInput = useRef<HTMLInputElement>(null);
  const waiting = Math.max(0, Math.ceil((resendAt - now) / 1000));

  useEffect(() => {
    if (resendAt <= Date.now()) return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [resendAt]);

  const holdResend = (seconds: number) => {
    setNow(Date.now());
    setResendAt(Date.now() + seconds * 1000);
  };

  const withBrowser = async () => {
    setBusy("browser");
    setError(null);
    try {
      onSignedIn(await api.signIn());
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    if (busy || !email.trim()) return;
    setBusy("send");
    setError(null);
    try {
      const answer = await api.emailSignInStart(email.trim());
      setSent(answer);
      setCode("");
      setStep("code");
      holdResend(answer.resend_after_s);
      setTimeout(() => codeInput.current?.focus(), 0);
    } catch (err) {
      const f = failureOf(err);
      setError(failureText(f));
      if (f.code === "rate_limited" && f.retry_after_s) holdResend(f.retry_after_s);
    } finally {
      setBusy(null);
    }
  };

  const verify = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || code.length < (sent?.code_length ?? 6)) return;
    setBusy("verify");
    setError(null);
    try {
      const who = await api.emailSignInVerify(email.trim(), code);
      if (proving && pending) {
        await finishProof(who);
      } else {
        onSignedIn(who);
      }
    } catch (err) {
      const f = failureOf(err);
      setError(proving ? careerText(err) : failureText(f));
      if (needsNewCode(f)) setCode("");
      else codeInput.current?.select();
    } finally {
      setBusy(null);
    }
  };

  const landed = (outcome: CareerOsOutcome, fallback: string) => {
    if (outcome.status === "signed_in") onSignedIn(outcome.name || fallback);
    else if (outcome.status === "added") onSignedIn(fallback);
    else {
      setPending(outcome);
      setChosenId(null);
      setProving(false);
      setStep(outcome.status === "choose_account" ? "accounts" : "match");
    }
  };

  const withCareerOs = async () => {
    setBusy("career");
    setError(null);
    try {
      landed(await api.careerOsSignIn(), "your account");
    } catch (e) {
      setError(careerText(e));
    } finally {
      setBusy(null);
    }
  };

  const finishProof = async (who: string) => {
    if (!pending) return;
    const outcome = await api.careerOsComplete(pending.ticket, "existing", chosenId);
    if (outcome.status === "added" || outcome.status === "signed_in") onSignedIn(outcome.status === "signed_in" ? outcome.name : who);
    else landed(outcome, who);
  };

  const proveWithBrowser = async () => {
    setBusy("browser");
    setError(null);
    try {
      await finishProof(await api.signIn());
    } catch (e) {
      setError(careerText(e));
    } finally {
      setBusy(null);
    }
  };

  const createSeparate = async () => {
    if (!pending || pending.status !== "existing_or_new") return;
    setBusy("career");
    setError(null);
    try {
      const outcome = await api.careerOsComplete(pending.ticket, "new", null);
      landed(outcome, outcome.status === "signed_in" ? outcome.name : "your account");
    } catch (e) {
      setError(careerText(e));
    } finally {
      setBusy(null);
    }
  };

  const back = (to: Step) => {
    setStep(to);
    setError(null);
    setCode("");
  };

  if (step === "address") {
    return (
      <form className="ui-stack-tight sign-in" onSubmit={send} noValidate>
        <Field label="Email address" hint="We'll email you a code to sign in. No password." error={error}>
          <Input type="email" autoComplete="email" autoFocus value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" disabled={busy !== null} />
        </Field>
        <div className="ui-row-inline">
          <Button type="submit" variant="primary" loading={busy === "send"} disabled={!email.trim() || waiting > 0}>
            {busy === "send" ? "Sending…" : waiting > 0 ? `Send code in ${clock(waiting)}` : "Send code"}
          </Button>
          <Button variant="link" onClick={() => back(proving ? "prove" : "choose")} disabled={busy !== null}>
            Back
          </Button>
        </div>
      </form>
    );
  }

  if (step === "code" && sent) {
    const length = sent.code_length;
    return (
      <form className="ui-stack-tight sign-in" onSubmit={verify} noValidate>
        <p className="ui-text-2">
          We sent a {length}-digit code to <strong>{email.trim()}</strong>. It works once, for {Math.round(sent.expires_in_s / 60)} minutes.
        </p>
        <Field label="Code" error={error}>
          <Input
            ref={codeInput}
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            value={code}
            onChange={(e) => setCode(cleanCode(e.target.value, length))}
            placeholder={"0".repeat(length)}
            disabled={busy !== null}
          />
        </Field>
        <div className="ui-row-inline">
          <Button type="submit" variant="primary" loading={busy === "verify"} disabled={code.length < length}>
            {busy === "verify" ? "Signing in…" : "Sign in"}
          </Button>
          <Button onClick={() => send()} loading={busy === "send"} disabled={waiting > 0 || busy !== null}>
            {waiting > 0 ? `Send a new code in ${clock(waiting)}` : "Send a new code"}
          </Button>
          <Button variant="link" onClick={() => back("address")} disabled={busy !== null}>
            Use another address
          </Button>
        </div>
      </form>
    );
  }

  if (step === "accounts" && pending?.status === "choose_account") {
    return (
      <div className="ui-stack-tight sign-in">
        <p className="ui-text-2">
          This Career-OS account is linked to more than one account here. Choose one, then sign in to it with its own method. Nothing is merged.
        </p>
        {pending.accounts.map((account) => (
          <AccountRow
            key={account.id}
            account={account}
            disabled={busy !== null}
            onChoose={() => {
              setChosenId(account.id ?? null);
              setProving(true);
              setError(null);
              setStep("prove");
            }}
          />
        ))}
        {error && <p className="ui-field-error" role="alert">{error}</p>}
        <Button variant="link" onClick={() => back("choose")} disabled={busy !== null}>Start over</Button>
      </div>
    );
  }

  if (step === "match" && pending?.status === "existing_or_new") {
    const shown = pending.verified ? pending.account?.email : null;
    return (
      <div className="ui-stack-tight sign-in">
        <p className="ui-text-2">
          {shown
            ? `An account here uses ${shown}. Sign in to it to use it, or create a separate account. Nothing is merged.`
            : "An account here uses this email. Career-OS has not verified it, so it is only a hint. Sign in to that account to use it, or create a separate account. Nothing is merged."}
        </p>
        <div className="ui-row-inline">
          <Button variant="primary" disabled={busy !== null} onClick={() => { setChosenId(null); setProving(true); setError(null); setStep("prove"); }}>
            Use my existing account
          </Button>
          <Button loading={busy === "career"} disabled={busy !== null} onClick={createSeparate}>
            Create a separate account
          </Button>
        </div>
        {error && <p className="ui-field-error" role="alert">{error}</p>}
        <Button variant="link" onClick={() => back("choose")} disabled={busy !== null}>Start over</Button>
      </div>
    );
  }

  if (step === "prove") {
    return (
      <div className="ui-stack-tight sign-in">
        <p className="ui-text-2">Sign in to that account with Google or email. That proves it is yours, and then Career-OS is added to it.</p>
        <div className="ui-row-inline">
          <Button variant="primary" loading={busy === "browser"} disabled={busy !== null} onClick={proveWithBrowser}>
            {busy === "browser" ? "Waiting for the browser…" : browserName ? `Continue with ${browserName}` : "Continue with your browser"}
          </Button>
          {emailOffered !== false && (
            <Button disabled={busy !== null} onClick={() => back("address")}>Continue with email</Button>
          )}
        </div>
        {error && <p className="ui-field-error" role="alert">{error}</p>}
        <Button variant="link" onClick={() => back(pending?.status === "choose_account" ? "accounts" : "match")} disabled={busy !== null}>Back</Button>
      </div>
    );
  }

  return (
    <div className="ui-stack-tight sign-in">
      <div className="ui-row-inline">
        <Button variant="primary" loading={busy === "browser"} disabled={disabled} onClick={withBrowser}>
          {busy === "browser" ? "Waiting for the browser…" : browserName ? `Continue with ${browserName}` : "Continue with your browser"}
        </Button>
        {emailOffered !== false && (
          <Button disabled={disabled || busy !== null} onClick={() => { setProving(false); back("address"); }}>
            Continue with email
          </Button>
        )}
        {careerOsOffered !== false && (
          <Button loading={busy === "career"} disabled={disabled || busy !== null} onClick={withCareerOs}>
            {busy === "career" ? "Waiting for Career-OS…" : "Continue with Career-OS"}
          </Button>
        )}
        {disabled && disabledHint}
      </div>
      {error && (
        <p className="ui-field-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function AccountRow({ account, disabled, onChoose }: { account: CareerOsAccount; disabled: boolean; onChoose: () => void }) {
  const title = account.email || "Account";
  const detail = [methodNames(account.methods), account.created_at && `created ${account.created_at}`].filter(Boolean).join(" · ");
  return (
    <div className="ui-row-inline">
      <span>{title}{detail ? <span className="ui-field-hint"> {detail}</span> : null}</span>
      <Button size="sm" disabled={disabled} onClick={onChoose}>Use this account</Button>
    </div>
  );
}
