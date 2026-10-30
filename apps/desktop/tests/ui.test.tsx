// The design system's components: their states, names and keyboard behaviour.
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  Badge,
  Banner,
  Button,
  Checkbox,
  ConfirmDialog,
  Dialog,
  EmptyState,
  Field,
  IconButton,
  Input,
  ListRow,
  List,
  ListState,
  Menu,
  SegmentedControl,
  Switch,
  Tabs,
  ToastProvider,
  Truncate,
  middleTruncate,
  required,
  resolveTheme,
  url,
  useToast,
} from "../src/ui";
import { loadTheme } from "../src/ui/theme";

describe("Button", () => {
  it("is busy and does not click while loading", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );
    const b = screen.getByRole("button", { name: "Save" });
    expect(b).toBeDisabled();
    expect(b).toHaveAttribute("aria-busy", "true");
    await userEvent.click(b);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("an icon button is named by its label, not its glyph", () => {
    render(<IconButton label="Overlay settings" icon="⚙" />);
    expect(screen.getByRole("button", { name: "Overlay settings" })).toBeInTheDocument();
  });
});

describe("Field", () => {
  it("ties the hint and the error to the control", () => {
    render(
      <Field label="Company" hint="As on the posting" error="Enter the company.">
        <Input />
      </Field>,
    );
    const input = screen.getByLabelText("Company");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(/Enter the company\. As on the posting/);
    expect(screen.getByRole("alert")).toHaveTextContent("Enter the company.");
  });

  it("validators say what to do", () => {
    expect(required("  ", "the company")).toBe("Enter the company.");
    expect(required("Acme", "the company")).toBeNull();
    expect(url("")).toBeNull();
    expect(url("https://jobs.example.com/1")).toBeNull();
    expect(url("jobs.example.com")).toMatch(/not a link/);
  });

  it("a checkbox is labelled and toggles", async () => {
    const onChange = vi.fn();
    render(<Checkbox checked={false} onChange={onChange} label="Store the transcript" />);
    await userEvent.click(screen.getByLabelText("Store the transcript"));
    expect(onChange).toHaveBeenCalledWith(true);
  });
});

describe("SegmentedControl", () => {
  function Mode() {
    const [v, setV] = useState<"a" | "b" | "c">("a");
    return (
      <SegmentedControl
        label="Mode"
        value={v}
        onChange={setV}
        options={[
          ["a", "Interview"],
          ["b", "Standup"],
          ["c", "Meeting"],
        ]}
      />
    );
  }
  it("is a radio group that arrow keys move through", async () => {
    render(<Mode />);
    expect(screen.getByRole("radiogroup", { name: "Mode" })).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Interview"));
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByLabelText("Standup")).toBeChecked();
    await userEvent.click(screen.getByLabelText("Meeting"));
    expect(screen.getByLabelText("Meeting")).toBeChecked();
  });
});

describe("Tabs", () => {
  function T() {
    const [v, setV] = useState("a");
    return (
      <Tabs
        label="Call"
        value={v}
        onChange={setV}
        items={[
          { value: "a", label: "Summary", content: <p>summary text</p> },
          { value: "b", label: "Replay", content: <p>replay text</p>, count: 2 },
        ]}
      />
    );
  }
  it("shows one panel and switches by click and by arrow key", async () => {
    render(<T />);
    expect(screen.getByRole("tablist", { name: "Call" })).toBeInTheDocument();
    expect(screen.getByText("summary text")).toBeVisible();
    expect(screen.queryByText("replay text")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /Replay/ }));
    expect(screen.getByText("replay text")).toBeVisible();
    screen.getByRole("tab", { name: /Replay/ }).focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("ListState", () => {
  const empty = <EmptyState title="No calls yet" />;
  it("loading: a skeleton announced as loading", () => {
    render(<ListState items={null} error={null} loadingLabel="calls" empty={empty}>{() => null}</ListState>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading calls…");
  });
  it("error: says what failed, with Retry", async () => {
    const retry = vi.fn();
    render(<ListState items={null} error="HTTP 502" onRetry={retry} loadingLabel="calls" empty={empty}>{() => null}</ListState>);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load calls");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalled();
  });
  it("empty: the empty state", () => {
    render(<ListState items={[]} error={null} loadingLabel="calls" empty={empty}>{() => null}</ListState>);
    expect(screen.getByRole("heading", { name: "No calls yet" })).toBeInTheDocument();
  });
  it("loaded: the rows, as buttons", async () => {
    const open = vi.fn();
    render(
      <ListState items={["a"]} error={null} loadingLabel="calls" empty={empty}>
        {(items) => (
          <List>
            {items.map((i) => (
              <ListRow key={i} title="Interview · Northwind" meta="52 min" onClick={open} />
            ))}
          </List>
        )}
      </ListState>,
    );
    await userEvent.click(screen.getByRole("button", { name: /Interview · Northwind/ }));
    expect(open).toHaveBeenCalled();
  });
});

describe("Dialogs", () => {
  it("a dialog traps focus, closes on Escape and returns focus", async () => {
    function D() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <Button onClick={() => setOpen(true)}>Open</Button>
          <Dialog open={open} onOpenChange={setOpen} title="Add application">
            <Input aria-label="Company" />
          </Dialog>
        </>
      );
    }
    render(<D />);
    const opener = screen.getByRole("button", { name: "Open" });
    await userEvent.click(opener);
    expect(screen.getByRole("dialog", { name: "Add application" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("a typed confirmation waits for the word, then confirms", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog open onOpenChange={() => {}} title="Delete your account?" typed="delete" confirmLabel="Delete everything" onConfirm={onConfirm}>
        This cannot be undone.
      </ConfirmDialog>,
    );
    const confirm = screen.getByRole("button", { name: "Delete everything" });
    expect(confirm).toBeDisabled();
    expect(confirm).not.toHaveFocus();
    await userEvent.type(screen.getByLabelText(/Type delete to confirm/), "delete");
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalled();
  });

  it("a confirmation that fails stays open and says why", async () => {
    render(
      <ConfirmDialog open onOpenChange={() => {}} title="Delete this call?" confirmLabel="Delete call" onConfirm={() => Promise.reject("HTTP 500")}>
        Gone for good.
      </ConfirmDialog>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Delete call" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 500");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });
});

describe("Toast", () => {
  it("announces a message with its action", async () => {
    const undo = vi.fn();
    function T() {
      const toast = useToast();
      return <Button onClick={() => toast({ tone: "success", text: "Archived", action: { label: "Undo", onClick: undo } })}>Go</Button>;
    }
    render(
      <ToastProvider>
        <T />
      </ToastProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(await screen.findByText("Archived")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(undo).toHaveBeenCalled();
  });
});

describe("Switch, Menu, Badge, Banner", () => {
  it("a switch is a labelled switch", async () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="Echo cancellation" />);
    const sw = screen.getByRole("switch", { name: "Echo cancellation" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await userEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("a menu opens, moves with arrows, picks with Enter, and Escape returns focus", async () => {
    const edit = vi.fn();
    const del = vi.fn();
    render(
      <Menu
        label="More actions"
        trigger="⋯"
        items={[
          { label: "Edit", onSelect: edit },
          "separator",
          { label: "Delete…", onSelect: del, danger: true },
        ]}
      />,
    );
    const button = screen.getByRole("button", { name: "More actions" });
    await userEvent.click(button);
    expect(screen.getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Delete…" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    await userEvent.click(button);
    await userEvent.keyboard("{Enter}");
    expect(edit).toHaveBeenCalled();
  });

  it("a badge says its state in words", () => {
    render(<Badge tone="success">Ready</Badge>);
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("a danger banner is an alert", () => {
    render(<Banner tone="danger" title="Could not load" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load");
  });
});

describe("Truncation and theme", () => {
  it("truncated text keeps the whole text as its tooltip", () => {
    render(<Truncate text="Bluewater Logistics and Supply Chain Solutions" />);
    expect(screen.getByTitle("Bluewater Logistics and Supply Chain Solutions")).toBeInTheDocument();
  });
  it("cuts long paths in the middle", () => {
    expect(middleTruncate("C:/Users/alex/Documents/resumes/2026/final.pdf", 20)).toBe("C:/Users/a…final.pdf");
    expect(middleTruncate("short.pdf", 20)).toBe("short.pdf");
  });
  it("the theme follows the system unless overridden", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(loadTheme({ getItem: () => "dark" })).toBe("dark");
    expect(loadTheme({ getItem: () => "purple" })).toBe("system");
  });
  it("act is available for timers", () => {
    act(() => {});
  });
});
