// Work: the companies the user works at (apart from the jobs they apply to), and per company
// a daily work log, its material (documents whose facts wait for approval) and its facts.
// Standup and meeting mode answer from these (the start screen picks the company).
import { useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type CompanyDocument, type Employer, type MemoryItem, type Proposal, type WorkLog } from "./api";
import { Chip, when } from "./Documents";
import { usePolling } from "./usePolling";
import { KIND_LABELS, byKind, itemLine, localDay } from "./workday";

const ACCEPT = ".pdf,.md,.markdown,.txt";
type Sub = "log" | "material" | "facts";

export function Work() {
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const list = (await rest.employers()).employers;
      setEmployers(list);
      setSelected((cur) => (cur && list.some((e) => e.id === cur) ? cur : (list.find((e) => e.current) ?? list[0])?.id ?? null));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const employer = employers?.find((e) => e.id === selected) ?? null;
  return (
    <section className="panel">
      <div className="companies">
        {employers?.map((e) => (
          <button key={e.id} className={e.id === selected ? "on" : ""} onClick={() => setSelected(e.id)}>
            {e.name}
            {e.status === "archived" ? " (archived)" : e.current ? "" : " (past)"}
            {(e.pending_proposals ?? 0) > 0 && <span className="count">{e.pending_proposals}</span>}
          </button>
        ))}
        <button className="link" onClick={() => setAdding(true)}>
          + Add a company
        </button>
      </div>
      <p className="note">
        Companies you work at, apart from the jobs you apply to. Standup and meeting mode answer from the company you pick on the start screen.
      </p>
      {error && <p className="note warn">{error}</p>}
      {employers?.length === 0 && !adding && <p className="callout">No company yet: add the one you work at.</p>}
      {(adding || employers?.length === 0) && (
        <EmployerForm
          onCancel={() => setAdding(false)}
          onSaved={async (e) => {
            setAdding(false);
            await load();
            setSelected(e.id);
          }}
        />
      )}
      {employer && !adding && <Company key={employer.id} employer={employer} onChanged={load} />}
    </section>
  );
}

function EmployerForm({ employer, onSaved, onCancel }: { employer?: Employer; onSaved: (e: Employer) => void; onCancel: () => void }) {
  const [name, setName] = useState(employer?.name ?? "");
  const [title, setTitle] = useState(employer?.title ?? "");
  const [team, setTeam] = useState(employer?.team ?? "");
  const [started, setStarted] = useState(employer?.started_on ?? "");
  const [ended, setEnded] = useState(employer?.ended_on ?? "");
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    const fields = { name: name.trim(), title: title.trim(), team: team.trim(), started_on: started || null, ended_on: ended || null };
    try {
      onSaved(employer ? await rest.updateEmployer(employer.id, fields) : await rest.createEmployer(fields));
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <div className="grid form">
      <label>Company</label>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Fernhill Logistics" />
      <label>Your role</label>
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Senior Backend Engineer" />
      <label>Team</label>
      <input value={team} onChange={(e) => setTeam(e.target.value)} placeholder="Optional" />
      <label>Started</label>
      <input type="date" value={started} onChange={(e) => setStarted(e.target.value)} />
      <label>Ended</label>
      <div>
        <input type="date" value={ended} onChange={(e) => setEnded(e.target.value)} />
        <p className="note">Leave empty while you work there.</p>
      </div>
      <span />
      <div className="actions">
        <button className="primary" disabled={!name.trim()} onClick={save}>
          {employer ? "Save" : "Add company"}
        </button>
        <button onClick={onCancel}>Cancel</button>
        {error && <span className="note warn">{error}</span>}
      </div>
    </div>
  );
}

function Company({ employer, onChanged }: { employer: Employer; onChanged: () => void }) {
  const [sub, setSub] = useState<Sub>("log");
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const archive = async () => {
    try {
      await rest.updateEmployer(employer.id, { status: employer.status === "archived" ? "active" : "archived" });
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };
  if (editing) {
    return (
      <EmployerForm
        employer={employer}
        onCancel={() => setEditing(false)}
        onSaved={() => {
          setEditing(false);
          onChanged();
        }}
      />
    );
  }
  return (
    <div className="company">
      <h3>
        {employer.name}
        <span className="muted">{[employer.title, employer.team].filter(Boolean).join(" · ")}</span>
      </h3>
      <div className="actions">
        <button className="link" onClick={() => setEditing(true)}>
          Edit
        </button>
        <button className="link" onClick={archive}>
          {employer.status === "archived" ? "Unarchive" : "Archive"}
        </button>
        <button className="link danger-text" onClick={() => setDeleting(true)}>
          Delete all data for this company…
        </button>
      </div>
      {deleting && <DeleteCompany employer={employer} onCancel={() => setDeleting(false)} onDeleted={onChanged} />}
      {error && <p className="note warn">{error}</p>}
      <div className="segmented">
        {(
          [
            ["log", "Work log"],
            ["material", "Material"],
            ["facts", "Facts"],
          ] as [Sub, string][]
        ).map(([k, label]) => (
          <button key={k} className={sub === k ? "on" : ""} onClick={() => setSub(k)}>
            {label}
            {k === "material" && (employer.pending_proposals ?? 0) > 0 ? ` (${employer.pending_proposals} to review)` : ""}
          </button>
        ))}
      </div>
      {sub === "log" && <LogTab employer={employer} />}
      {sub === "material" && <MaterialTab employer={employer} onDecided={onChanged} />}
      {sub === "facts" && <FactsTab employer={employer} />}
    </div>
  );
}

function DeleteCompany({ employer, onCancel, onDeleted }: { employer: Employer; onCancel: () => void; onDeleted: () => void }) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    try {
      await rest.deleteEmployer(employer.id, typed);
      onDeleted();
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <div className="callout">
      <p>
        This deletes everything for {employer.name}: its work logs and attachments, documents, facts, and its standup and meeting sessions with their
        transcripts. It cannot be undone. Type the company's name to confirm.
      </p>
      <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={employer.name} />
      <div className="actions">
        <button className="danger" disabled={typed.trim().toLowerCase() !== employer.name.trim().toLowerCase()} onClick={go}>
          Delete all data
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="note warn">{error}</p>}
    </div>
  );
}

// --- Work log ------------------------------------------------------------------------------

function LogTab({ employer }: { employer: Employer }) {
  const [day, setDay] = useState(localDay());
  const [entries, setEntries] = useState<WorkLog[] | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setEntries((await rest.logs(employer.id)).logs);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [employer.id]);
  useEffect(() => {
    load();
  }, [load]);
  const entry = entries?.find((e) => e.day === day) ?? null;
  useEffect(() => {
    if (!dirty) setText(entry?.text ?? "");
  }, [entry?.text, day]); // eslint-disable-line react-hooks/exhaustive-deps
  usePolling(load, 3000, entries?.some((e) => e.status === "extracting") ?? false);

  const save = async () => {
    setBusy(true);
    try {
      await rest.putLog(employer.id, day, text);
      setDirty(false);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const attach = async (f: File) => {
    setBusy(true);
    try {
      if (dirty || !entry) await rest.putLog(employer.id, day, text);
      setDirty(false);
      await api.uploadCompanyFile(`/employers/${employer.id}/logs/${day}/attachments`, f);
      await load();
    } catch (e) {
      setError(`${f.name}: ${e}`);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!entry || !confirm(`Delete the log for ${day} and the facts that came from it?`)) return;
    try {
      await rest.deleteLog(employer.id, day);
      setText("");
      setDirty(false);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <div className="grid">
        <label>Day</label>
        <input
          type="date"
          value={day}
          onChange={(e) => {
            setDay(e.target.value);
            setDirty(false);
          }}
        />
        <label>What you worked on</label>
        <div>
          <textarea
            rows={10}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setDirty(true);
            }}
            placeholder={"Tasks, tickets and PRs, decisions, blockers, plans for tomorrow.\nPaste from your notes, Jira or Slack as it is."}
          />
          <div className="actions">
            <button className="primary" disabled={busy || !text.trim() || !dirty} onClick={save}>
              {entry ? "Save changes" : "Save"}
            </button>
            <input
              ref={file}
              type="file"
              accept={ACCEPT}
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) attach(f);
                e.target.value = "";
              }}
            />
            <button disabled={busy || !text.trim()} onClick={() => file.current?.click()}>
              Attach a file…
            </button>
            {entry && (
              <button className="link danger-text" onClick={remove}>
                Delete this day
              </button>
            )}
            {entry && <LogStatus log={entry} />}
          </div>
          {entry && entry.attachments.length > 0 && (
            <ul className="files">
              {entry.attachments.map((a) => (
                <li key={a.id}>
                  {a.name} <span className="muted">{Math.ceil(a.size_bytes / 1024)} KB</span>{" "}
                  <button className="link" onClick={() => rest.deleteCompanyDocument(employer.id, a.id).then(load, (e) => setError(String(e)))}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {error && <p className="note warn">{error}</p>}
      {entry?.items && entry.items.length > 0 && (
        <>
          <h3>From this day's log</h3>
          <p className="note">Your own log is kept as it is; fix anything that came out wrong.</p>
          <Facts items={entry.items} onChanged={load} />
        </>
      )}
      <h3>Past entries</h3>
      {entries?.length === 0 && <p className="note">No entries yet.</p>}
      <table className="list clickable">
        <tbody>
          {entries?.map((e) => (
            <tr
              key={e.id}
              onClick={() => {
                setDay(e.day);
                setDirty(false);
              }}
            >
              <td>{e.day}</td>
              <td className="muted">{e.text.split("\n")[0].slice(0, 90)}</td>
              <td>
                <LogStatus log={e} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LogStatus({ log }: { log: WorkLog }) {
  switch (log.status) {
    case "extracting":
      return <Chip state="busy">Reading</Chip>;
    case "extracted":
      return <Chip state="ok">{log.items?.length ?? 0} facts</Chip>;
    case "failed":
      return <Chip state="bad">Could not read it; save again</Chip>;
    default:
      return <Chip state="off">Saved</Chip>;
  }
}

// --- Material --------------------------------------------------------------------------------

function MaterialTab({ employer, onDecided }: { employer: Employer; onDecided: () => void }) {
  const [docs, setDocs] = useState<CompanyDocument[] | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const [d, p] = await Promise.all([rest.companyDocuments(employer.id), rest.proposals(employer.id)]);
      setDocs(d.documents);
      setProposals(p.proposals);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [employer.id]);
  useEffect(() => {
    load();
  }, [load]);
  usePolling(load, 3000, docs?.some((d) => d.status === "ingesting") ?? false);

  const upload = async (f: File) => {
    setBusy(true);
    try {
      await api.uploadCompanyFile(`/employers/${employer.id}/documents`, f);
      await load();
    } catch (e) {
      setError(`${f.name}: ${e}`);
    } finally {
      setBusy(false);
    }
  };
  const decide = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
      onDecided();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <div className="actions">
        <input
          ref={file}
          type="file"
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload(f);
            e.target.value = "";
          }}
        />
        <button disabled={busy} onClick={() => file.current?.click()}>
          {busy ? "Uploading…" : "Upload a document…"}
        </button>
        <span className="note">Architecture notes, wiki pages, meeting notes, specs: PDF, Markdown or text. You review its facts before they are kept.</span>
      </div>
      {error && <p className="note warn">{error}</p>}
      {docs?.length === 0 && <p className="note">No documents yet.</p>}
      {docs?.map((d) => {
        const mine = proposals.filter((p) => p.company_document_id === d.id);
        return (
          <div key={d.id} className="doc">
            <div className="doc-head">
              <strong>{d.name}</strong> <span className="muted">{d.created_at ? when(d.created_at) : ""}</span>{" "}
              {d.status === "ingesting" ? (
                <Chip state="busy">Reading</Chip>
              ) : d.status === "failed" ? (
                <Chip state="bad">Could not read it</Chip>
              ) : (
                <Chip state="ok">{d.approved_proposals ?? 0} facts kept</Chip>
              )}
              {mine.length > 0 && (
                <>
                  <button className="primary small" onClick={() => decide(() => rest.decideDocument(d.id, "approve"))}>
                    Approve all {mine.length}
                  </button>
                  <button className="small" onClick={() => decide(() => rest.decideDocument(d.id, "reject"))}>
                    Reject all
                  </button>
                </>
              )}
              <button className="link danger-text" onClick={() => decide(() => rest.deleteCompanyDocument(employer.id, d.id))}>
                Delete
              </button>
            </div>
            {mine.map((p) => (
              <ProposalRow key={p.id} proposal={p} decide={decide} />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function ProposalRow({ proposal, decide }: { proposal: Proposal; decide: (fn: () => Promise<unknown>) => void }) {
  const [summary, setSummary] = useState(proposal.summary);
  const [editing, setEditing] = useState(false);
  return (
    <div className="proposal">
      <span className="kind">{KIND_LABELS[proposal.kind] ?? proposal.kind}</span>
      <div>
        <strong>{proposal.title}</strong>
        {editing ? <input value={summary} onChange={(e) => setSummary(e.target.value)} /> : <span>: {summary}</span>}
        {proposal.evidence && <div className="muted evidence">“{proposal.evidence}”</div>}
      </div>
      <div className="actions">
        <button
          className="small"
          onClick={() => decide(() => rest.approveProposal(proposal.id, summary !== proposal.summary ? { summary } : undefined))}
        >
          Approve
        </button>
        <button className="link" onClick={() => setEditing((v) => !v)}>
          Edit
        </button>
        <button className="link" onClick={() => decide(() => rest.rejectProposal(proposal.id))}>
          Reject
        </button>
      </div>
    </div>
  );
}

// --- Facts -------------------------------------------------------------------------------------

function FactsTab({ employer }: { employer: Employer }) {
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setItems((await rest.memory(employer.id)).memory);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [employer.id]);
  useEffect(() => {
    load();
  }, [load]);
  return (
    <div>
      <p className="note">What standups and meetings at {employer.name} can draw on. Work logs add to it; documents add what you approve.</p>
      {error && <p className="note warn">{error}</p>}
      {items?.length === 0 && <p className="note">Nothing yet: write a work log or upload a document.</p>}
      {items && <Facts items={items} onChanged={load} />}
    </div>
  );
}

function Facts({ items, onChanged }: { items: MemoryItem[]; onChanged: () => void }) {
  return (
    <>
      {byKind(items).map(([kind, list]) => (
        <div key={kind}>
          <h4>{KIND_LABELS[kind]}</h4>
          <ul className="facts">
            {list.map((i) => (
              <FactRow key={i.id} item={i} onChanged={onChanged} />
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

function FactRow({ item, onChanged }: { item: MemoryItem; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(item.title);
  const [summary, setSummary] = useState(item.summary);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setEditing(false);
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };
  if (editing) {
    return (
      <li>
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
        <input value={summary} onChange={(e) => setSummary(e.target.value)} />
        <button className="small" onClick={() => run(() => rest.updateMemory(item.id, { title, summary }))}>
          Save
        </button>
        <button className="link" onClick={() => setEditing(false)}>
          Cancel
        </button>
        {error && <span className="note warn">{error}</span>}
      </li>
    );
  }
  return (
    <li>
      {item.day && <span className="muted">{item.day} </span>}
      {itemLine(item)}
      {item.edited && <span className="muted"> (edited)</span>}{" "}
      <button className="link" onClick={() => setEditing(true)}>
        Edit
      </button>
      <button className="link" onClick={() => run(() => rest.archiveMemory(item.id))}>
        Remove
      </button>
    </li>
  );
}
