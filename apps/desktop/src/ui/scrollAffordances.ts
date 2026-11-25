/** Native scrolling with edge cues instead of scrollbar tracks. Works for pages, portals and editors. */
type Edge = "top" | "bottom" | "left" | "right";
type Box = { left: number; top: number; right: number; bottom: number };
type Tracked = { cues: Map<Edge, HTMLElement>; addedTabStop: boolean };

export function scrollEdges(element: Pick<HTMLElement, "scrollTop" | "scrollLeft" | "scrollHeight" | "scrollWidth" | "clientHeight" | "clientWidth">, rtl = false): Edge[] {
  const maxY = Math.max(0, element.scrollHeight - element.clientHeight);
  const maxX = Math.max(0, element.scrollWidth - element.clientWidth);
  const y = Math.max(0, Math.min(maxY, element.scrollTop));
  const x = Math.max(0, Math.min(maxX, rtl ? maxX + element.scrollLeft : element.scrollLeft));
  const edges: Edge[] = [];
  // Fractional layout and scroll offsets should not leave a cue at the final pixel.
  if (y > 1) edges.push("top");
  if (maxY - y > 1) edges.push("bottom");
  if (x > 1) edges.push("left");
  if (maxX - x > 1) edges.push("right");
  return edges;
}

export function installScrollAffordances() {
  const host = document.createElement("div");
  host.className = "ui-scroll-cues";
  host.setAttribute("aria-hidden", "true");
  document.body.append(host);
  const tracked = new Map<HTMLElement, Tracked>();
  let frame = 0;
  let disposed = false;
  let discoverAll = true;
  const added = new Set<Element>();
  const scrollable = (value: string) => value === "auto" || value === "scroll";
  const clips = (value: string) => value !== "visible" && value !== "unset";
  const schedule = () => {
    if (!frame && !disposed) frame = requestAnimationFrame(update);
  };
  const resize = new ResizeObserver(schedule);
  const add = (element: Element) => {
    if (!(element instanceof HTMLElement) || element === host || host.contains(element) || tracked.has(element) || element.matches("input, select")) return;
    const style = getComputedStyle(element);
    if (!scrollable(style.overflowX) && !scrollable(style.overflowY) && element !== document.scrollingElement) return;
    tracked.set(element, { cues: new Map(), addedTabStop: false });
    resize.observe(element);
    // A child's height can change while the scroll viewport itself stays the same.
    for (const child of element.children) resize.observe(child);
  };
  const discover = (root: Element) => {
    add(root);
    for (const child of root.querySelectorAll("*")) add(child);
  };
  const clientBox = (element: HTMLElement): Box => {
    if (element === document.scrollingElement) return { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
    const rect = element.getBoundingClientRect();
    const sx = element.offsetWidth ? rect.width / element.offsetWidth : 1;
    const sy = element.offsetHeight ? rect.height / element.offsetHeight : 1;
    const left = rect.left + element.clientLeft * sx, top = rect.top + element.clientTop * sy;
    return { left, top, right: left + element.clientWidth * sx, bottom: top + element.clientHeight * sy };
  };
  const remove = (element: HTMLElement, state: Tracked) => {
    for (const cue of state.cues.values()) cue.remove();
    if (state.addedTabStop && element.getAttribute("tabindex") === "0") element.removeAttribute("tabindex");
    element.removeAttribute("data-scroll-cue-focus");
    resize.unobserve(element);
    for (const child of element.children) resize.unobserve(child);
    tracked.delete(element);
  };
  function update() {
    frame = 0;
    if (disposed) return;
    if (discoverAll) { discover(document.documentElement); discoverAll = false; }
    else for (const element of added) if (element.isConnected) discover(element);
    added.clear();
    const styles = new Map<HTMLElement, CSSStyleDeclaration>();
    const styleFor = (element: HTMLElement) => {
      let style = styles.get(element);
      if (!style) { style = getComputedStyle(element); styles.set(element, style); }
      return style;
    };
    for (const [element, state] of tracked) {
      if (!element.isConnected) { remove(element, state); continue; }
      const style = styleFor(element);
      const root = element === document.scrollingElement;
      const vertical = scrollable(style.overflowY) || (root && style.overflowY === "visible");
      const horizontal = scrollable(style.overflowX) || (root && style.overflowX === "visible");
      const edges = scrollEdges(element, style.direction === "rtl").filter(edge => edge === "top" || edge === "bottom" ? vertical : horizontal);
      const visible = style.visibility !== "hidden" && element.getClientRects().length > 0 && !element.closest("[hidden], [inert]");
      if (visible && edges.length && !element.hasAttribute("tabindex") && !element.matches("textarea, [contenteditable], button, a[href], [role=listbox], [role=menu]")) {
        element.tabIndex = 0;
        element.setAttribute("data-scroll-cue-focus", "");
        state.addedTabStop = true;
      } else if ((!visible || !edges.length) && state.addedTabStop) {
        if (element.getAttribute("tabindex") === "0") element.removeAttribute("tabindex");
        element.removeAttribute("data-scroll-cue-focus");
        state.addedTabStop = false;
      }

      const box = clientBox(element);
      const clip = { left: Math.max(0, box.left), top: Math.max(0, box.top), right: Math.min(innerWidth, box.right), bottom: Math.min(innerHeight, box.bottom) };
      let background = style.backgroundColor;
      for (let parent = element.parentElement; parent && parent !== document.documentElement; parent = parent.parentElement) {
        const parentStyle = styleFor(parent);
        const bounds = clientBox(parent);
        if (clips(parentStyle.overflowX)) { clip.left = Math.max(clip.left, bounds.left); clip.right = Math.min(clip.right, bounds.right); }
        if (clips(parentStyle.overflowY)) { clip.top = Math.max(clip.top, bounds.top); clip.bottom = Math.min(clip.bottom, bounds.bottom); }
        if (background === "rgba(0, 0, 0, 0)" || background === "transparent") background = parentStyle.backgroundColor;
      }
      const wanted = new Set<Edge>();
      if (visible && clip.right - clip.left >= 24 && clip.bottom - clip.top >= 24) {
        for (const edge of edges) {
          // Don't paint at an ancestor's clipping edge; its own cue describes that overflow.
          if (Math.abs(clip[edge] - box[edge]) > 1) continue;
          const verticalEdge = edge === "top" || edge === "bottom";
          const x = verticalEdge ? (clip.left + clip.right) / 2 : edge === "left" ? clip.left + 4 : clip.right - 4;
          const y = !verticalEdge ? (clip.top + clip.bottom) / 2 : edge === "top" ? clip.top + 4 : clip.bottom - 4;
          const hit = document.elementFromPoint(x, y);
          // Menus and modal backdrops cover the page's cues too.
          if (hit && hit !== element && !element.contains(hit)) continue;
          wanted.add(edge);
          let cue = state.cues.get(edge);
          if (!cue) {
            cue = document.createElement("span");
            cue.className = "ui-scroll-cue";
            cue.dataset.scrollEdge = edge;
            host.append(cue);
            state.cues.set(edge, cue);
          }
          const size = 24;
          Object.assign(cue.style, {
            left: (edge === "right" ? clip.right - size : clip.left) + "px",
            top: (edge === "bottom" ? clip.bottom - size : clip.top) + "px",
            width: (verticalEdge ? clip.right - clip.left : size) + "px",
            height: (verticalEdge ? size : clip.bottom - clip.top) + "px",
          });
          cue.style.setProperty("--scroll-cue-surface", background === "rgba(0, 0, 0, 0)" || background === "transparent" ? "var(--surface)" : background);
        }
      }
      for (const [edge, cue] of state.cues) {
        if (!wanted.has(edge)) { cue.remove(); state.cues.delete(edge); }
      }
    }
  }
  const mutations = new MutationObserver(records => {
    let changed = false;
    for (const record of records) {
      if (record.target === host || host.contains(record.target)) continue;
      changed = true;
      if (record.type === "attributes" && record.target instanceof Element) added.add(record.target);
      for (const node of record.addedNodes) if (node instanceof Element) added.add(node);
      if (record.type === "childList" && record.target instanceof HTMLElement && tracked.has(record.target)) {
        for (const node of record.addedNodes) if (node instanceof Element) resize.observe(node);
        for (const node of record.removedNodes) if (node instanceof Element) resize.unobserve(node);
      }
    }
    if (changed) schedule();
  });
  mutations.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class", "style", "hidden", "open", "inert"] });
  mutations.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
  const relayout = () => { discoverAll = true; schedule(); };
  document.addEventListener("scroll", schedule, { capture: true, passive: true });
  document.addEventListener("input", schedule, true);
  document.addEventListener("load", relayout, true);
  document.addEventListener("toggle", schedule, true);
  document.addEventListener("focusin", schedule, true);
  document.addEventListener("transitionend", schedule, true);
  document.addEventListener("animationend", schedule, true);
  window.addEventListener("resize", relayout);
  document.fonts?.addEventListener("loadingdone", relayout);
  schedule();
  return () => {
    disposed = true;
    cancelAnimationFrame(frame);
    mutations.disconnect();
    resize.disconnect();
    document.removeEventListener("scroll", schedule, true);
    document.removeEventListener("input", schedule, true);
    document.removeEventListener("load", relayout, true);
    document.removeEventListener("toggle", schedule, true);
    document.removeEventListener("focusin", schedule, true);
    document.removeEventListener("transitionend", schedule, true);
    document.removeEventListener("animationend", schedule, true);
    window.removeEventListener("resize", relayout);
    document.fonts?.removeEventListener("loadingdone", relayout);
    for (const [element, state] of tracked) remove(element, state);
    host.remove();
  };
}
