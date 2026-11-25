import { createContext, useContext, useState, type ReactNode } from "react";

export interface SessionNoteDraft { id: string; loaded: boolean; saved: string; draft: string; revision?: number }
const Drafts = createContext<Map<string, SessionNoteDraft> | null>(null);

/** In-memory drafts live only for the current account/profile workspace. */
export function SessionNoteDrafts({ children }: { children: ReactNode }) {
  const [drafts] = useState(() => new Map<string, SessionNoteDraft>());
  return <Drafts.Provider value={drafts}>{children}</Drafts.Provider>;
}

export const useSessionNoteDrafts = () => useContext(Drafts);
