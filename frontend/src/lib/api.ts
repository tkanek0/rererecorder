/**
 * Client for the Python control plane.
 *
 * Field names stay snake_case, matching the server and `session.json`.
 */

const DEFAULT_PORT = 8040;

/**
 * Base HTTP URL of the control plane: this page's host, on `VITE_CONTROL_PORT`.
 * See docs/features.md "The page".
 *
 * @returns The URL, with no trailing slash.
 */
const controlBase = (): string =>
  `http://${window.location.hostname}:${
    import.meta.env.VITE_CONTROL_PORT ?? DEFAULT_PORT
  }`;

/** Which preview a panel is showing. */
export type PreviewKind = 'color' | 'depth' | 'ir1' | 'ir2';

/** What each preview is called on screen. */
export const PREVIEW_LABELS: Record<PreviewKind, string> = {
  color: 'color',
  depth: 'depth',
  ir1: 'infrared left',
  ir2: 'infrared right',
};

/** What the camera is, as the SDK reports it. */
export type DeviceInfo = {
  name: string;
  serial: string;
  firmware: string;
  usb_type: string;
};

/** The stream configuration in force. Settled when the pipeline starts. */
export type StreamConfig = {
  color: [number, number, number] | null;
  depth: [number, number, number] | null;
  color_format: string;
  infrared: boolean;
  align_to_color: boolean;
  motion: boolean;
};

/** What the camera has contributed to the running session. */
export type VideoState = {
  frames: number;
  dropped: number;
  /** Inertial samples written. */
  motion: number;
  motion_overrun: number;
  skipped_duplicate: number;
  skipped_warmup: number;
  fps: number | null;
  timestamp_domain: string;
};

/** What the array has contributed, when it is being recorded at all. */
export type AudioState = {
  seconds: number;
  filled: number;
  overruns: number;
};

/** The recorder's own view of the session in progress. */
export type RecordingState = {
  recording: boolean;
  session_id: string | null;
  seconds: number;
  size_bytes: number;
  video: VideoState | null;
  audio: AudioState | null;
  /** How many marks have been written into this session so far. */
  marks: number;
  errors: string[];
};

/** Where recordings go, and how long the disk lasts at the current rate. */
export type StorageStatus = {
  sessions_dir: string;
  free_bytes?: number;
  total_bytes?: number;
  write_bytes_per_s: number | null;
  seconds_left: number | null;
  error?: string;
};

/**
 * What the SDK currently sees of the D455, independent of whether a preview
 * or a recording has opened it.
 */
export type RealsenseDeviceStatus = {
  connected: boolean;
  device: DeviceInfo | null;
  /** What is being asked for right now. */
  streams: StreamConfig;
  /** Whether opening it failed. See `reconnectDevice`. */
  failed: boolean;
  error: string | null;
};

/**
 * What PortAudio currently sees of the array, independent of whether a
 * recording has opened it.
 */
export type RespeakerDeviceStatus = {
  connected: boolean;
  name: string | null;
  host_api: string | null;
  channels: number | null;
  rate: number | null;
  /** Whether the audio stream or the direction readings failed. See
   * `reconnectDevice`. */
  failed: boolean;
  error: string | null;
  recording: boolean;
  overruns: number;
};

/** A device the page can ask the server to reconnect. */
export type DeviceName = 'realsense' | 'respeaker';

/** Whether each device is plugged in right now. */
export type Devices = {
  realsense: RealsenseDeviceStatus;
  respeaker: RespeakerDeviceStatus;
};

/** Everything the page polls for. */
export type Status = {
  recording: RecordingState;
  storage: StorageStatus;
  devices: Devices;
};

/** One channel's current level, in dBFS, or null for silence. */
export type AudioLevels = {
  mix: number | null;
  mic1: number | null;
  mic2: number | null;
  mic3: number | null;
  mic4: number | null;
};

/** One finished session, as its manifest describes it. */
export type SessionSummary = {
  session_id: string;
  duration_s: number | null;
  started_at: { monotonic: number; realtime: number } | null;
  video: {
    frames: number;
    dropped: number;
    skipped_duplicate: number;
    motion: number;
    fps: number | null;
    timestamp_domain: string;
  } | null;
  audio: { seconds: number; channels: number; filled: number } | null;
  calibration: { offset_s: number | null };
  errors: string[];
};

/** What an archive holds, without decoding any of it. */
export type ArchiveDetail = {
  frames?: number;
  first_index?: number | null;
  last_index?: number | null;
  first_monotonic?: number | null;
  streams?: { color: boolean; depth: boolean; infrared: boolean };
  aligned?: boolean;
  codecs?: Record<string, string> | null;
  color_format?: string | null;
  /** Measured sample rate per inertial stream, in Hz. */
  motion_rate?: Record<string, number>;
  error?: string;
};

/** The array's track in a finished session. */
export type AudioTrack = {
  rate: number;
  channels: number;
  samples: number;
  seconds: number;
  filled: number;
  overruns: number;
  /** Capture time of sample zero, on the common axis. */
  first_monotonic: number | null;
};

/** One session in full: its manifest, its size and its frame range. */
export type SessionDetail = Omit<SessionSummary, 'audio'> & {
  size_bytes: number;
  archive: ArchiveDetail;
  audio: AudioTrack | null;
};

/** What the page is allowed to change, and what it is set to. */
export type Settings = {
  sessions_dir: string;
  writable: boolean;
  streams: StreamConfig;
  codecs: Record<string, string>;
};

/** A stream whose archive codec can be chosen from the page. */
export type StreamName = 'color' | 'depth' | 'infrared';

/** Anything the camera can be asked to capture at all, codec or not. */
export type CaptureName = StreamName | 'motion';

/** How a stream's archive is encoded, without naming the algorithm. */
export type CodecChoice = 'compressed' | 'raw';

/** Turn a failed response into an error carrying the server's `detail`. */
const failure = async (response: Response): Promise<Error> => {
  const body: unknown = await response.json().catch(() => null);
  const detail =
    typeof body === 'object' && body !== null && 'detail' in body
      ? String(body.detail)
      : null;
  return new Error(detail ?? `${response.status} ${response.statusText}`);
};

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`${controlBase()}${path}`, {
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
};

/**
 * Fetch the camera, recording, storage and device status in one round trip.
 *
 * @returns The status.
 */
export const fetchStatus = (): Promise<Status> => request<Status>('/api/status');

/**
 * Fetch the sessions on disk, newest first.
 *
 * @returns The sessions directory and its sessions.
 */
export const fetchSessions = (): Promise<{
  sessions_dir: string;
  sessions: SessionSummary[];
}> => request('/api/sessions');

/**
 * Fetch what the page may change.
 *
 * @returns The current settings.
 */
export const fetchSettings = (): Promise<Settings> =>
  request<Settings>('/api/settings');

/**
 * Start or stop recording.
 *
 * @param recording Whether to be recording afterwards.
 * @param session Directory name to write, or undefined to name it by the time.
 */
export const setRecording = (
  recording: boolean,
  session?: string,
): Promise<unknown> =>
  request('/api/recording', {
    method: 'PUT',
    body: JSON.stringify({ recording, session: session ?? null }),
  });

/**
 * Mark the running recording. See docs/decisions.md 16.
 *
 * @param label What the mark means. Refused if empty.
 * @param data Anything else worth keeping with it.
 */
export const addMark = (
  label: string,
  data?: Record<string, unknown>,
): Promise<unknown> =>
  request('/api/events', {
    method: 'POST',
    body: JSON.stringify({ label, data: data ?? null }),
  });

/**
 * Move where future recordings are written.
 *
 * @param sessionsDir Directory to use. Created if it does not exist; refused if
 *   it cannot be written, or while a recording is running.
 */
export const setSessionsDir = (sessionsDir: string): Promise<Settings> =>
  request<Settings>('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ sessions_dir: sessionsDir }),
  });

/**
 * Change which streams the camera is asked for, from its next opening.
 *
 * @param streams Streams to turn on or off; a key left out is unchanged.
 *   Refused while recording.
 */
export const setStreams = (
  streams: Partial<Record<CaptureName, boolean>>,
): Promise<Settings> =>
  request<Settings>('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ streams }),
  });

/**
 * Try a device again, after it failed or once it has been plugged in.
 * Refused while recording. See docs/decisions.md 29.
 *
 * @param name Which device.
 */
export const reconnectDevice = (name: DeviceName): Promise<unknown> =>
  request(`/api/devices/${name}/reconnect`, { method: 'POST' });

/**
 * Change how each stream's archive is encoded, from the next recording.
 *
 * @param codecs Streams mapped to a choice; a key left out is unchanged.
 */
export const setCodecs = (
  codecs: Partial<Record<StreamName, CodecChoice>>,
): Promise<Settings> =>
  request<Settings>('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ codecs }),
  });

/**
 * URL of a live preview, for an `<img>` element.
 *
 * @param kind Which stream.
 * @returns The MJPEG URL, at the server's preview width.
 */
export const previewUrl = (kind: PreviewKind): string =>
  `${controlBase()}/stream/${kind}.mjpg`;

/**
 * URL of the live per-channel audio level stream (server-sent events).
 *
 * @returns The URL.
 */
export const audioLevelsUrl = (): string =>
  `${controlBase()}/stream/audio-levels`;

/**
 * Fetch one session in full, enough to play it back.
 *
 * @param sessionId Directory name.
 * @returns The manifest, its size and its frame range.
 */
export const fetchSession = (sessionId: string): Promise<SessionDetail> =>
  request<SessionDetail>(`/api/sessions/${encodeURIComponent(sessionId)}`);

/**
 * Delete a session and everything in it.
 *
 * @param sessionId Directory name. Refused while that session is recording.
 */
export const deleteSession = (sessionId: string): Promise<unknown> =>
  request(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });

/**
 * URL of one channel of a session's audio, for an `<audio>` element.
 *
 * @param sessionId Directory name.
 * @param channel 0 for the processed channel, 1-4 for the microphones.
 * @returns The WAV URL.
 */
export const audioUrl = (sessionId: string, channel = 0): string =>
  `${controlBase()}/api/sessions/${encodeURIComponent(sessionId)}/audio.wav` +
  `?channel=${channel}`;

/**
 * Fetch every frame's index and capture time, for audio-led playback.
 *
 * @param sessionId Directory name.
 * @returns `[index, received_monotonic]` pairs, in time order.
 */
export const fetchFrameTimes = async (
  sessionId: string,
): Promise<[number, number][]> => {
  const body = await request<{ times: [number, number][] }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/frames.json`,
  );
  return body.times;
};

/** What a frame response says about the frame it carries. */
export type FrameMeta = {
  index: number;
  /** Capture time on the monotonic axis, as recorded. */
  monotonic: number | null;
};

/**
 * Load one recorded frame, with the capture time the server reports for it.
 * The headers read here must be in the server's CORS `expose_headers`.
 *
 * @param sessionId Directory name.
 * @param index The archive's own frame index.
 * @param kind Which stream to render.
 * @param width Width to scale to before encoding.
 * @returns An object URL the caller must revoke, and the frame's metadata.
 */
export const loadFrame = async (
  sessionId: string,
  index: number,
  kind: PreviewKind,
  width: number,
): Promise<{ url: string; meta: FrameMeta }> => {
  const response = await fetch(
    `${controlBase()}/api/sessions/${encodeURIComponent(sessionId)}` +
      `/frame/${index}.jpg?kind=${kind}&width=${width}`,
  );
  if (!response.ok) throw await failure(response);
  const monotonic = response.headers.get('X-Received-Monotonic');
  return {
    url: URL.createObjectURL(await response.blob()),
    meta: {
      index: Number(response.headers.get('X-Frame-Index') ?? index),
      monotonic: monotonic === null ? null : Number(monotonic),
    },
  };
};
