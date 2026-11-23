import { Children, forwardRef, isValidElement, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type SelectHTMLAttributes } from "react";
import { createPortal } from "react-dom";
import { cx } from "./Button";

type Option = { value: string; label: string; disabled: boolean; group?: string; direction?: string; language?: string };
function text(node: ReactNode): string {
  return Children.toArray(node).map(child => isValidElement<{ children?: ReactNode }>(child) ? text(child.props.children) : String(child)).join("");
}
function optionsFrom(children: ReactNode, group?: string, disabled = false): Option[] {
  return Children.toArray(children).flatMap(child => {
    if (!isValidElement<{ children?: ReactNode; value?: string; label?: string; disabled?: boolean; dir?: string; lang?: string }>(child)) return [];
    const props = child.props;
    if (child.type === "option") return [{ value: String(props.value ?? text(props.children)), label: props.label ?? text(props.children), disabled: disabled || !!props.disabled, group, direction: props.dir, language: props.lang }];
    return optionsFrom(props.children, child.type === "optgroup" ? props.label : group, disabled || !!props.disabled);
  });
}

function searchText(value: string) {
  return value.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase().trim();
}

/** A popup is portaled after the page, so Tab must resume at its trigger's position. */
function advanceFocus(trigger: HTMLButtonElement | null, backwards: boolean) {
  if (!trigger) return;
  const scope = trigger.closest(".ui-dialog") ?? document.body;
  const controls = [...scope.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex]')].filter(element => {
    if (element.tabIndex < 0 || element.matches(":disabled") || element.closest('[hidden], [inert], [aria-hidden="true"], .ui-select-searchable')) return false;
    const closed = element.closest("details:not([open])");
    if (closed && closed.querySelector("summary") !== element) return false;
    for (let parent: HTMLElement | null = element; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    return true;
  });
  const next = controls[controls.indexOf(trigger) + (backwards ? -1 : 1)];
  (next ?? trigger).focus({ preventScroll: true });
}

/** A themed select; searchLabel opts into an editable search popup for large catalogs.
 * A hidden native select preserves form values and the existing change-event API. */
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement> & { displayValue?: ReactNode; searchLabel?: string }>(function Select(props, forwardedRef) {
  const { children, className, value, defaultValue, disabled, id, onChange, displayValue, searchLabel, ...rest } = props;
  const generated = useId();
  const listId = `${generated}-options`;
  const native = useRef<HTMLSelectElement | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [local, setLocal] = useState(defaultValue);
  const [query, setQuery] = useState("");
  const [position, setPosition] = useState({ left: 0, top: 0, width: 0, maxHeight: 0 });
  const search = useRef({ value: "", at: 0 });
  const options = optionsFrom(children);
  const selected = options.findIndex(o => o.value === String(value ?? local ?? options[0]?.value));
  const enabled = options.map((o, i) => o.disabled ? -1 : i).filter(i => i >= 0);
  const visible = options.map((option, index) => ({ option, index })).filter(({ option }) => !searchLabel || searchText(`${option.label} ${option.value}`).includes(searchText(query)));
  const matching = visible.filter(({ option }) => !option.disabled).map(({ index }) => index);
  const close = () => setOpen(false);
  const choose = (index: number) => {
    const option = options[index];
    if (!option || option.disabled || !native.current) return;
    native.current.value = option.value;
    setLocal(option.value);
    native.current.dispatchEvent(new Event("change", { bubbles: true }));
    close();
    trigger.current?.focus({ preventScroll: true });
  };
  const show = (last = false) => {
    if (disabled || !enabled.length) return;
    setQuery("");
    setActive(last ? enabled[enabled.length - 1] : enabled.includes(selected) ? selected : enabled[0]);
    setOpen(true);
  };
  useLayoutEffect(() => {
    if (!open) return;
    const fit = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const gap = 4, edge = 8;
      const below = window.innerHeight - rect.bottom - gap - edge;
      const above = rect.top - gap - edge;
      const up = below < Math.min(240, options.length * 32 + (searchLabel ? 76 : 8)) && above > below;
      const maxHeight = Math.max(0, Math.min(searchLabel ? 340 : 280, up ? above : below));
      const height = Math.min(popup.current ? popup.current.scrollHeight + 2 : maxHeight, maxHeight);
      const width = Math.min(Math.max(rect.width, 160), window.innerWidth - edge * 2);
      const dialog = trigger.current?.closest<HTMLElement>(".ui-dialog");
      const origin = dialog?.getBoundingClientRect();
      setPosition({ left: Math.max(edge, Math.min(rect.left, window.innerWidth - width - edge)) - (origin ? origin.left + dialog!.clientLeft : 0), top: (up ? Math.max(edge, rect.top - gap - height) : rect.bottom + gap) - (origin ? origin.top + dialog!.clientTop : 0), width, maxHeight });
    };
    fit();
    const observer = new ResizeObserver(fit);
    if (trigger.current) observer.observe(trigger.current);
    if (popup.current) observer.observe(popup.current);
    window.addEventListener("resize", fit);
    const onScroll = (event: Event) => { if (!popup.current?.contains(event.target as Node)) close(); };
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !popup.current?.contains(event.target as Node)) close();
    };
    const blur = () => close();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("blur", blur);
    document.addEventListener("pointerdown", outside);
    return () => {
      observer.disconnect(); window.removeEventListener("resize", fit);
      window.removeEventListener("scroll", onScroll, true); window.removeEventListener("blur", blur);
      document.removeEventListener("pointerdown", outside);
    };
  }, [open, options.length, searchLabel]);
  useEffect(() => { if (disabled) close(); }, [disabled]);
  useEffect(() => { if (open && searchLabel) searchInput.current?.focus({ preventScroll: true }); }, [open, searchLabel]);
  useEffect(() => {
    if (open) document.getElementById(`${listId}-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [open, active, listId]);

  // Multi-select/list controls retain their native semantics.
  if (props.multiple || (props.size ?? 0) > 1) return <select {...rest} value={value} defaultValue={defaultValue} disabled={disabled} id={id} onChange={onChange} ref={forwardedRef} className={cx("ui-input", className)}>{children}</select>;
  const optionRows = visible.map(({ option, index }, at) => <div key={`${option.value}-${index}`}>
    {option.group && option.group !== visible[at - 1]?.option.group && <div className="ui-select-group">{option.group}</div>}
    <div id={`${listId}-${index}`} role="option" aria-selected={index === selected} aria-disabled={option.disabled || undefined}
      className={cx("ui-select-option", active === index && "is-active")} onPointerMove={() => !option.disabled && setActive(index)}
      onClick={() => choose(index)}><bdi dir={option.direction ?? "auto"} lang={option.language}>{option.label}</bdi><span aria-hidden="true">{index === selected ? "✓" : ""}</span></div>
  </div>);
  return <span className={cx("ui-select-wrap", className)}>
    <select {...rest} ref={element => {
      native.current = element;
      if (typeof forwardedRef === "function") forwardedRef(element);
      else if (forwardedRef) forwardedRef.current = element;
    }} hidden aria-hidden="true" tabIndex={-1} disabled={disabled} value={value} defaultValue={defaultValue} onChange={onChange}
      onInvalid={e => { e.preventDefault(); trigger.current?.focus(); props.onInvalid?.(e); }}>{children}</select>
    <button ref={trigger} id={id} type="button" value={options[selected]?.value ?? ""} role={searchLabel ? undefined : "combobox"} className="ui-input ui-select" disabled={disabled}
      aria-label={props["aria-label"]} aria-labelledby={props["aria-labelledby"]} aria-describedby={[props["aria-describedby"], searchLabel ? `${generated}-selected` : undefined].filter(Boolean).join(" ") || undefined}
      aria-invalid={props["aria-invalid"]} aria-required={searchLabel ? undefined : props.required} aria-expanded={open} aria-haspopup={searchLabel ? "dialog" : "listbox"}
      aria-controls={open ? searchLabel ? `${generated}-popup` : listId : undefined} aria-activedescendant={open && !searchLabel ? `${listId}-${active}` : undefined}
      title={props.title ?? options[selected]?.label} onClick={() => open ? close() : show()}
      onBlur={event => { if (!searchLabel || !popup.current?.contains(event.relatedTarget)) close(); }} onKeyDown={event => {
        const { key } = event;
        if (key === "Escape") {
          if (open) { event.preventDefault(); event.stopPropagation(); close(); }
          return;
        }
        if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(key)) {
          event.preventDefault();
          if (key === "Enter" || key === " ") { if (open) choose(active); else show(); return; }
          if (!open) { show(key === "End"); return; }
          const at = enabled.indexOf(active);
          const next = key === "Home" ? enabled[0] : key === "End" ? enabled[enabled.length - 1] : enabled[(at + (key === "ArrowDown" ? 1 : -1) + enabled.length) % enabled.length];
          if (next != null) setActive(next);
        } else if (key === "Tab") close();
        else if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          if (searchLabel) {
            event.preventDefault(); show(); setQuery(key);
            setActive(options.findIndex(option => !option.disabled && searchText(`${option.label} ${option.value}`).includes(searchText(key))));
            return;
          }
          const now = Date.now();
          search.current = { value: now - search.current.at < 700 ? search.current.value + key : key, at: now };
          const query = search.current.value.toLocaleLowerCase();
          const index = enabled.find(i => options[i].label.toLocaleLowerCase().startsWith(query));
          if (index != null) { if (open) setActive(index); else choose(index); }
        }
      }}><span id={searchLabel ? `${generated}-selected` : undefined}><bdi dir={options[selected]?.direction ?? "auto"} lang={options[selected]?.language}>{displayValue ?? options[selected]?.label ?? "Choose…"}</bdi></span></button>
    {open && createPortal(<div ref={popup} id={searchLabel ? `${generated}-popup` : listId} role={searchLabel ? "dialog" : "listbox"} aria-label={props["aria-label"]}
      aria-labelledby={props["aria-label"] ? undefined : id} className={cx("ui-select-menu", searchLabel && "ui-select-searchable")} style={position}
      onBlur={event => { if (searchLabel && !popup.current?.contains(event.relatedTarget) && !trigger.current?.contains(event.relatedTarget)) close(); }}
      onPointerDown={e => { if ((e.target as Element).closest('[role="option"]')) e.preventDefault(); }}>
      {searchLabel ? <>
        <div className="ui-select-search"><input ref={searchInput} type="text" role="combobox" className="ui-input" value={query}
          aria-label={searchLabel} placeholder={searchLabel} aria-expanded="true" aria-autocomplete="list" aria-controls={listId}
          aria-activedescendant={matching.includes(active) ? `${listId}-${active}` : undefined} autoComplete="off" autoCorrect="off" spellCheck={false}
          onChange={event => {
            const next = event.target.value; setQuery(next);
            setActive(options.findIndex(option => !option.disabled && searchText(`${option.label} ${option.value}`).includes(searchText(next))));
          }} onKeyDown={event => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); trigger.current?.focus({ preventScroll: true }); }
            else if (event.key === "Enter") { event.preventDefault(); if (matching.includes(active)) choose(active); }
            else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              const at = matching.indexOf(active);
              const next = matching[(at + (event.key === "ArrowDown" ? 1 : -1) + matching.length) % matching.length];
              if (next != null) setActive(next);
            } else if (event.key === "Tab") {
              event.preventDefault(); advanceFocus(trigger.current, event.shiftKey); close();
            }
          }} /></div>
        <div id={listId} role="listbox" aria-label={props["aria-label"]} aria-labelledby={props["aria-label"] ? undefined : id} className="ui-select-results">{optionRows}</div>
        <p className="ui-select-result-count" role="status">{visible.length ? `${visible.length} ${visible.length === 1 ? "result" : "results"}` : "No matches. Try another name or language code."}</p>
      </> : optionRows}
    </div>, trigger.current?.closest(".ui-dialog") ?? document.fullscreenElement ?? document.body)}
  </span>;
});
