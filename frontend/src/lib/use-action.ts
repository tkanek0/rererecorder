import { useCallback, useState } from 'react';

/**
 * Turn whatever was thrown into a message the page can show.
 *
 * @param cause What was caught.
 * @returns Its message.
 */
export const errorMessage = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

/**
 * Track a panel's requests: whether one is in flight, and what the last failed with.
 *
 * @returns `busy`, `error`, a `setError` for failures found elsewhere, and
 *   `run`, which clears the error, marks the panel busy for the action and
 *   records a failure instead of throwing it.
 */
export const useAction = () => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async (action: () => Promise<void>): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
};
