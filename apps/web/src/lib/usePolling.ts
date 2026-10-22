import { useEffect } from "react";

export const POLL_INTERVAL_MS = 5000;

/**
 * Call *reload* every *intervalMs* while *active* and the page is visible.
 * Used to follow tasks that are still running; stops by itself once nothing
 * is left to wait for.
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
