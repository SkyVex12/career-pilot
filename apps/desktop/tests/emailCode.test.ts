import { describe, expect, it } from "vitest";
import { cleanCode, clock, failureOf, failureText, needsNewCode, wait } from "../src/emailCode";

describe("email sign-in words", () => {
  it("formats the resend clock and a rate-limit wait", () => {
    expect(clock(75)).toBe("1:15");
    expect(clock(5)).toBe("0:05");
    expect(clock(0)).toBe("0:00");
    expect(wait(45)).toBe("45 seconds");
    expect(wait(840)).toBe("14 minutes");
    expect(wait(5400)).toBe("2 hours");
  });

  it("names a wrong code, an expired code, and a locked code", () => {
    expect(failureText({ code: "invalid_code", message: "no" })).toBe("That code isn't right. Check it and try again.");
    expect(failureText({ code: "code_expired", message: "no" })).toBe("That code has expired or was already used. Send a new one.");
    expect(failureText({ code: "code_locked", message: "no" })).toBe("Too many wrong tries, so that code no longer works. Send a new one.");
    expect(failureText({ code: "rate_limited", message: "no", retry_after_s: 90 })).toBe("Too many tries. Try again in 2 minutes.");
    expect(needsNewCode({ code: "code_expired", message: "" })).toBe(true);
    expect(needsNewCode({ code: "invalid_code", message: "" })).toBe(false);
  });

  it("keeps a structured failure and keeps only digits of a pasted code", () => {
    expect(failureOf({ code: "not_offered", message: "no" }).code).toBe("not_offered");
    expect(failureOf("the server is down").code).toBe("unavailable");
    expect(cleanCode("123 456", 6)).toBe("123456");
    expect(cleanCode("12-34-56-78", 6)).toBe("123456");
  });
});
