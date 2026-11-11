// Email sign-in's words and clock (SignIn.tsx): what each failure from the Rust core
// (emailauth.rs, `EmailFailure`) says on screen, and the countdown to the next code.
// The name is `emailCode`, not `signin`: on Windows `signin.ts` and `SignIn.tsx` are one path.

/** A failed step: the API's code, or `not_offered` / `network` from the app itself. */
export interface EmailFailure {
  code: string;
  message: string;
  retry_after_s?: number;
}

/** `POST /auth/email/start`'s answer. */
export interface CodeSent {
  code_length: number;
  expires_in_s: number;
  resend_after_s: number;
}

/** What a rejected command carried: the failure, or a plain message from anything else. */
export function failureOf(e: unknown): EmailFailure {
  if (e && typeof e === "object" && typeof (e as EmailFailure).code === "string") return e as EmailFailure;
  return { code: "unavailable", message: String(e) };
}

/** 75 → "1:15". */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 840 → "14 minutes"; 45 → "45 seconds". */
export function wait(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} seconds`;
  const minutes = Math.ceil(seconds / 60);
  return minutes >= 90 ? `${Math.ceil(minutes / 60)} hours` : `${minutes} minutes`;
}

export function failureText(f: EmailFailure): string {
  switch (f.code) {
    case "invalid_email":
      return "That doesn't look like an email address.";
    case "invalid_code":
      return "That code isn't right. Check it and try again.";
    case "code_expired":
      return "That code has expired or was already used. Send a new one.";
    case "code_locked":
      return "Too many wrong tries, so that code no longer works. Send a new one.";
    case "rate_limited":
      return f.retry_after_s ? `Too many tries. Try again in ${wait(f.retry_after_s)}.` : "Too many tries. Wait a while and try again.";
    case "not_offered":
      return "This server doesn't offer sign-in by email.";
    case "email_taken":
      return "This email belongs to another account.";
    default:
      return f.message || "Sign-in didn't work. Try again.";
  }
}

/** A failure that means the code is dead: only a new code helps. */
export function needsNewCode(f: EmailFailure): boolean {
  return f.code === "code_expired" || f.code === "code_locked";
}

/** Digits only, as many as the code has: a pasted "123 456" or "123-456" works. */
export function cleanCode(raw: string, length: number): string {
  return raw.replace(/\D/g, "").slice(0, length);
}
