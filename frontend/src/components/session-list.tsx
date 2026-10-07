import { useState } from 'react';

import { deleteSession, type SessionSummary } from '../lib/api';
import { clockTime, duration, offset } from '../lib/format';
import { useAction } from '../lib/use-action';

type Props = {
  sessions: SessionSummary[];
  selected: string | null;
  recordingId: string | null;
  onSelect: (sessionId: string) => void;
  onDeleted: () => void;
};

/**
 * The sessions on disk, newest first, with their losses, playback and deletion.
 * Deletion takes two clicks rather than a `confirm()`, which would block the page.
 */
export const SessionList = ({
  sessions,
  selected,
  recordingId,
  onSelect,
  onDeleted,
}: Props) => {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const { error, run } = useAction();

  const remove = async (sessionId: string) => {
    setDeleting(sessionId);
    await run(async () => {
      await deleteSession(sessionId);
      onDeleted();
    });
    setDeleting(null);
    setConfirming(null);
  };

  return (
    <section className="panel wide">
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
              const lost = video ? video.dropped + video.skipped_duplicate : 0;
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
                    {/* Null until scripts/calibrate.py measures it; never shown as 0. */}
                    {offset(session.calibration.offset_s)}
                  </td>
                  <td className="actions">
                    <button
                      onClick={() => onSelect(session.session_id)}
                      disabled={recordingId !== null}
                      title={
                        recordingId !== null
                          ? 'stop recording first'
                          : 'play this session'
                      }
                    >
                      Play
                    </button>
                    {confirming === session.session_id ? (
                      <>
                        <button
                          className="danger"
                          onClick={() => remove(session.session_id)}
                          disabled={deleting !== null}
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
                        disabled={live || deleting !== null}
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
