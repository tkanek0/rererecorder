import { useEffect, useState } from 'react';

import { setSessionsDir, type Settings, type StorageStatus } from '../lib/api';
import { bytes, duration } from '../lib/format';

type Props = {
  storage: StorageStatus;
  settings: Settings | null;
  recording: boolean;
  onChanged: () => void;
};

/**
 * Where recordings go, how much room is left, and how long that lasts.
 * See docs/features.md "The page".
 */
export const StoragePanel = ({
  storage,
  settings,
  recording,
  onChanged,
}: Props) => {
  const [draft, setDraft] = useState(storage.sessions_dir);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Follow the server's value, except while a change is being applied.
  useEffect(() => {
    if (!busy) setDraft(storage.sessions_dir);
  }, [storage.sessions_dir, busy]);

  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      await setSessionsDir(draft.trim());
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const changed = draft.trim() !== storage.sessions_dir;
  const used =
    storage.total_bytes && storage.free_bytes
      ? storage.total_bytes - storage.free_bytes
      : null;

  return (
    <section className="panel">
      <h2>storage</h2>
      <div className="rows">
        <div className="row">
          <span className="label">free</span>
          <span className="value">
            {bytes(storage.free_bytes)}
            {storage.total_bytes ? ` of ${bytes(storage.total_bytes)}` : ''}
          </span>
        </div>
        {used !== null ? (
          <div className="row">
            <span className="label">used</span>
            <span className="value">{bytes(used)}</span>
          </div>
        ) : null}
        <div className="row">
          <span className="label">room left</span>
          <span
            className={`value ${
              storage.seconds_left !== null && storage.seconds_left < 600
                ? 'bad'
                : ''
            }`}
          >
            {storage.seconds_left === null
              ? 'measured while recording'
              : duration(storage.seconds_left)}
          </span>
        </div>
      </div>

      <div className="field">
        <input
          type="text"
          value={draft}
          spellCheck={false}
          disabled={!settings?.writable || recording}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          onClick={apply}
          disabled={!changed || busy || recording || !settings?.writable}
        >
          Move
        </button>
      </div>

      {recording ? <p className="note">Stop recording to move it.</p> : null}
      {storage.error ? <p className="error">{storage.error}</p> : null}
      {error ? <p className="error">{error}</p> : null}
    </section>
  );
};
