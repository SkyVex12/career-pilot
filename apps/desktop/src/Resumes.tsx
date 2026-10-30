// Resumes: the base resume and the tailored versions (each owned by one application), uploaded
// as a file or pasted, with labels; their processing and each one's digest. Interview packs
// for an application use only the version it is paired with (ARCHITECTURE.md, "Resume versions").
import { useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type ResumeVersion } from "./api";
import { isTextFile, resumeStatusLabel } from "./pairing";
import { ACCEPT, plural, useOnShow, when } from "./shared";
import { usePolling } from "./usePolling";
import { Badge, Banner, Button, Card, ConfirmDialog, Dialog, EmptyState, Field, Input, List, ListRow, ListState, Menu, PageHeader, SkeletonList, Textarea, useToast, type Tone } from "./ui";

export function statusTone(s: ResumeVersion["status"]): Tone {
  return s === "extracted" ? "success" : s === "failed" ? "danger" : s === "extracting" ? "warning" : "neutral";
}

export function versionFacts(v: ResumeVersion): string {
  return [
    v.used_by.length ? `used by ${plural(v.used_by.length, "application")}` : "not used by an application now",
    v.digest ? `${plural(v.digest.facts, "fact")}${v.digest.built_at ? `, read ${when(v.digest.built_at)}` : ""}` : null,
    v.career_os ? `pulled from Career-OS ${when(v.career_os.pulled_at)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function Resumes({ active = true, onOpenApplication }: { active?: boolean; onOpenApplication?: (id: string) => void }) {
  const [base, setBase] = useState<ResumeVersion | null>(null);
  const [versions, setVersions] = useState<ResumeVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [replacing, setReplacing] = useState(false);
  const [renaming, setRenaming] = useState<ResumeVersion | null>(null);
  const [deleting, setDeleting] = useState<ResumeVersion | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const r = await rest.resumes();
      setBase(r.base);
      setVersions(r.versions);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(active, load);
  usePolling(load, 3000, [base, ...(versions ?? [])].some((v) => v?.status === "extracting"));

  const tailored = versions === null ? null : [...versions].sort((a, b) => (a.application?.company ?? "").localeCompare(b.application?.company ?? "") || a.label.localeCompare(b.label));

  return (
    <>
      <PageHeader title="Resumes" meta="Applications without a tailored resume use the base resume." />
      <section className="ui-section" aria-labelledby="base-title">
        <h2 className="ui-section-title" id="base-title">
          Base resume
        </h2>
        {versions === null && !error ? (
          <SkeletonList rows={1} label="Loading resumes" />
        ) : error && versions === null ? (
          <Banner tone="danger" title="Could not load resumes" action={<Button size="sm" onClick={load}>Retry</Button>}>
            {error}
          </Banner>
        ) : base ? (
          <Card
            title={
              <span className="ui-row-inline">
                <span className="ui-truncate" title={base.label}>
                  {base.label}
                </span>
                <Badge tone={statusTone(base.status)}>{resumeStatusLabel(base.status)}</Badge>
              </span>
            }
            actions={
              <>
                <Button onClick={() => setRenaming(base)}>Rename…</Button>
                <Button variant="primary" onClick={() => setReplacing(true)}>
                  Replace…
                </Button>
              </>
            }
          >
            <p className="ui-text-2 resume-facts">{versionFacts(base)}</p>
          </Card>
        ) : (
          <EmptyState
            icon="▧"
            title="Add your base resume"
            action={
              <Button variant="primary" onClick={() => setReplacing(true)}>
                Add resume…
              </Button>
            }
          >
            Answers draw on it for every call. PDF, Markdown or text, or paste it.
          </EmptyState>
        )}
      </section>

      <section className="ui-section" aria-labelledby="tailored-title">
        <h2 className="ui-section-title" id="tailored-title">
          Tailored resumes
        </h2>
        {versions !== null && (
        <ListState
          items={tailored}
          error={null}
          loadingLabel="tailored resumes"
          empty={<EmptyState title="No tailored resumes">Add one to an application, on its Resume tab.</EmptyState>}
        >
          {(list) => (
            <List label="Tailored resumes">
              {list.map((v) => (
                <ListRow
                  key={v.id}
                  title={v.label}
                  fullTitle={v.label}
                  meta={`${v.application ? `${v.application.company} · ${v.application.role} · ` : ""}${versionFacts(v)}`}
                  trailing={<Badge tone={statusTone(v.status)}>{resumeStatusLabel(v.status)}</Badge>}
                  actions={
                    <Menu
                      label={`Actions for ${v.label}`}
                      trigger="⋯"
                      items={[
                        ...(v.application && onOpenApplication ? [{ label: "Open application", onSelect: () => onOpenApplication(v.application!.id) }] : []),
                        { label: "Rename…", onSelect: () => setRenaming(v) },
                        "separator" as const,
                        { label: "Delete…", danger: true, onSelect: () => setDeleting(v) },
                      ]}
                    />
                  }
                />
              ))}
            </List>
          )}
        </ListState>
        )}
      </section>

      <ReplaceBase open={replacing} onOpenChange={setReplacing} hasBase={Boolean(base)} onDone={load} />
      <Rename version={renaming} onClose={() => setRenaming(null)} onDone={load} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete "${deleting?.label ?? ""}"?`}
        confirmLabel="Delete resume"
        onConfirm={async () => {
          if (!deleting) return;
          await rest.deleteResume(deleting.id);
          toast({ tone: "success", text: "Deleted." });
          await load();
        }}
      >
        Its facts are deleted too. {deleting?.application ? `${deleting.application.company} then uses its next resume (the base, unless another is picked).` : ""}
      </ConfirmDialog>
    </>
  );
}

function ReplaceBase({ open, onOpenChange, hasBase, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; hasBase: boolean; onDone: () => void }) {
  const [label, setLabel] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();
  useEffect(() => {
    if (open) {
      setLabel("");
      setText("");
      setError(null);
    }
  }, [open]);
  const run = async (what: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setError(null);
    try {
      await what();
      toast({ tone: "success", text: done });
      onOpenChange(false);
      onDone();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const save = () => {
    if (!text.trim()) {
      setError("Paste the resume's text, or upload a file.");
      return;
    }
    run(() => rest.createResume({ kind: "base", label: label.trim() || null, text }), "Saved. Its facts and digest are being rebuilt.");
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={hasBase ? "Replace the base resume" : "Add your base resume"}
      description={hasBase ? "Its facts are rebuilt from the new one; tailored resumes keep theirs." : "PDF, Markdown or text, or paste it."}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={save}>
            Save pasted text
          </Button>
        </>
      }
    >
      <div className="ui-row-inline">
        <input
          ref={file}
          type="file"
          accept={ACCEPT}
          hidden
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            if (isTextFile(f.name)) setText(await f.text());
            else run(() => api.uploadDocument("resume", f, { label: label.trim() || null }), "Uploaded. Its facts are rebuilt once it is read.");
          }}
        />
        <Button onClick={() => file.current?.click()} disabled={busy}>
          Upload a file…
        </Button>
        <span className="ui-muted">or paste it below</span>
      </div>
      <Field label="Label" optional>
        <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Resume 2026" />
      </Field>
      <Field label="Resume text" error={error}>
        <Textarea rows={10} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
    </Dialog>
  );
}

function Rename({ version, onClose, onDone }: { version: ResumeVersion | null; onClose: () => void; onDone: () => void }) {
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setLabel(version?.label ?? "");
    setError(null);
  }, [version]);
  const save = async () => {
    if (!version) return;
    if (!label.trim()) {
      setError("Enter a name.");
      return;
    }
    setBusy(true);
    try {
      await rest.updateResume(version.id, { label: label.trim() });
      onClose();
      onDone();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={version !== null}
      onOpenChange={(o) => !o && onClose()}
      size="sm"
      title="Rename resume"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <Field label="Name" error={error}>
          <Input value={label} onChange={(e) => setLabel(e.target.value)} autoFocus />
        </Field>
      </form>
    </Dialog>
  );
}
