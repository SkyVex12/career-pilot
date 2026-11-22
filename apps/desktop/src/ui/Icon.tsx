import type { ReactNode } from "react";

const paths = {
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M7 3v4M17 3v4M3 11h18M7 15h3m4 0h3M7 18h3" /></>,
  square: <rect x="4" y="4" width="16" height="16" rx="1" />,
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87" /><circle cx="9" cy="7" r="4" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></>,
  monitor: <><rect x="2" y="3" width="20" height="14" rx="2" /><path d="M8 21h8M12 17v4" /></>,
  mouse: <><rect x="5" y="2" width="14" height="20" rx="7" /><path d="M12 2v6" /></>,
  menu: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
  compass: <><circle cx="12" cy="12" r="9" /><path d="m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z" /></>,
  call: <><rect x="3" y="4" width="18" height="13" rx="3" /><path d="m8 21 4-4 4 4M8 10h2l2-3 2 6 2-3" /></>,
  briefcase: <><rect x="3" y="7" width="18" height="14" rx="3" /><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a22 22 0 0 0 18 0M12 11v4" /></>,
  document: <><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9ZM14 3v6h6M8 13h8M8 17h5" /></>,
  work: <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 7h2m4 0h2M8 11h2m4 0h2M9 21v-5h6v5" /></>,
  history: <><path d="M3 11a9 9 0 1 1 2.6 7M3 5v6h6M12 7v5l3 2" /></>,
  settings: <><path d="m9 3-.6 2-2 1-2-.5-2 3.5 1.5 1.5v3L2.4 15l2 3.5 2-.5 2 1L9 21h6l.6-2 2-1 2 .5 2-3.5-1.5-1.5v-3L21.6 9l-2-3.5-2 .5-2-1L15 3Z" /><circle cx="12" cy="12" r="3" /></>,
  mic: <><rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M9 22h6" /></>,
  headphones: <><path d="M4 14v-3a8 8 0 0 1 16 0v3" /><rect x="3" y="12" width="4" height="8" rx="2" /><rect x="17" y="12" width="4" height="8" rx="2" /></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  check: <path d="m5 12 4 4L19 6" />,
  layers: <><path d="m12 3 10 6-10 6L2 9ZM3 13l9 5 9-5M3 17l9 5 9-5" /></>,
  shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z" /><path d="m8 12 3 3 5-6" /></>,
  split: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M10 4v16" /></>,
  expand: <path d="M8 3H3v5m13 13h5v-5M3 3l7 7m11 11-7-7" />,
  compact: <path d="M3 3l7 7M5 10h5V5m11 16-7-7m5 0h-5v5" />,
  chevronUp: <path d="m7 14 5-5 5 5" />,
  chevronDown: <path d="m7 10 5 5 5-5" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  power: <><path d="M12 3v9" /><path d="M6 5.3a9 9 0 1 0 12 0" /></>,
  minus: <path d="M5 12h14" />,
  thumbUp: <path d="M7 10v11H3V10Zm0 0 5-7c3 0 2 5 2 7h5a2 2 0 0 1 2 2l-2 7a2 2 0 0 1-2 2H7" />,
  thumbDown: <path d="M7 14V3H3v11Zm0 0 5 7c3 0 2-5 2-7h5a2 2 0 0 0 2-2l-2-7a2 2 0 0 0-2-2H7" />,
  code: <path d="m7 7-5 5 5 5m10-10 5 5-5 5m-4-14-4 18" />,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof paths;

/** A single stroke and grid for product navigation and controls. Labels belong to the control. */
export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return <svg className="ui-symbol" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}
