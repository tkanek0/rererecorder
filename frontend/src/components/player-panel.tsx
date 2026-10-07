import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, X } from 'lucide-react';

import {
  PREVIEW_LABELS,
  audioUrl,
  fetchFrameTimes,
  fetchSession,
  loadFrame,
  type FrameMeta,
  type PreviewKind,
  type SessionDetail,
} from '../lib/api';
import { bytes, duration, offset } from '../lib/format';
import { nearestFrame, timeOfFrame } from '../lib/frame-index';
import { errorMessage } from '../lib/use-action';
import { Row } from './row';

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

/**
 * Play one recorded session back, one frame at a time.
 * See docs/decisions.md 10 and docs/features.md "The page".
 */
export const PlayerPanel = ({ sessionId, onClose }: Props) => {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [kind, setKind] = useState<PreviewKind>('color');
  const [speed, setSpeed] = useState<number>(1);
  const [playing, setPlaying] = useState(false);
  const [src, setSrc] = useState<string | null>(null);
  const [meta, setMeta] = useState<FrameMeta | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Also kept in a ref so the playback loops can read it without restarting.
  const indexRef = useRef(0);
  const [index, setIndex] = useState(0);
  const objectUrl = useRef<string | null>(null);

  // With audio, the audio element is the clock; `loading` drops frame requests
  // rather than queueing them when fetching falls behind.
  const audioRef = useRef<HTMLAudioElement>(null);
  const loading = useRef(false);
  const [frameTimes, setFrameTimes] = useState<[number, number][]>([]);
  const [channel, setChannel] = useState(0);

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
        setError(errorMessage(cause)),
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
      // Revoke the previous blob URL, or every frame's bytes leak.
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
          setError(errorMessage(cause)),
        );
      }
    },
    [first, last, playing, show, audioStart, frameTimes],
  );

  // The first frame, and any change of stream, while paused.
  useEffect(() => {
    if (!detail || playing) return;
    show(indexRef.current).catch((cause) =>
      setError(errorMessage(cause)),
    );
  }, [detail, kind, playing, show]);

  // Audio-led playback: each animation frame shows the video frame nearest the
  // audio position, skipping any that cannot be fetched in time.
  useEffect(() => {
    if (!playing || !hasAudio || audioStart === null || frameTimes.length === 0) {
      return;
    }
    const audio = audioRef.current;
    if (!audio) return;
    audio.playbackRate = speed;
    void audio.play().catch((cause) => {
      setError(errorMessage(cause));
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
          setError(errorMessage(cause)),
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

  // Frame-led playback, without audio. Chained rather than on an interval, so
  // a slow server slows playback instead of queueing requests.
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
            setError(errorMessage(cause));
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

  useEffect(
    () => () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    },
    [],
  );

  // Pause when the tab is hidden: Chrome throttles background timers. See
  // docs/features.md "The page". Resuming is left to the user.
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
    <section className="panel player wide">
      <h2>
        playing {sessionId}
        <button className="close" onClick={onClose} title="close">
          <X size={16} />
        </button>
      </h2>

      {error ? <p className="error">{error}</p> : null}
      {detail?.archive.error ? (
        <p className="error">{detail.archive.error}</p>
      ) : null}

      {/* No controls of its own: the transport below drives it. */}
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
            <img src={src} alt={PREVIEW_LABELS[kind]} />
          ) : (
            <div className="placeholder">loading…</div>
          )}

          <div className="transport">
            <button
              onClick={() => {
                // The playback effect owns the audio element; this only flips intent.
                setPlaying((was) => !was);
              }}
              disabled={!detail}
            >
              {playing ? 'Pause' : 'Play'}
            </button>
            <button onClick={() => seek(index - 1)} disabled={playing} title="previous frame">
              <ChevronLeft size={16} />
            </button>
            <button onClick={() => seek(index + 1)} disabled={playing} title="next frame">
              <ChevronRight size={16} />
            </button>
            <input
              type="range"
              min={first}
              max={last}
              value={index}
              onChange={(event) => seek(Number(event.target.value))}
            />
            <span className="counter">
              {/* The archive's own frame index, not a position. */}
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
                {PREVIEW_LABELS[option]}
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
          <Row label="length" value={duration(detail?.duration_s)} />
          <Row label="frames" value={String(detail?.archive.frames ?? '-')} />
          <Row
            label="fps"
            value={detail?.video?.fps ? detail.video.fps.toFixed(2) : '-'}
          />
          <Row label="size" value={bytes(detail?.size_bytes)} />
          <Row
            label="dropped"
            value={String(detail?.video?.dropped ?? '-')}
            tone={detail?.video?.dropped ? 'bad' : 'good'}
          />
          <Row
            label="skipped"
            value={String(detail?.video?.skipped_duplicate ?? '-')}
            tone={detail?.video?.skipped_duplicate ? 'warn' : 'good'}
          />
          <Row
            label="timestamps"
            value={detail?.video?.timestamp_domain ?? '-'}
            tone={
              detail?.video?.timestamp_domain === 'global_time' ? 'good' : 'bad'
            }
          />
          <Row
            label="aligned"
            value={detail?.archive.aligned === undefined ? '-' : String(detail.archive.aligned)}
          />
          <Row label="color" value={detail?.archive.color_format ?? '-'} />
          <Row
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
          <Row
            label="depth codec"
            value={detail?.archive.codecs?.depth ?? '-'}
          />
          <Row
            label="audio"
            value={
              detail?.audio
                ? `${duration(detail.audio.seconds)}, ${detail.audio.channels} ch`
                : 'none'
            }
          />
          {detail?.audio?.filled ? (
            <Row
              label="audio filled"
              value={`${detail.audio.filled} samples`}
              tone="warn"
            />
          ) : null}
          <Row
            label="offset"
            value={offset(detail?.calibration.offset_s)}
          />
        </div>
      </div>
    </section>
  );
};
