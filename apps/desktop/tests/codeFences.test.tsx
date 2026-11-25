import { act, fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CodeAnswer, splitBlocks } from "../src/CodeAnswer";

// Authored cases: only Markdown container/fence indentation is removed.
// Closed space-container fixtures were also checked against a CommonMark lexer.
const fixtures = [
  {
    "name": "list opener - ",
    "source": "- ```python\n  def f():\n      return 1\n  ```",
    "code": "def f():\n    return 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "list opener * ",
    "source": "* ```python\n  def f():\n      return 1\n  ```",
    "code": "def f():\n    return 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "list opener + ",
    "source": "+ ```python\n  def f():\n      return 1\n  ```",
    "code": "def f():\n    return 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "list opener 1. ",
    "source": "1. ```python\n   def f():\n       return 1\n   ```",
    "code": "def f():\n    return 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "list opener 2) ",
    "source": "2) ```python\n   def f():\n       return 1\n   ```",
    "code": "def f():\n    return 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "list opener 23. ",
    "source": "23. ```python\n    def f():\n        return 1\n    ```",
    "code": "def f():\n    return 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "2-space list continuation",
    "source": "- explanation\n\n  ```python\n  def f():\n      return 2\n  ```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "4-space list continuation",
    "source": "- explanation\n\n    ```python\n    def f():\n        return 2\n    ```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "nested bullet",
    "source": "- outer\n  - ```py\n    def f():\n        return 2\n    ```",
    "code": "def f():\n    return 2",
    "lang": "py",
    "open": false
  },
  {
    "name": "nested ordered",
    "source": "1. outer\n   1. ```python\n      def f():\n          return 2\n      ```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "same-line nested markers",
    "source": "- - ```python\n    x = 1\n    ```",
    "code": "x = 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "0-space top-level fence",
    "source": "```python\ndef f():\n    return 2\n```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "1-space top-level fence",
    "source": " ```python\n def f():\n     return 2\n ```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "2-space top-level fence",
    "source": "  ```python\n  def f():\n      return 2\n  ```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "3-space top-level fence",
    "source": "   ```python\n   def f():\n       return 2\n   ```",
    "code": "def f():\n    return 2",
    "lang": "python",
    "open": false
  },
  {
    "name": "relative indentation and multiline strings",
    "source": "- ```python\n  def f():\n      text = \"\"\"first\n    - inside string\n      final\"\"\"\n      return text\n  ```",
    "code": "def f():\n    text = \"\"\"first\n  - inside string\n    final\"\"\"\n    return text",
    "lang": "python",
    "open": false
  },
  {
    "name": "blank lines trailing spaces",
    "source": "- ```python\n  x = 1  \n  \n  \n  ```",
    "code": "x = 1  \n\n",
    "lang": "python",
    "open": false
  },
  {
    "name": "literal string backticks",
    "source": "```python\nx = \"```\"\n```",
    "code": "x = \"```\"",
    "lang": "python",
    "open": false
  },
  {
    "name": "shorter literal closing",
    "source": "````python\nx = 1\n```\n````",
    "code": "x = 1\n```",
    "lang": "python",
    "open": false
  },
  {
    "name": "longer matching closing",
    "source": "```python\nx = 1\n`````",
    "code": "x = 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "tilde block",
    "source": "- ~~~~python\n  x = \"```\"\n  ~~~~~",
    "code": "x = \"```\"",
    "lang": "python",
    "open": false
  },
  {
    "name": "different fence marker literal",
    "source": "```python\n~~~\n```",
    "code": "~~~",
    "lang": "python",
    "open": false
  },
  {
    "name": "closing trailer literal",
    "source": "```python\n``` not a close\n```",
    "code": "``` not a close",
    "lang": "python",
    "open": false
  },
  {
    "name": "code tabs retained",
    "source": "```python\ndef f():\n\treturn 3\n```",
    "code": "def f():\n\treturn 3",
    "lang": "python",
    "open": false
  },
  {
    "name": "CRLF and Unicode",
    "source": "🙂 text\r\n\r\n- ```python\r\n  x = \"é🙂\"\r\n  ```\r\n",
    "code": "x = \"é🙂\"",
    "lang": "python",
    "open": false
  },
  {
    "name": "empty body",
    "source": "```python\n```",
    "code": "",
    "lang": "python",
    "open": false
  },
  {
    "name": "streaming list fence",
    "source": "- ```python\n  def broken(",
    "code": "def broken(",
    "lang": "python",
    "open": true
  },
  {
    "name": "streaming partial close",
    "source": "- ```python\n  x = 1\n  ``",
    "code": "x = 1\n``",
    "lang": "python",
    "open": true
  },
  {
    "name": "no global code dedent",
    "source": "```python\n    x = 1\n```",
    "code": "    x = 1",
    "lang": "python",
    "open": false
  },
  {
    "name": "literal code tab after space container",
    "source": "- ```python\n  def f():\n  \treturn 3\n  ```",
    "code": "def f():\n\treturn 3",
    "lang": "python",
    "open": false
  }
];

describe("fenced code in list containers", () => {
  it.each([
  {
    "name": "lazy list continuation outside supported grammar",
    "source": "- explanation\nlazy continuation\n\n    ```py\n    x=1\n    ```"
  },
  {
    "name": "thematic break before indented prose",
    "source": "- - -\n    ```py\n    x=1\n    ```"
  },
  {
    "name": "tab supplies required container indentation",
    "source": "- ```py\n\tprint(1)\n  ```"
  },
  {
    "name": "unclosed tab container source",
    "source": "- ```py\n\tprint(1)"
  },
  {
    "name": "partial space then tab container",
    "source": "1. ```py\n \tprint(1)\n   ```"
  }
]) ("preserves unsupported syntax as plain source: $name", ({ source }) => {
    const blocks = splitBlocks(source);
    expect(blocks.every(block => block.kind === "text")).toBe(true);
    expect(blocks.flatMap(block => block.kind === "text" ? block.lines : []).join("\n")).toBe(source);
  });

  it("offers no copy block for ambiguous tab container indentation", () => {
    const ui = render(<CodeAnswer text={"- ```py\n\tprint(1)\n  ```"} />);
    expect(ui.container.querySelector(".code-block")).toBeNull();
    expect(ui.queryByRole("button", { name: "Copy the py code" })).toBeNull();
    expect(ui.container.textContent).toContain("print(1)");
  });

  it.each(fixtures)("$name", ({ source, code, lang, open }) => {
    expect(splitBlocks(source).filter(block => block.kind === "code")).toEqual([
      { kind: "code", lang, code, open },
    ]);
  });

  it.each([
    "Use ```python in prose.",
    "    ```python\n    x=1\n    ```",
    "> ```python\n> x=1\n> ```",
  ])("does not interpret prose, indented code, or quote syntax as fences: %s", source => {
    expect(splitBlocks(source).every(block => block.kind === "text")).toBe(true);
  });

  it("keeps text and the next block outside a closed list fence", () => {
    expect(splitBlocks("- ```py\n  x=1\n  ```\nAfter.\n```js\ny=2\n```")).toEqual([
      { kind: "code", lang: "py", code: "x=1", open: false },
      { kind: "text", lines: ["After."] },
      { kind: "code", lang: "js", code: "y=2", open: false },
    ]);
  });

  it("ends an unclosed list fence when its container ends", () => {
    expect(splitBlocks("- ```py\n  x=1\nOutside.")).toEqual([
      { kind: "code", lang: "py", code: "x=1", open: false },
      { kind: "text", lines: ["Outside."] },
    ]);
  });

  it("renders and copies exactly the same code after container removal", async () => {
    const write = vi.fn(async (_text: string) => {});
    Object.assign(navigator, { clipboard: { writeText: write } });
    const code = 'def f():\n\n    text = "<tag>& ```"  \n    return text';
    const text = "- ```python\n" + code.split("\n").map(line => "  " + line).join("\n") + "\n  ```";
    const ui = render(<CodeAnswer text={text} />);
    expect(ui.container.querySelector("pre code")?.textContent).toBe(code);
    expect(ui.container.querySelector("tag")).toBeNull();
    await act(async () => { fireEvent.click(ui.getByRole("button", { name: "Copy the python code" })); });
    expect(write).toHaveBeenCalledWith(code);
  });

  it("does not copy an open streaming fence before its matching close", () => {
    const text = "- ```python\n  def f():\n      return 1";
    const ui = render(<CodeAnswer text={text} streaming />);
    expect(ui.getByRole("button", { name: "Copy the python code" }).hasAttribute("disabled")).toBe(true);
    ui.rerender(<CodeAnswer text={text + "\n  ```"} streaming />);
    expect(ui.getByRole("button", { name: "Copy the python code" }).hasAttribute("disabled")).toBe(false);
    expect(ui.container.querySelector("pre code")?.textContent).toBe("def f():\n    return 1");
  });
});
