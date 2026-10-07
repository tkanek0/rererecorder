import { useCallback, useEffect, useRef, useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';

import {
  PREVIEW_LABELS,
  audioLevelsUrl,
  previewUrl,
  reconnectDevice,
  setCodecs,
  setRecordAudio,
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
import { useAction } from '../lib/use-action';
import { PreviewModal } from './preview-modal';

type Props = {
  devices: Devices;
  settings: Settings | null;
  recording: boolean;
  onChanged: () => void;
};

const STREAMS: StreamName[] = ['color', 'depth', 'infrared'];

/** The streams the page previews, each of which this viewer can turn off. */
type Previewed = 'color' | 'depth';
const PREVIEWED: Previewed[] = ['color', 'depth'];
type PreviewChoice = Record<Previewed, boolean>;

const PREVIEW_STORAGE_KEY = 'rrr.previews';

/** This viewer's preview choice, remembered in the browser; both on if unknown. */
const loadPreviewChoice = (): PreviewChoice => {
  const fallback = { color: true, depth: true };
  try {
    const saved: unknown = JSON.parse(
      localStorage.getItem(PREVIEW_STORAGE_KEY) ?? 'null',
    );
    if (typeof saved !== 'object' || saved === null) return fallback;
    const choice = saved as Partial<Record<string, unknown>>;
    return {
      color: choice.color !== false,
      depth: choice.depth !== false,
    };
  } catch {
    return fallback;
  }
};

/**
 * Remember which previews this viewer wants, across reloads. A preview is the
 * viewer's own business, so it never reaches the server.
 */
const usePreviewChoice = (): [PreviewChoice, (kind: Previewed) => void] => {
  const [choice, setChoice] = useState(loadPreviewChoice);
  const toggle = (kind: Previewed) =>
    setChoice((was) => {
      const next = { ...was, [kind]: !was[kind] };
      try {
        localStorage.setItem(PREVIEW_STORAGE_KEY, JSON.stringify(next));
      } catch {
        // Not remembered, but still applied.
      }
      return next;
    });
  return [choice, toggle];
};


const CODEC_CHOICES: CodecChoice[] = ['compressed', 'raw'];

/** Read a codec name back as the two-way choice the page offers. */
const codecChoiceOf = (codec: string | undefined): CodecChoice =>
  codec === 'raw' ? 'raw' : 'compressed';

/**
 * Each microphone's angle on the circle, in the chip's DOA convention.
 * Unverified: the spacing is safe, the rotation is a guess.
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
  const { busy, error, run } = useAction();

  if (!settings) return null;
  const streams = settings.streams;
  const locked = !settings.writable || recording || busy;

  const toggleStream = (name: CaptureName) =>
    run(async () => {
      const wanted: Partial<Record<CaptureName, boolean>> = {
        [name]: !streams[name],
      };
      // Infrared needs depth, so turning depth off turns infrared off too.
      if (name === 'depth' && streams.depth && streams.infrared) {
        wanted.infrared = false;
      }
      await setStreams(wanted);
      onChanged();
    });

  const chooseCodec = (name: StreamName, choice: CodecChoice) =>
    run(async () => {
      await setCodecs({ [name]: choice });
      onChanged();
    });

  return (
    <div className="capture">
      <div className="rows">
        {STREAMS.map((name) => {
          const on = Boolean(streams[name]);
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
                  {name}
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
 * Whether the array is recorded. Disabled while recording.
 */
const ArrayControls = ({
  settings,
  recording,
  onChanged,
}: {
  settings: Settings | null;
  recording: boolean;
  onChanged: () => void;
}) => {
  const { busy, error, run } = useAction();

  if (!settings) return null;

  const toggleAudio = (audio: boolean) =>
    run(async () => {
      await setRecordAudio(audio);
      onChanged();
    });

  return (
    <div className="capture">
      <div className="rows">
        <div className="row">
          <span className="label">
            <label>
              <input
                type="checkbox"
                checked={settings.audio}
                disabled={!settings.writable || recording || busy}
                onChange={() => void toggleAudio(!settings.audio)}
              />{' '}
              audio (6 ch WAV)
            </label>
          </span>
        </div>
      </div>

      {recording ? (
        <p className="note">Stop recording to change what is recorded.</p>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
    </div>
  );
};

/**
 * An `<img>` showing an MJPEG stream, which closes the stream on unmount:
 * Chrome keeps a removed `<img>`'s connection open, against a per-host limit.
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
  const { busy, error, run } = useAction();
  const reconnect = () => run(() => reconnectDevice(name).then(() => undefined));

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
  // The enlarged preview replaces the small one, to hold one connection each.
  const [enlarged, setEnlarged] = useState<PreviewKind | null>(null);
  const [previews, togglePreview] = usePreviewChoice();
  const closeEnlarged = useCallback(() => setEnlarged(null), []);

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
            {PREVIEWED.map((kind) => {
              const stream = realsense.streams[kind];
              const shown = previews[kind] && stream !== null;
              return (
                <figure key={kind}>
                  {!shown ? (
                    <div className="placeholder">
                      {stream === null ? 'not captured' : 'preview off'}
                    </div>
                  ) : enlarged === kind ? (
                    <div className="placeholder" />
                  ) : (
                    <div
                      className="enlargeable"
                      onClick={() => setEnlarged(kind)}
                      title="enlarge"
                    >
                      <LiveImage src={previewUrl(kind)} alt={PREVIEW_LABELS[kind]} />
                    </div>
                  )}
                  <figcaption>
                    <span className="caption-title">
                      {PREVIEW_LABELS[kind]}
                      {/* Only a stream the camera is asked for can be previewed. */}
                      <button
                        className="icon"
                        aria-pressed={shown}
                        disabled={stream === null}
                        onClick={() => togglePreview(kind)}
                        title={
                          stream === null
                            ? 'not captured, so nothing to preview'
                            : shown
                              ? 'hide the preview'
                              : 'show the preview'
                        }
                      >
                        {shown ? <Eye size={14} /> : <EyeOff size={14} />}
                      </button>
                    </span>
                    <span>
                      {stream ? `${stream[0]}x${stream[1]} @${stream[2]}` : 'off'}
                    </span>
                  </figcaption>
                </figure>
              );
            })}
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
                  <br />
                  block {respeaker.block_size} (
                  {respeaker.rate
                    ? `${((respeaker.block_size / respeaker.rate) * 1000).toFixed(0)} ms`
                    : '?'}
                  ) · direction {respeaker.doa_poll_hz} Hz
                  {respeaker.doa_recorded ? '' : ', not recorded'}
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

          <ArrayControls
            settings={settings}
            recording={recording}
            onChanged={onChanged}
          />
        </div>
      </div>

      {enlarged ? (
        <PreviewModal title={PREVIEW_LABELS[enlarged]} onClose={closeEnlarged}>
          <LiveImage
            src={previewUrl(enlarged)}
            alt={PREVIEW_LABELS[enlarged]}
          />
        </PreviewModal>
      ) : null}
    </section>
  );
};
