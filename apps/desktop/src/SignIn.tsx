// Sign in to a deployed stage: "Continue with Google" (the browser sign-in) or "Continue with
// email" (the address, then the six-digit code mailed to it). Used by the Session tab's setup
// and by Settings, Account. Both ways start the stage's server first if it sleeps; the app's
// server note shows that while it happens.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "./api";
import { cleanCode, clock, failureOf, failureText, needsNewCode, type CodeSent } from "./emailCode";
import { Button, Field, Input } from "./ui";

type Step = "choose" | "address" | "code";

export function SignIn({
  browserName,
  emailOffered,
  disabled,
  disabledHint,
  onSignedIn,
}: {
  /** Who the browser sign-in is with: "Google", or null for a generic browser sign-in. */
  browserName: string | null;
  /** Whether the stage signs in by email; null while the app has not asked it yet. */
  emailOffered: boolean | null;
  /** No stage to sign in to yet (a development build without its cloud URLs). */
  disabled?: boolean;
  disabledHint?: React.ReactNode;
  onSignedIn: (email: string) => void;
}) {
  const [step, setStep] = useState<Step>("choose");
  const [busy, setBusy] = useState<"browser" | "send" | "verify" | null>(null);
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
      onSignedIn(await api.emailSignInVerify(email.trim(), code));
    } catch (err) {
      const f = failureOf(err);
      setError(failureText(f));
      if (needsNewCode(f)) setCode("");
      else codeInput.current?.select();
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
          <Button variant="link" onClick={() => back("choose")} disabled={busy !== null}>
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

  return (
    <div className="ui-stack-tight sign-in">
      <div className="ui-row-inline">
        <Button variant="primary" loading={busy === "browser"} disabled={disabled} onClick={withBrowser}>
          {busy === "browser" ? "Waiting for the browser…" : browserName ? `Continue with ${browserName}` : "Continue with your browser"}
        </Button>
        {emailOffered !== false && (
          <Button disabled={disabled || busy !== null} onClick={() => back("address")}>
            Continue with email
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
