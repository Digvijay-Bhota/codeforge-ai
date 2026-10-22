import { useEffect } from "react";

export const POLL_INTERVAL_MS = 5000;

/**
 * Call *reload* every *intervalMs* while *active* and the page is visible.
 * Used to follow tasks that are still running; stops by itself once nothing
 * is left to wait for.
 *
 * Callers pass useApi's `refresh`, which sends nothing while the previous
 * request is unanswered: a slow API (a GitHub outage behind it) is never sent
 * a growing pile of requests, and polling resumes once it answers.
 */
export function usePolling(active: boolean, reload: () => void, intervalMs: number = POLL_INTERVAL_MS): void {
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [active, reload, intervalMs]);
}
