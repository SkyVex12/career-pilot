import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useOverlayPopover } from "./overlayPopover";
import { GUESTS_CHANGED, normalizeGuestEmail, recentGuests, validGuestEmail, type GuestPerson, type PlannedGuest } from "./guestStorage";
import { Button, Checkbox, HelpTip, Input } from "./ui";
import { Icon } from "./ui/Icon";

export function GuestPicker({ scope, selected, onChange, disabled }: { scope: string; selected: PlannedGuest[]; onChange: (guests: PlannedGuest[]) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [recents, setRecents] = useState(() => recentGuests(scope));
  const button = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null), input = useRef<HTMLInputElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const position = useOverlayPopover(open, close, button, panel, "start");
  useEffect(() => {
    const update = () => setRecents(recentGuests(scope));
    update(); window.addEventListener("storage", update); window.addEventListener(GUESTS_CHANGED, update);
    return () => { window.removeEventListener("storage", update); window.removeEventListener(GUESTS_CHANGED, update); };
  }, [scope]);
  useEffect(() => { if (open) input.current?.focus(); }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  const query = search.trim().toLowerCase();
  const people: GuestPerson[] = recents.filter(p => `${p.name ?? ""} ${p.email}`.toLowerCase().includes(query));
  for (const guest of selected) if (!people.some(p => p.email === guest.email) && `${guest.name ?? ""} ${guest.email}`.toLowerCase().includes(query)) people.push(guest);
  if (validGuestEmail(query) && !people.some(p => p.email === query)) people.push({ email: normalizeGuestEmail(query) });
  const toggle = (person: GuestPerson, checked: boolean) => onChange(checked ? [...selected, { ...person, screen: false, control: false }].slice(0, 2) : selected.filter(p => p.email !== person.email));
  const permission = (email: string, kind: "screen" | "control", checked: boolean) => onChange(selected.map(p => p.email !== email ? p : { ...p, [kind]: checked, ...(kind === "control" && checked ? { screen: true } : {}), ...(kind === "screen" && !checked ? { control: false } : {}) }));
  return <div className="call-guests">
    <div className="call-row"><span className="call-row-label">Assistants<HelpTip label="Assistants">Invite up to two assistants to send notes and talk to you. Invitations are sent when your call starts.</HelpTip></span><div className="call-guest-selection">
      {selected.map(p => <span className="call-guest-chip" key={p.email} title={p.email}><span>{p.name || p.email}</span><button type="button" disabled={disabled} aria-label={`Remove ${p.name || p.email}`} onClick={() => toggle(p, false)}><Icon name="close" size={12} /></button></span>)}
      <Button ref={button} size="sm" disabled={disabled} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)}>{selected.length ? "Change assistants" : "Add assistants"}<Icon name="chevronDown" size={14} /></Button>
    </div></div>
    {open && createPortal(<div ref={panel} className="call-popover guest-picker" role="dialog" aria-label="Add assistants" style={position}>
      <div className="guest-picker-search"><Input ref={input} aria-label="Find assistant by name or email" placeholder="Name or email" value={search} onChange={e => setSearch(e.target.value)} /><span className="ui-field-hint">{selected.length} of 2 assistants</span></div>
      <div className="guest-picker-people">{people.length ? people.map(p => {
        const chosen = selected.find(g => g.email === p.email);
        return <div className="guest-picker-person" key={p.email}><Checkbox checked={!!chosen} disabled={!chosen && selected.length >= 2} onChange={on => toggle(p, on)} label={<span className="guest-person-name"><strong>{p.name || p.email}</strong><small>{p.name ? p.email : "Name will appear when they connect"}</small></span>} />
          {chosen && <div className="guest-permissions"><Checkbox checked={chosen.screen} onChange={on => permission(p.email, "screen", on)} label="View my shared screen" /><Checkbox checked={chosen.control} onChange={on => permission(p.email, "control", on)} label="Control my mouse and keyboard" /></div>}
        </div>;
      }) : <p className="ui-muted">{query ? "Enter a complete email address." : "Enter an email to add an assistant."}</p>}</div>
      <p className="guest-picker-footer">Assistants can send notes and talk to you.</p>
    </div>, document.body)}
  </div>;
}
