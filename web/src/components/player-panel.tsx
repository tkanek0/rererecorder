import { useCallback, useEffect, useRef, useState } from 'react';

import {
  audioUrl,
  fetchFrameTimes,
  fetchSession,
  loadFrame,
  type FrameMeta,
  type PreviewKind,
  type SessionDetail,
} from '../lib/api';
import { bytes, duration } from '../lib/format';
import { nearestFrame, timeOfFrame } from '../lib/frame-index';

/** Playback speeds offered, as multiples of the recorded rate. */
const SPEEDS = [0.25, 0.5, 1, 2, 4] as const;

/** Frame rate assumed when the recording does not report one. */
const FALLBACK_FPS = 30;

/** What each channel of a ReSpeaker recording is. */
const CHANNELS = [
  { value: 0, label: 'processed' },
  { value: 1, label: 'mic 1' },
  { value: 2, label: 'mic 2' },
  { value: 3, label: 'mic 3' },
  { value: 4, label: 'mic 4' },
  { value: 5, label: 'playback' },
] as const;

type Props = {
  sessionId: string;
  onClose: () => void;
};

/** What each stream is called on screen. */
const KIND_LABELS: Record<PreviewKind, string> = {
  color: 'colour',
  depth: 'depth',
  ir1: 'infrared left',
  ir2: 'infrared right',
};

/**
 * Play one recorded session back, one frame at a time.
 *
 * Frames are fetched individually rather than as a stream. That costs a request
 * per frame - about 16 ms of server work, so 30 fps is comfortable - and buys
 * the two things a stream cannot give: seeking to an arbitrary frame, and
 * knowing which frame is on screen. The server reports each frame's own capture
 * time in a header, so the clock shown is the recording's, not one computed by
 * assuming frames are evenly spaced. They are not: a set the camera mispaired
 * leaves a gap.
 */
export const PlayerPanel = ({ sessionId, onClose }: Props) => {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [kind, setKind] = useState<PreviewKind>('color');
  const [speed, setSpeed] = useState<number>(1);
  const [playing, setPlaying] = useState(false);
  const [src, setSrc] = useState<string | null>(null);
  const [meta, setMeta] = useState<FrameMeta | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The position lives in a ref as well as in state: the playback loop reads it
  // to decide what to fetch next, and a state dependency would restart the loop
  // on every frame.
  const indexRef = useRef(0);
  const [index, setIndex] = useState(0);
  const objectUrl = useRef<string | null>(null);
  const panel = useRef<HTMLElement>(null);

  // When the session has audio, the audio element is the clock: it cannot be
  // paused and resumed without a gap the ear notices, whereas a late video
  // frame is simply a late video frame. Frames are then chased rather than
  // scheduled, which is why `loading` exists - it drops requests rather than
  // queueing them when decoding falls behind.
  const audioRef = useRef<HTMLAudioElement>(null);
  const loading = useRef(false);
  const [frameTimes, setFrameTimes] = useState<[number, number][]>([]);
  const [channel, setChannel] = useState(0);

  // The panel appears below the live preview, so opening it would otherwise
  // leave the transport controls off screen.
  useEffect(() => {
    panel.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [sessionId]);

  const first = detail?.archive.first_index ?? 0;
  const last = detail?.archive.last_index ?? 0;
  const start = detail?.archive.first_monotonic ?? null;
  const fps = detail?.video?.fps ?? FALLBACK_FPS;
  const audioStart = detail?.audio?.first_monotonic ?? null;
  const hasAudio = detail?.audio != null && audioStart !== null;

  useEffect(() => {
    let cancelled = false;
    fetchSession(sessionId)
      .then((loaded) => {
        if (cancelled) return;
        setDetail(loaded);
        const at = loaded.archive.first_index ?? 0;
        indexRef.current = at;
        setIndex(at);
      })
      .catch((cause) =>
        setError(cause instanceof Error ? cause.message : String(cause)),
      );
    fetchFrameTimes(sessionId)
      .then((times) => {
        if (!cancelled) setFrameTimes(times);
      })
      .catch(() => setFrameTimes([]));
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  /** Show one frame, replacing whatever is on screen. */
  const show = useCallback(
    async (at: number): Promise<void> => {
      const loaded = await loadFrame(sessionId, at, kind, 960);
      // Revoked as soon as it is replaced: a blob URL holds its bytes until it
      // is, and 40 KB a frame at 30 fps is 1.2 MB a second leaked otherwise.
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
      objectUrl.current = loaded.url;
      setSrc(loaded.url);
      setMeta(loaded.meta);
      setIndex(loaded.meta.index);
    },
    [sessionId, kind],
  );

  const seek = useCallback(
    (at: number) => {
      const clamped = Math.min(Math.max(at, first), last);
      indexRef.current = clamped;
      // Move the audio too, or the next tick would drag the picture back.
      const audio = audioRef.current;
      if (audio && audioStart !== null) {
        const when = timeOfFrame(frameTimes, clamped);
        if (when !== null) audio.currentTime = Math.max(0, when - audioStart);
      }
      if (!playing) {
        show(clamped).catch((cause) =>
          setError(cause instanceof Error ? cause.message : String(cause)),
        );
      }
    },
    [first, last, playing, show, audioStart, frameTimes],
  );

  // The first frame, and any change of stream, while paused.
  useEffect(() => {
    if (!detail || playing) return;
    show(indexRef.current).catch((cause) =>
      setError(cause instanceof Error ? cause.message : String(cause)),
    );
  }, [detail, kind, playing, show]);

  // Audio-led playback: the audio element runs, and each animation frame asks
  // which video frame belongs to its current position. Frames that cannot be
  // fetched in time are skipped rather than queued - the sound is the thing
  // that must not stutter, and a dropped frame during playback costs nothing.
  useEffect(() => {
    if (!playing || !hasAudio || audioStart === null || frameTimes.length === 0) {
      return;
    }
    const audio = audioRef.current;
    if (!audio) return;
    audio.playbackRate = speed;
    void audio.play().catch((cause) => {
      setError(cause instanceof Error ? cause.message : String(cause));
      setPlaying(false);
    });

    let raf = 0;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      if (audio.ended) {
        setPlaying(false);
        return;
      }
      const wanted = nearestFrame(frameTimes, audioStart + audio.currentTime);
      if (wanted === null || wanted === indexRef.current || loading.current) {
        return;
      }
      indexRef.current = wanted;
      loading.current = true;
      show(wanted)
        .catch((cause) =>
          setError(cause instanceof Error ? cause.message : String(cause)),
        )
        .finally(() => {
          loading.current = false;
        });
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      audio.pause();
    };
  }, [playing, hasAudio, audioStart, frameTimes, speed, show]);

  // Frame-led playback, for a session with no audio. Chained rather than on an
  // interval: the next frame is requested only once the previous has arrived,
  // so a slow server makes playback slower rather than making it queue up
  // requests it cannot serve.
  useEffect(() => {
    if (!playing || !detail || hasAudio) return;
    let cancelled = false;

    const run = async () => {
      while (!cancelled) {
        const began = performance.now();
        try {
          await show(indexRef.current);
        } catch (cause) {
          if (!cancelled) {
            setError(cause instanceof Error ? cause.message : String(cause));
            setPlaying(false);
          }
          return;
        }
        if (cancelled) return;
        if (indexRef.current >= last) {
          setPlaying(false);
          return;
        }
        indexRef.current += 1;
        const target = 1000 / (fps * speed);
        const remaining = target - (performance.now() - began);
        if (remaining > 0) {
          await new Promise((resolve) => setTimeout(resolve, remaining));
        }
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [playing, detail, hasAudio, last, fps, speed, show]);

  // Nothing left holding bytes once the panel goes away.
  useEffect(
    () => () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    },
    [],
  );

  // Pause when the tab goes away, rather than pretending to still be playing.
  //
  // Chrome throttles timers in a hidden tab: measured, a setTimeout(10) in a
  // background tab took 557 ms, so the loop would crawl at under 2 fps while
  // the button still said "Pause". Stopping is honest, and it stops fetching
  // frames nobody is looking at. Resuming is left to the person coming back -
  // playback restarting on its own would be a surprise.
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) setPlaying(false);
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  const elapsed =
    meta?.monotonic !== null && meta?.monotonic !== undefined && start !== null
      ? meta.monotonic - start
      : null;
  const available = detail?.archive.streams;
  const kinds: PreviewKind[] = [
    ...(available?.color ? (['color'] as const) : []),
    ...(available?.depth ? (['depth'] as const) : []),
    ...(available?.infrared ? (['ir1', 'ir2'] as const) : []),
  ];

  return (
    <section
      className="panel player"
      style={{ gridColumn: '1 / -1' }}
      ref={panel}
    >
      <h2>
        playing {sessionId}
        <button className="close" onClick={onClose} title="close">
          ×
        </button>
      </h2>

      {error ? <p className="error">{error}</p> : null}
      {detail?.archive.error ? (
        <p className="error">{detail.archive.error}</p>
      ) : null}

      {/* Present but not shown: the transport below drives it, and a second
          set of controls would only disagree with the first. */}
      {hasAudio ? (
        <audio
          ref={audioRef}
          src={audioUrl(sessionId, channel)}
          preload="auto"
          onEnded={() => setPlaying(false)}
        />
      ) : null}

      <div className="player-body">
        <div className="player-view">
          {src ? (
            <img src={src} alt={KIND_LABELS[kind]} />
          ) : (
            <div className="placeholder">loading…</div>
          )}

          <div className="transport">
            <button
              onClick={() => {
                // The effect starts and stops the audio; this only flips the
                // intent, so that one place owns the element.
                setPlaying((was) => !was);
              }}
              disabled={!detail}
            >
              {playing ? 'Pause' : 'Play'}
            </button>
            <button onClick={() => seek(index - 1)} disabled={playing} title="previous frame">
              ‹
            </button>
            <button onClick={() => seek(index + 1)} disabled={playing} title="next frame">
              ›
            </button>
            <input
              type="range"
              min={first}
              max={last}
              value={index}
              onChange={(event) => seek(Number(event.target.value))}
            />
            <span className="counter">
              {/* Index, not position: it is the archive's own number, and it
                  is what the frame endpoint takes. */}
              {index} / {last}
              {elapsed === null ? '' : ` · ${elapsed.toFixed(2)}s`}
            </span>
          </div>

          <div className="transport">
            {kinds.map((option) => (
              <button
                key={option}
                onClick={() => setKind(option)}
                disabled={option === kind}
              >
                {KIND_LABELS[option]}
              </button>
            ))}
            <span className="counter">speed</span>
            {SPEEDS.map((option) => (
              <button
                key={option}
                onClick={() => setSpeed(option)}
                disabled={option === speed}
              >
                {option}×
              </button>
            ))}
          </div>

          {hasAudio ? (
            <div className="transport">
              <span className="counter">audio</span>
              {CHANNELS.slice(0, detail?.audio?.channels ?? 0).map((option) => (
                <button
                  key={option.value}
                  onClick={() => setChannel(option.value)}
                  disabled={option.value === channel}
                  title={
                    option.value === 0
                      ? 'beamformed and echo-cancelled by the array'
                      : option.value === 5
                        ? 'loopback of what was played out'
                        : 'a raw microphone'
                  }
                >
                  {option.label}
                </button>
              ))}
            </div>
          ) : (
            <p className="note">This session has no audio.</p>
          )}
        </div>

        <div className="player-facts rows">
          <Fact label="length" value={duration(detail?.duration_s)} />
          <Fact label="frames" value={String(detail?.archive.frames ?? '-')} />
          <Fact
            label="fps"
            value={detail?.video?.fps ? detail.video.fps.toFixed(2) : '-'}
          />
          <Fact label="size" value={bytes(detail?.size_bytes)} />
          <Fact
            label="dropped"
            value={String(detail?.video?.dropped ?? '-')}
            tone={detail?.video?.dropped ? 'bad' : 'good'}
          />
          <Fact
            label="skipped"
            value={String(detail?.video?.skipped ?? '-')}
            tone={detail?.video?.skipped ? 'warn' : 'good'}
          />
          <Fact
            label="timestamps"
            value={detail?.video?.timestamp_domain ?? '-'}
            tone={
              detail?.video?.timestamp_domain === 'global_time' ? 'good' : 'bad'
            }
          />
          <Fact
            label="aligned"
            value={detail?.archive.aligned === undefined ? '-' : String(detail.archive.aligned)}
          />
          <Fact label="colour" value={detail?.archive.color_format ?? '-'} />
          <Fact
            label="inertial"
            value={
              detail?.archive.motion_rate &&
              Object.keys(detail.archive.motion_rate).length > 0
                ? Object.entries(detail.archive.motion_rate)
                    .map(([stream, hz]) => `${stream} ${hz.toFixed(0)} Hz`)
                    .join(', ')
                : 'none'
            }
          />
          <Fact
            label="depth codec"
            value={detail?.archive.codecs?.depth ?? '-'}
          />
          <Fact
            label="audio"
            value={
              detail?.audio
                ? `${duration(detail.audio.seconds)}, ${detail.audio.channels} ch`
                : 'none'
            }
          />
          {detail?.audio?.filled ? (
            <Fact
              label="audio filled"
              value={`${detail.audio.filled} samples`}
              tone="warn"
            />
          ) : null}
          <Fact
            label="offset"
            value={
              detail?.calibration.offset_s === null ||
              detail?.calibration.offset_s === undefined
                ? 'unmeasured'
                : `${(detail.calibration.offset_s * 1000).toFixed(1)} ms`
            }
          />
        </div>
      </div>
    </section>
  );
};

/** One label-and-value line in the facts column. */
const Fact = ({
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
