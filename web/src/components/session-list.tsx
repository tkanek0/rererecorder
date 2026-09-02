import type { SessionSummary } from '../lib/api';

import { clockTime, duration } from '../lib/format';

type Props = {
  sessions: SessionSummary[];
};

/**
 * The sessions on disk, newest first.
 *
 * Shows the loss counts rather than only the length, because "20 s recorded" is
 * not the same claim as "20 s recorded with nothing missing", and the
 * difference is the whole point of the counters.
 */
export const SessionList = ({ sessions }: Props) => (
  <section className="panel" style={{ gridColumn: '1 / -1' }}>
    <h2>sessions</h2>
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
          </tr>
        </thead>
        <tbody>
          {sessions.map((session) => {
            const video = session.video;
            const lost = video ? video.dropped + video.skipped : 0;
            return (
              <tr key={session.session_id}>
                <td>{session.session_id}</td>
                <td>{clockTime(session.started_at?.realtime ?? null)}</td>
                <td>{duration(session.duration_s)}</td>
                <td>{video?.frames ?? '-'}</td>
                <td>{video?.fps?.toFixed(2) ?? '-'}</td>
                <td className={lost > 0 ? 'value bad' : 'value good'}>{lost}</td>
                <td>
                  {session.audio ? duration(session.audio.seconds) : '-'}
                </td>
                <td>
                  {/* Null until tools.calibrate has measured it. Saying
                      "unmeasured" is the honest answer; showing 0 would claim
                      the two devices are aligned. */}
                  {session.calibration.offset_s === null
                    ? 'unmeasured'
                    : `${(session.calibration.offset_s * 1000).toFixed(1)} ms`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    )}
  </section>
);
