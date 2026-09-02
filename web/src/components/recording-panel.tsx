import { useState } from 'react';

import { setRecording, type RecordingState } from '../lib/api';
import { bytes, duration, rate } from '../lib/format';

type Props = {
  recording: RecordingState;
  writeRate: number | null;
  onChanged: () => void;
};

/** One line of the readout, coloured when the number means something is wrong. */
const Row = ({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'good' | 'warn' | 'bad';
}) => (
  <div className="row">
    <span className="label">{label}</span>
    <span className={`value ${tone ?? ''}`}>{value}</span>
  </div>
);

/**
 * Start and stop recording, and show what the running session has captured.
 *
 * The dropped and filled counts are shown even when they are zero. They are how
 * anyone learns that a recording has holes in it, and a panel that hid them
 * until they went wrong would let a bad session look fine.
 */
export const RecordingPanel = ({ recording, writeRate, onChanged }: Props) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      await setRecording(!recording.recording, name.trim() || undefined);
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const video = recording.video;
  const audio = recording.audio;

  return (
    <section className="panel">
      <h2>
        recording{' '}
        <span
          className={`dot ${recording.recording ? 'rec' : ''}`}
          title="writing"
        />
      </h2>

      <button
        className={`record ${recording.recording ? 'armed' : ''}`}
        onClick={toggle}
        disabled={busy}
      >
        {recording.recording ? 'Stop recording' : 'Start recording'}
      </button>

      {!recording.recording ? (
        <div className="field">
          <input
            type="text"
            value={name}
            placeholder="session name (default: the time)"
            onChange={(event) => setName(event.target.value)}
          />
        </div>
      ) : null}

      <div className="rows" style={{ marginTop: 12 }}>
        <Row label="session" value={recording.session_id ?? '-'} />
        <Row label="elapsed" value={duration(recording.seconds)} />
        <Row label="written" value={bytes(recording.size_bytes)} />
        <Row label="rate" value={rate(writeRate)} />
      </div>

      {video ? (
        <>
          <h2 style={{ marginTop: 14 }}>camera</h2>
          <div className="rows">
            <Row label="frames" value={String(video.frames)} />
            <Row
              label="fps"
              value={video.fps === null ? '-' : video.fps.toFixed(2)}
            />
            <Row
              label="dropped"
              value={String(video.dropped)}
              tone={video.dropped > 0 ? 'bad' : 'good'}
            />
            <Row
              label="skipped mid-stream"
              value={String(video.skipped)}
              tone={video.skipped > 0 ? 'warn' : 'good'}
            />
            {/* Normal, and every recording has a few - shown so that nobody
                wonders where three frames went. */}
            <Row label="skipped at startup" value={String(video.skipped_warmup)} />
            <Row
              label="timestamps"
              value={video.timestamp_domain}
              tone={video.timestamp_domain === 'global_time' ? 'good' : 'bad'}
            />
            {/* Roughly 800 Hz means the sensor is recorded at its own rate;
                roughly 60 would mean one sample of each per video frame. */}
            <Row
              label="inertial"
              value={
                video.motion
                  ? `${video.motion} samples`
                  : recording.recording
                    ? 'none'
                    : '-'
              }
              tone={video.motion ? 'good' : undefined}
            />
            {video.motion_overrun ? (
              <Row
                label="inertial lost"
                value={String(video.motion_overrun)}
                tone="bad"
              />
            ) : null}
          </div>
        </>
      ) : null}

      {audio ? (
        <>
          <h2 style={{ marginTop: 14 }}>array</h2>
          <div className="rows">
            <Row label="audio" value={duration(audio.seconds)} />
            <Row
              label="filled"
              value={`${audio.filled} samples`}
              tone={audio.filled > 0 ? 'warn' : 'good'}
            />
            <Row
              label="overruns"
              value={String(audio.overruns)}
              tone={audio.overruns > 0 ? 'warn' : 'good'}
            />
          </div>
        </>
      ) : null}

      {recording.errors.length > 0 ? (
        <p className="error">{recording.errors.join('\n')}</p>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
    </section>
  );
};
