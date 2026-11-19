/** Morph the existing compact controls on a stable surface before changing the native shape. */
export function animateCompactMorph(host: HTMLElement | null, collapse: boolean) {
  const animations: Animation[] = [];
  let cancelled = false;
  const cancel = () => { cancelled = true; animations.forEach(animation => animation.cancel()); };
  const outline = host?.querySelector<HTMLElement>(".session-compact-outline");
  const ruby = host?.querySelector<HTMLElement>(".session-compact-ruby");
  if (!host || !outline || !ruby || typeof outline.animate !== "function" || document.hidden || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    return { finished: Promise.resolve(true), cancel };
  }

  const frame = host.getBoundingClientRect();
  const mark = ruby.getBoundingClientRect();
  const left = (frame.width - 48) / 2, top = (frame.height - 48) / 2;
  const dx = frame.left + frame.width / 2 - (mark.left + mark.width / 2);
  const dy = frame.top + frame.height / 2 - (mark.top + mark.height / 2);
  const options: KeyframeAnimationOptions = { duration: 260, easing: "cubic-bezier(.22, 1, .36, 1)", fill: "both" };
  const play = (element: HTMLElement, expanded: Keyframe, collapsed: Keyframe) => {
    const animation = element.animate(collapse ? [expanded, collapsed] : [collapsed, expanded], options);
    animations.push(animation);
  };
  play(outline,
    { left: "0px", top: "0px", width: `${frame.width}px`, height: `${frame.height}px`, borderRadius: "28px" },
    { left: `${left}px`, top: `${top}px`, width: "48px", height: "48px", borderRadius: "24px" });
  play(ruby, { transform: "translate(0px, 0px)" }, { transform: `translate(${dx}px, ${dy}px)` });
  for (const child of [...host.children]) {
    if (child instanceof HTMLElement && child !== outline && child !== ruby) {
      const opacity = Number.parseFloat(getComputedStyle(child).opacity);
      play(child, { opacity: Number.isFinite(opacity) ? opacity : 1 }, { opacity: 0 });
    }
  }
  // Finishing on a visibility change avoids a suspended animation delaying native restore.
  const finishWhenHidden = () => { if (document.hidden) animations.forEach(animation => animation.finish()); };
  document.addEventListener("visibilitychange", finishWhenHidden);
  const finished = Promise.all(animations.map(animation => animation.finished))
    .then(() => !cancelled, () => false)
    .finally(() => document.removeEventListener("visibilitychange", finishWhenHidden));
  return { finished, cancel };
}
