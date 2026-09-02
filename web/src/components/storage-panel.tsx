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
 *
 * The remaining time is the number that matters. A session writes about 54 MB/s
 * with every stream enabled, so "200 GB free" reads as plenty and is an hour.
 * It is computed from the rate this recording is actually achieving rather than
 * from a constant, because how well the frames compress depends on the scene.
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

  // Follow the server when it changes underneath, but never while the field is
  // being edited - overwriting a half-typed path is infuriating.
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

      {recording ? (
        <p className="note">
          The directory cannot move while recording: half a session on each disk
          would be described by neither manifest.
        </p>
      ) : (
        <p className="note">
          Created if it does not exist. About 54 MB/s with every stream on.
        </p>
      )}
      {storage.error ? <p className="error">{storage.error}</p> : null}
      {error ? <p className="error">{error}</p> : null}
    </section>
  );
};
