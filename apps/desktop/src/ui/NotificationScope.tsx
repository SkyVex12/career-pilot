import { createContext, useState, type ReactNode } from "react";

/** A shared bottom-right destination. Dialogs own a scope so actions stay in their focus trap. */
export const NotificationVisibility = createContext(true);
export const NotificationOutlet = createContext<HTMLElement | null>(null);
export function NotificationScope({ children, notices }: { children: ReactNode; notices?: ReactNode }) {
  const [outlet, setOutlet] = useState<HTMLElement | null>(null);
  return <NotificationOutlet.Provider value={outlet}>
    {children}
    <section ref={setOutlet} className="ui-toast-viewport" aria-label="Notifications">{notices}</section>
  </NotificationOutlet.Provider>;
}
