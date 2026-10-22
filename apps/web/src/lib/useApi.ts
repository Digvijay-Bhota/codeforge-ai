import { useCallback, useEffect, useRef, useState } from "react";

export interface ApiState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  /** Load again; keeps showing the current data meanwhile. */
  reload: () => void;
  /**
   * Load again unless a request is still unanswered (for polling): a slow API
   * gets at most one request at a time from this page, however often it polls.
   */
  refresh: () => void;
}

/**
 * Run *load* whenever *key* changes, aborting the previous request.  *load*
 * may change identity on every render; only *key* decides when to refetch.
 */
export function useApi<T>(key: string, load: (signal: AbortSignal) => Promise<T>): ApiState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const lastKey = useRef(key);
  // Set synchronously, so a refresh between a request starting and the next render is still refused.
  const inFlight = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    if (lastKey.current !== key) {
      lastKey.current = key;
      setData(undefined);
    }
    setLoading(true);
    inFlight.current = true;
    loadRef.current(controller.signal).then(
      (value) => {
        if (controller.signal.aborted) return;
        inFlight.current = false;
        setData(value);
        setError(null);
        setLoading(false);
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        inFlight.current = false;
        setError(err);
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [key, generation]);

  const reload = useCallback(() => setGeneration((n) => n + 1), []);
  const refresh = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    setGeneration((n) => n + 1);
  }, []);
  return { data, error, loading, reload, refresh };
}
