import { useState } from 'react';

import { deleteSession, type SessionSummary } from '../lib/api';
import { clockTime, duration } from '../lib/format';

type Props = {
  sessions: SessionSummary[];
  selected: string | null;
  recordingId: string | null;
  onSelect: (sessionId: string) => void;
  onDeleted: () => void;
};

/**
 * The sessions on disk, newest first, with playback and deletion.
 *
 * Shows the loss counts rather than only the length: "34 s recorded" is not the
 * same claim as "34 s recorded with nothing missing", and the difference is what
 * the counters are for.
 *
 * Deletion is offered because a session costs 1.7 GB for 34 seconds - without
 * it the only way to reclaim space is a shell. It takes two clicks rather than a
 * `confirm()` dialog: a browser modal blocks everything until dismissed, which
 * makes the page untestable and is heavier than the decision warrants.
 */
export const SessionList = ({
  sessions,
  selected,
  recordingId,
  onSelect,
  onDeleted,
}: Props) => {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const remove = async (sessionId: string) => {
    setBusy(sessionId);
    setError(null);
    try {
      await deleteSession(sessionId);
      onDeleted();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  };

  return (
    <section className="panel" style={{ gridColumn: '1 / -1' }}>
      <h2>sessions</h2>
      {error ? <p className="error">{error}</p> : null}
      {sessions.length === 0 ? (
        <p className="note">Nothing recorded here yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>session</th>
              <th>started</th>
              <th>length</th>
              <th>frames</th>
              <th>fps</th>
              <th>lost</th>
              <th>audio</th>
              <th>offset</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => {
              const video = session.video;
              const lost = video ? video.dropped + video.skipped : 0;
              const live = session.session_id === recordingId;
              return (
                <tr
                  key={session.session_id}
                  className={session.session_id === selected ? 'selected' : ''}
                >
                  <td>
                    {session.session_id}
                    {live ? ' · recording' : ''}
                  </td>
                  <td>{clockTime(session.started_at?.realtime ?? null)}</td>
                  <td>{duration(session.duration_s)}</td>
                  <td>{video?.frames ?? '-'}</td>
                  <td>{video?.fps?.toFixed(2) ?? '-'}</td>
                  <td className={lost > 0 ? 'value bad' : 'value good'}>{lost}</td>
                  <td>{session.audio ? duration(session.audio.seconds) : '-'}</td>
                  <td>
                    {/* Null until tools.calibrate has measured it. Saying
                        "unmeasured" is honest; showing 0 would claim the two
                        devices are aligned. */}
                    {session.calibration.offset_s === null
                      ? 'unmeasured'
                      : `${(session.calibration.offset_s * 1000).toFixed(1)} ms`}
                  </td>
                  <td className="actions">
                    <button
                      onClick={() => onSelect(session.session_id)}
                      disabled={live}
                      title={live ? 'still recording' : 'play this session'}
                    >
                      Play
                    </button>
                    {confirming === session.session_id ? (
                      <>
                        <button
                          className="danger"
                          onClick={() => remove(session.session_id)}
                          disabled={busy !== null}
                        >
                          Delete for good
                        </button>
                        <button onClick={() => setConfirming(null)}>
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        onClick={() => setConfirming(session.session_id)}
                        disabled={live || busy !== null}
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
};
