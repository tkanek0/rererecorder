import { useEffect, useRef, useState } from 'react';

import {
  audioLevelsUrl,
  previewUrl,
  reconnectDevice,
  setCodecs,
  setStreams,
  type AudioLevels,
  type CodecChoice,
  type CaptureName,
  type DeviceName,
  type Devices,
  type PreviewKind,
  type Settings,
  type StreamName,
} from '../lib/api';

type Props = {
  devices: Devices;
  settings: Settings | null;
  recording: boolean;
  onChanged: () => void;
};

/** What each camera preview is called on screen. */
const PREVIEW_LABELS: Record<PreviewKind, string> = {
  color: 'colour',
  depth: 'depth',
  ir1: 'infrared left',
  ir2: 'infrared right',
};

const STREAMS: { name: StreamName; label: string }[] = [
  { name: 'color', label: 'color' },
  { name: 'depth', label: 'depth' },
  { name: 'infrared', label: 'infrared' },
];

const CODEC_CHOICES: CodecChoice[] = ['compressed', 'raw'];

/** Read a codec name back as the two-way choice the page offers. */
const codecChoiceOf = (codec: string | undefined): CodecChoice =>
  codec === 'raw' ? 'raw' : 'compressed';

/**
 * Each microphone's angle on the circle, in the chip's DOA convention.
 * Unverified, like `backend/respeaker_adapter/config.py` MIC_ANGLES: the rotation is a guess.
 */
const MIC_LAYOUT: { key: keyof Omit<AudioLevels, 'mix'>; angleDeg: number }[] = [
  { key: 'mic1', angleDeg: 45 },
  { key: 'mic2', angleDeg: 135 },
  { key: 'mic3', angleDeg: 225 },
  { key: 'mic4', angleDeg: 315 },
];

/** dBFS range a meter maps onto 0..1. Below the floor reads as silence. */
const LEVEL_FLOOR_DB = -60;
const LEVEL_CEILING_DB = -6;

const levelFraction = (db: number | null): number => {
  if (db === null) return 0;
  return Math.min(
    1,
    Math.max(0, (db - LEVEL_FLOOR_DB) / (LEVEL_CEILING_DB - LEVEL_FLOOR_DB)),
  );
};

/** Quiet reads dim, loud reads like a VU meter running into the red. */
const levelColor = (fraction: number): string => {
  if (fraction > 0.85) return 'var(--bad)';
  if (fraction > 0.55) return 'var(--warn)';
  if (fraction > 0.04) return 'var(--accent)';
  return 'var(--line)';
};

const RADIUS_OUTER = 82;
const RADIUS_MICS = 62;
const DOT_MIN = 6;
const DOT_MAX = 20;

const micPosition = (angleDeg: number, radius: number) => {
  const rad = (angleDeg * Math.PI) / 180;
  return { x: 100 + radius * Math.cos(rad), y: 100 - radius * Math.sin(rad) };
};

/**
 * Subscribe to the live per-channel level stream while mounted. `EventSource`
 * reconnects on its own; the last reading is kept rather than cleared meanwhile.
 */
const useAudioLevels = (): AudioLevels => {
  const [levels, setLevels] = useState<AudioLevels>({
    mix: null,
    mic1: null,
    mic2: null,
    mic3: null,
    mic4: null,
  });

  useEffect(() => {
    const source = new EventSource(audioLevelsUrl());
    source.onmessage = (event) => {
      try {
        setLevels(JSON.parse(event.data) as AudioLevels);
      } catch {
        // Ignore a malformed line rather than closing the stream.
      }
    };
    return () => source.close();
  }, []);

  return levels;
};

const dbLabel = (db: number | null): string => (db === null ? '—' : `${db.toFixed(0)} dB`);

/**
 * Which streams the camera captures, and how each is stored. Disabled while
 * recording. See docs/decisions.md 23.
 */
const CaptureControls = ({
  settings,
  recording,
  onChanged,
}: {
  settings: Settings | null;
  recording: boolean;
  onChanged: () => void;
}) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!settings) return null;
  const streams = settings.streams;
  const locked = !settings.writable || recording || busy;

  const toggleStream = async (name: CaptureName) => {
    setBusy(true);
    setError(null);
    try {
      const wanted: Partial<Record<CaptureName, boolean>> = {
        [name]: !streams[name],
      };
      // Infrared needs depth, so turning depth off turns infrared off too.
      if (name === 'depth' && streams.depth && streams.infrared) {
        wanted.infrared = false;
      }
      await setStreams(wanted);
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const chooseCodec = async (name: StreamName, choice: CodecChoice) => {
    setBusy(true);
    setError(null);
    try {
      await setCodecs({ [name]: choice });
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="capture">
      <div className="rows">
        {STREAMS.map(({ name, label }) => {
          const on =
            name === 'color'
              ? Boolean(streams.color)
              : name === 'depth'
                ? Boolean(streams.depth)
                : streams.infrared;
          return (
            <div className="row" key={name}>
              <span className="label">
                <label>
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={locked || (name === 'infrared' && !streams.depth)}
                    onChange={() => void toggleStream(name)}
                  />{' '}
                  {label}
                </label>
              </span>
              <span className="value">
                {CODEC_CHOICES.map((choice) => (
                  <label key={choice} style={{ marginLeft: 10 }}>
                    <input
                      type="radio"
                      name={`${name}-codec`}
                      checked={codecChoiceOf(settings.codecs[name]) === choice}
                      disabled={locked || !on}
                      onChange={() => void chooseCodec(name, choice)}
                    />{' '}
                    {choice}
                  </label>
                ))}
              </span>
            </div>
          );
        })}
        <div className="row">
          <span className="label">
            <label>
              <input
                type="checkbox"
                checked={streams.motion}
                disabled={locked}
                onChange={() => void toggleStream('motion')}
              />{' '}
              inertial (accel + gyro)
            </label>
          </span>
        </div>
      </div>

      {recording ? (
        <p className="note">
          Stop recording to change what is captured or how it is stored.
        </p>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
    </div>
  );
};

/**
 * An `<img>` showing an MJPEG stream, which closes the stream on unmount.
 * Chrome keeps a removed `<img>`'s multipart connection open, and leaked ones
 * exhaust its six-per-host limit; clearing `src` in the effect cleanup closes it.
 */
const LiveImage = ({ src, alt }: { src: string; alt: string }) => {
  const image = useRef<HTMLImageElement>(null);

  useEffect(() => {
    const element = image.current;
    if (!element) return;
    element.src = src;
    return () => {
      element.src = '';
    };
  }, [src]);

  return <img ref={image} alt={alt} />;
};

/**
 * Ask the server to try a device again. See docs/decisions.md 29.
 * The status poll picks up the result, so this only reports a refusal.
 */
const ReconnectButton = ({
  name,
  recording,
}: {
  name: DeviceName;
  recording: boolean;
}) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reconnect = async () => {
    setBusy(true);
    setError(null);
    try {
      await reconnectDevice(name);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="reconnect">
      <button
        onClick={reconnect}
        disabled={busy || recording}
        title={recording ? 'stop the recording first' : 'try the device again'}
      >
        {busy ? 'Reconnecting…' : 'Reconnect'}
      </button>
      {error ? <p className="error">{error}</p> : null}
    </div>
  );
};

/**
 * The camera and the array, each with its connection state, identity, capture
 * settings and a live view, whether or not anything has opened it.
 */
export const DevicesPanel = ({ devices, settings, recording, onChanged }: Props) => {
  const levels = useAudioLevels();
  const { realsense, respeaker } = devices;

  return (
    <section className="panel devices">
      <h2>devices</h2>
      <div className="device-grid">
        {/* -- RealSense -------------------------------------------------- */}
        <div className="device-card">
          <div className="info">
            <h3>
              <span className={`dot ${realsense.connected ? 'live' : ''}`} />
              RealSense
            </h3>
            <div className="meta">
              {realsense.device ? (
                <>
                  {realsense.device.name}
                  <br />
                  {realsense.device.serial}
                  <br />
                  FW {realsense.device.firmware} · USB {realsense.device.usb_type}
                </>
              ) : (
                'not detected'
              )}
            </div>
            {realsense.error ? <p className="error">{realsense.error}</p> : null}
            {realsense.failed || !realsense.connected ? (
              <ReconnectButton name="realsense" recording={recording} />
            ) : null}
          </div>

          <div className="visual previews">
            {(['color', 'depth'] as PreviewKind[]).map((kind) => (
              <figure key={kind}>
                <LiveImage src={previewUrl(kind)} alt={PREVIEW_LABELS[kind]} />
                <figcaption>
                  <span>{PREVIEW_LABELS[kind]}</span>
                  <span>
                    {kind === 'depth'
                      ? realsense.streams.depth
                        ? `${realsense.streams.depth[0]}x${realsense.streams.depth[1]} @${realsense.streams.depth[2]}`
                        : 'off'
                      : realsense.streams.color
                        ? `${realsense.streams.color[0]}x${realsense.streams.color[1]} @${realsense.streams.color[2]}`
                        : 'off'}
                  </span>
                </figcaption>
              </figure>
            ))}
          </div>

          <CaptureControls
            settings={settings}
            recording={recording}
            onChanged={onChanged}
          />
        </div>

        {/* -- ReSpeaker ---------------------------------------------------- */}
        <div className="device-card">
          <div className="info">
            <h3>
              <span className={`dot ${respeaker.connected ? 'live' : ''}`} />
              ReSpeaker
            </h3>
            <div className="meta">
              {respeaker.connected ? (
                <>
                  {respeaker.name}
                  <br />
                  {respeaker.host_api}
                  <br />
                  {respeaker.rate ? respeaker.rate / 1000 : '?'} kHz · {respeaker.channels}ch
                </>
              ) : (
                (respeaker.error ?? 'not detected')
              )}
            </div>
            {respeaker.connected && respeaker.failed && respeaker.error ? (
              <p className="error">{respeaker.error}</p>
            ) : null}
            {respeaker.failed || !respeaker.connected ? (
              <ReconnectButton name="respeaker" recording={recording} />
            ) : null}
            {respeaker.recording ? (
              <p className="note">
                Being recorded.{respeaker.overruns ? ` ${respeaker.overruns} overruns.` : ''}
              </p>
            ) : null}
          </div>

          <div className="visual">
            <svg className="mic-radial" viewBox="0 0 200 200">
              <circle cx={100} cy={100} r={RADIUS_OUTER} fill="none" stroke="var(--line)" />
              {MIC_LAYOUT.map(({ key, angleDeg }) => {
                const pos = micPosition(angleDeg, RADIUS_MICS);
                const fraction = levelFraction(levels[key]);
                const label = micPosition(angleDeg, RADIUS_MICS + 22);
                return (
                  <g key={key}>
                    <circle cx={pos.x} cy={pos.y} r={DOT_MAX} fill="none" stroke="var(--line)" />
                    <circle
                      cx={pos.x}
                      cy={pos.y}
                      r={DOT_MIN + fraction * (DOT_MAX - DOT_MIN)}
                      fill={levelColor(fraction)}
                    />
                    <text x={label.x} y={label.y - 4} textAnchor="middle">
                      {key}
                    </text>
                    <text x={label.x} y={label.y + 7} textAnchor="middle">
                      {dbLabel(levels[key])}
                    </text>
                  </g>
                );
              })}
              {/* The chip's beamformed channel, at the centre. */}
              <circle cx={100} cy={100} r={26} fill="none" stroke="var(--line)" />
              <circle
                cx={100}
                cy={100}
                r={8 + levelFraction(levels.mix) * 16}
                fill={levelColor(levelFraction(levels.mix))}
              />
              <text x={100} y={100 + 40} textAnchor="middle">
                mix {dbLabel(levels.mix)}
              </text>
            </svg>
          </div>
        </div>
      </div>
    </section>
  );
};
