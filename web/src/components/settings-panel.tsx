import { useState } from 'react';

import {
  setCodecs,
  setStreams,
  type CodecChoice,
  type Settings,
  type StreamName,
} from '../lib/api';

type Props = {
  settings: Settings | null;
  recording: boolean;
  onChanged: () => void;
};

const STREAMS: { name: StreamName; label: string }[] = [
  { name: 'color', label: 'color' },
  { name: 'depth', label: 'depth' },
  { name: 'infrared', label: 'infrared' },
];

const CHOICES: CodecChoice[] = ['compressed', 'raw'];

/** Read a codec name back as the two-way choice the page offers. */
const choiceOf = (codec: string | undefined): CodecChoice =>
  codec === 'raw' ? 'raw' : 'compressed';

/**
 * Which streams the camera captures, and how each is stored.
 *
 * Streams take effect the next time the camera opens - the SDK settles
 * resolution and frame rate at pipeline start, so toggling one restarts the
 * shared pipeline rather than reaching into a running one. Codecs need
 * nothing restarted: they only decide how the next recording's archive
 * encodes what it gets.
 *
 * Disabled while recording, for the same reason the sessions directory is: a
 * session cannot describe two configurations at once.
 */
export const SettingsPanel = ({ settings, recording, onChanged }: Props) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!settings) return null;
  const streams = settings.streams;
  const locked = !settings.writable || recording || busy;

  const toggleStream = async (name: StreamName) => {
    setBusy(true);
    setError(null);
    try {
      const wanted: Partial<Record<StreamName, boolean>> = {
        [name]: name === 'infrared' ? !streams.infrared : !streams[name],
      };
      // Infrared is the depth sensor's own pair - turning depth off takes it
      // with it, rather than leaving a combination the server would refuse.
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
    <section className="panel">
      <h2>capture</h2>
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
                {CHOICES.map((choice) => (
                  <label key={choice} style={{ marginLeft: 10 }}>
                    <input
                      type="radio"
                      name={`${name}-codec`}
                      checked={choiceOf(settings.codecs[name]) === choice}
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
      </div>

      {recording ? (
        <p className="note">
          Stop recording to change what is captured or how it is stored.
        </p>
      ) : (
        <p className="note">
          On this machine, color alone with raw is the only combination
          measured to hold 30 fps with nothing dropped - see
          docs/windows-native.md.
        </p>
      )}
      {error ? <p className="error">{error}</p> : null}
    </section>
  );
};
