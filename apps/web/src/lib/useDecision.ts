import { useRef, useState } from "react";
import { ApiError, errorMessage } from "../api/client";

/**
 * Submit one decision at a time.  *onDone* gets the API's message.  A 409 means
 * the server's state moved on (decided, expired, or a newer request); with
 * *onStale* it goes there instead of being shown as an error, so the caller
 * can reload rather than let the user retry against stale data.
 */
export function useDecision(onDone: (message: string) => void, onStale?: (message: string) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  async function submit(action: () => Promise<{ message: string }>) {
    if (inFlight.current) return; // a double click submits once
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      onDone(result.message);
    } catch (err) {
      if (onStale && err instanceof ApiError && err.status === 409) onStale(err.message);
      else setError(errorMessage(err));
      inFlight.current = false;
      setBusy(false);
    }
  }

  return { busy, error, submit };
}
