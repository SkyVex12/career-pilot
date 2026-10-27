// Resumes: the base resume and the tailored versions (each owned by one application), uploaded
// as a file or pasted, with labels; their processing and each one's digest. Interview packs
// for an application use only the version it is paired with (ARCHITECTURE.md, "Resume versions").
import { useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type ResumeVersion } from "./api";
import { isTextFile, resumeStatusLabel } from "./pairing";
import { Chip, when } from "./ui";
import { usePolling } from "./usePolling";

export function Resumes() {
  const [base, setBase] = useState<ResumeVersion | null>(null);
  const [versions, setVersions] = useState<ResumeVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

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
  const pending = [base, ...(versions ?? [])].some((v) => v?.status === "extracting");
  usePolling(load, 3000, pending);

  const run = async (what: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      await what();
      setMessage(done);
      await load();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  const pick = (f: File) =>
    isTextFile(f.name)
      ? f.text().then(setText)
      : run(
          () => api.uploadDocument("resume", f, { label: label.trim() || null }),
          "Uploaded. It replaces your base resume's facts once read; tailored resumes keep theirs.",
        );

  return (
    <section className="panel">
      <h3>Base resume</h3>
      {base ? <VersionRow v={base} onChanged={load} /> : <p className="note">None yet: upload or paste your resume below.</p>}
      <div className="grid">
        <label>{base ? "Replace it" : "Add it"}</label>
        <div>
          <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Label (optional)" />
          <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste your resume" />
          <div className="row">
            <button
              className="primary"
              disabled={busy || !text.trim()}
              onClick={() =>
                run(async () => {
                  await rest.createResume({ kind: "base", label: label.trim() || null, text });
                  setText("");
                }, "Saved. Its facts and digest are being rebuilt.")
              }
            >
              Save pasted resume
            </button>
            <input
              ref={file}
              type="file"
              accept=".pdf,.md,.markdown,.txt"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) pick(f);
                e.target.value = "";
              }}
            />
            <button disabled={busy} onClick={() => file.current?.click()}>
              Upload a file…
            </button>
          </div>
          <p className="note">PDF, Markdown or text. Applications without a tailored resume use this one.</p>
        </div>
      </div>
      {message && <p className="note">{message}</p>}
      {error && <p className="note warn">{error}</p>}

      <h3>Tailored resumes</h3>
      {versions?.length === 0 && <p className="note">None yet: add one to an application under Applications.</p>}
      {versions?.map((v) => (
        <VersionRow key={v.id} v={v} onChanged={load} />
      ))}
    </section>
  );
}

function VersionRow({ v, onChanged }: { v: ResumeVersion; onChanged: () => void }) {
  const [label, setLabel] = useState(v.label);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setLabel(v.label), [v.label]);
  const state = v.status === "extracted" ? "ok" : v.status === "failed" ? "bad" : v.status === "extracting" ? "busy" : "off";
  const act = (what: () => Promise<unknown>) => what().then(onChanged, (e) => setError(String(e)));
  return (
    <div className="subpanel">
      <div className="row">
        <input value={label} onChange={(e) => setLabel(e.target.value)} onBlur={() => label.trim() && label !== v.label && act(() => rest.updateResume(v.id, { label: label.trim() }))} />
        <Chip state={state}>{resumeStatusLabel(v.status)}</Chip>
        {v.career_os && <Chip state="off">From Career-OS</Chip>}
        <span className="spacer" />
        {v.kind === "tailored" && (
          <button
            className="danger"
            onClick={() => window.confirm(`Delete “${v.label}” and its facts?`) && act(() => rest.deleteResume(v.id))}
          >
            Delete
          </button>
        )}
      </div>
      <p className="note">
        {v.application ? `For ${v.application.company} · ${v.application.role}. ` : ""}
        {v.used_by.length ? `Used by ${v.used_by.length} application${v.used_by.length > 1 ? "s" : ""}. ` : "Not used by an application now. "}
        {v.digest ? `Digest: ${v.digest.facts} facts${v.digest.built_at ? `, built ${when(v.digest.built_at)}` : ""}.` : "No digest yet."}
        {v.career_os ? ` Pulled ${when(v.career_os.pulled_at)}.` : ""}
      </p>
      {error && <p className="note warn">{error}</p>}
    </div>
  );
}
