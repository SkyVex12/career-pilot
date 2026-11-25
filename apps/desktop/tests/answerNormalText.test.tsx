import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerText } from "../src/AnswerText";
import { CodeAnswer } from "../src/CodeAnswer";

function expectNormal(container: HTMLElement) {
  expect(container.querySelector(".suggested, .suggest-tag")).toBeNull();
  expect(container.textContent).not.toContain("[~");
  expect(container.querySelector('[title*="check it before"]')).toBeNull();
}

describe("normal answer presentation", () => {
  it.each([
    ["I would compare both options. [~]", "I would compare both options."],
    ["[~] I would compare both options.", "I would compare both options."],
    ["- [~] I would compare both options.\n- I used Redis. [n:abc]", "I would compare both options.I used Redis."],
    ["First paragraph. [~]\nSecond paragraph.", "First paragraph.Second paragraph."],
  ])("displays historical notation as ordinary answer text: %s", (text, expected) => {
    const { container } = render(<AnswerText text={text} />);
    expect(container.textContent?.trim()).toBe(expected);
    expectNormal(container);
    expect(container.querySelector(".ov-caret")).toBeNull();
  });

  it.each(["", "- ", "* ", "• "])("does not create an empty row while a prefixed marker streams after %j", (bullet) => {
    const { container, rerender } = render(<AnswerText text="" streaming />);
    for (const fragment of ["[", "[~", "[~]", "[~] "]) {
      rerender(<AnswerText text={bullet + fragment} streaming />);
      expect(container.querySelectorAll("p, li")).toHaveLength(0);
      expectNormal(container);
    }
    rerender(<AnswerText text={bullet + "[~] I would compare both options."} streaming />);
    expect(container.querySelectorAll("p, li")).toHaveLength(1);
    expect(container.textContent?.trim()).toBe("I would compare both options.");
    expectNormal(container);
    expect(container.querySelectorAll(".ov-caret")).toHaveLength(1);
  });

  it("keeps an existing sentence stable as a suffix marker arrives in every split", () => {
    const base = "I would compare both options.";
    const { container, rerender } = render(<AnswerText text={base} streaming />);
    const row = container.querySelector("p");
    for (const suffix of [" ", " [", " [~", " [~]"]) {
      rerender(<AnswerText text={base + suffix} streaming />);
      expect(container.querySelector("p")).toBe(row);
      expect(container.textContent?.trim()).toBe(base);
      expectNormal(container);
    }
    rerender(<AnswerText text={base + " [~]"} />);
    expect(container.querySelector("p")).toBe(row);
    expect(container.querySelector(".ov-caret")).toBeNull();
  });

  it("retains ordinary brackets and citation streaming behavior", () => {
    const { container, rerender } = render(<AnswerText text="Use [array] and [link](https://example.test). [n:ab]" />);
    expect(container.textContent).toBe("Use [array] and [link](https://example.test).");
    rerender(<AnswerText text={"The source is supported. [n:ab\n"} streaming />);
    expect(container.textContent).toBe("The source is supported.");
    rerender(<AnswerText text="Use arrays [" />);
    expect(container.textContent).toBe("Use arrays [");
  });

  it("leaves actual fenced and inline code contents unchanged", () => {
    const text = 'Use \u0060[~]\u0060 literally.\n\u0060\u0060\u0060python\nvalue = "[~] [n:abc]"\n\u0060\u0060\u0060';
    const { container } = render(<CodeAnswer text={text} />);
    expect(container.querySelector(".code-text code")?.textContent).toBe("[~]");
    expect(container.querySelector("pre code")?.textContent).toBe('value = "[~] [n:abc]"');
    expect(container.querySelector(".suggested, .suggest-tag")).toBeNull();
  });
});

describe("normal prose beside code", () => {
  it("removes legacy markers in explanatory paragraphs, headings, bullets, and bold text", () => {
    const { container } = render(<CodeAnswer text={"- [~] Use **bounded workers**.\n## [~] Notes\n**A practical option [~]**\nSupported [n:abc]. [~]"} />);
    expect(Array.from(container.querySelectorAll(".code-text p, .code-text h3"), (node) => node.textContent?.trim().replace(/\s+/g, " "))).toEqual(["• Use bounded workers.", "Notes", "A practical option", "Supported [n:abc]."]);
    expect(container.querySelector("h3")?.textContent?.trim()).toBe("Notes");
    expectNormal(container);
  });

  it.each(["[~", "[~]", "- [~", "- [~]", "## [~]", "**[~]**"])("omits a marker-only prose row: %s", (text) => {
    const { container } = render(<CodeAnswer text={text} streaming />);
    expect(container.querySelectorAll("p, h3")).toHaveLength(0);
    expect(container.querySelector(".ov-caret")).toBeNull();
  });

  it("leaves ordinary brackets visible in code explanations", () => {
    const { container } = render(<CodeAnswer text="Use [array] and [link](https://example.test). [" streaming />);
    expect(container.querySelector(".code-text")?.textContent).toBe("Use [array] and [link](https://example.test). [");
  });

  it("keeps an incomplete inline-code tail literal until its delimiter arrives", () => {
    const { container, rerender } = render(<CodeAnswer text={'Use \u0060[~'} streaming />);
    expect(container.querySelector(".code-text")?.textContent).toBe('Use \u0060[~');
    rerender(<CodeAnswer text={'Use \u0060[~]\u0060 [~]'} />);
    expect(container.querySelector(".code-text code")?.textContent).toBe("[~]");
    expect(container.querySelector(".code-text")?.textContent).toBe("Use [~]");
  });

  it("does not rewrite backtick-delimited contents inside a bold token", () => {
    const { container } = render(<CodeAnswer text={'**Use \u0060[~]\u0060 literally** [~]'} />);
    expect(container.querySelector("strong")?.textContent).toBe('Use \u0060[~]\u0060 literally');
    expect(container.querySelector(".code-text")?.textContent).toBe('Use \u0060[~]\u0060 literally');
  });
  it.each([
    ["**Use \u0060[~]\u0060 literally [~]** [~]", "Use \u0060[~]\u0060 literally"],
    ["**Use \u0060\u0060[~]\u0060x\u0060\u0060 then [~]**", "Use \u0060\u0060[~]\u0060x\u0060\u0060 then"],
  ])("removes prose notation after a closed code span inside bold text: %s", (text, expected) => {
    const { container } = render(<CodeAnswer text={text} />);
    expect(container.querySelector("strong")?.textContent).toBe(expected);
    expect(container.querySelector(".code-text")?.textContent).toBe(expected);
    expect(container.querySelector(".suggested, .suggest-tag")).toBeNull();
  });

});
