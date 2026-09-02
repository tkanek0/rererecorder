/**
 * Client for the Python control plane.
 *
 * Field names are the server's own, snake_case and all, rather than converted
 * to camelCase. A translation layer would only be a place for the two to drift
 * apart, and every name here appears in `session.json` too - so what the page
 * shows and what the recording says are the same words.
 */

const DEFAULT_PORT = 8040;

/** Base HTTP URL of the control plane, derived from the page's own host. */
export const controlBase = (): string =>
  import.meta.env.VITE_CONTROL_URL ??
  `http://${window.location.hostname}:${DEFAULT_PORT}`;

/** Which preview a panel is showing. */
export type PreviewKind = 'color' | 'depth' | 'ir1' | 'ir2';

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

/** What the camera is doing right now. */
export type CameraStatus = {
  active: boolean;
  fps: number;
  error: string | null;
  device: DeviceInfo | null;
  streams: StreamConfig;
  timestamp_domain: string;
  listeners: number;
};

/** What the camera has contributed to the running session. */
export type VideoState = {
  frames: number;
  dropped: number;
  skipped: number;
  skipped_unpaired: number;
  skipped_duplicate: number;
  skipped_warmup: number;
  fps: number | null;
  timestamp_domain: string;
  error: string | null;
};

/** What the array has contributed, when it is being recorded at all. */
export type AudioState = {
  seconds: number;
  filled: number;
  gaps: number;
  dropped_by_reader: number;
  overruns: number;
  clock_points: number;
  error: string | null;
};

/** The recorder's own view of the session in progress. */
export type RecordingState = {
  recording: boolean;
  session_id: string | null;
  directory: string | null;
  seconds: number;
  size_bytes: number;
  video: VideoState | null;
  audio: AudioState | null;
  errors: string[];
};

/** Where recordings go, and how long the disk lasts at the current rate. */
export type StorageStatus = {
  sessions_dir: string;
  free_bytes?: number;
  total_bytes?: number;
  write_bytes_per_s: number | null;
  /** Whether the remaining time comes from a recording happening now. */
  rate_is_live?: boolean;
  seconds_left: number | null;
  error?: string;
};

/** Everything the page polls for. */
export type Status = {
  camera: CameraStatus;
  recording: RecordingState;
  storage: StorageStatus;
};

/** One finished session, as its manifest describes it. */
export type SessionSummary = {
  session_id: string;
  duration_s: number | null;
  started_at: { monotonic: number; realtime: number } | null;
  video: {
    frames: number;
    dropped: number;
    skipped: number;
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
  last_monotonic?: number | null;
  streams?: { color: boolean; depth: boolean; infrared: boolean };
  aligned?: boolean;
  codecs?: Record<string, string> | null;
  color_format?: string | null;
  error?: string;
};

/** One session in full: its manifest, its size and its frame range. */
export type SessionDetail = SessionSummary & {
  size_bytes: number;
  archive: ArchiveDetail;
  clock_reference: string;
  stopped_at: { monotonic: number; realtime: number } | null;
};

/** What the page is allowed to change, and what it is set to. */
export type Settings = {
  sessions_dir: string;
  writable: boolean;
  streams: StreamConfig;
  codecs: Record<string, string>;
};

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`${controlBase()}${path}`, {
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  });
  if (!response.ok) {
    // The server puts its explanation in `detail`; surfacing the status alone
    // would turn "stop the recording first" into "409".
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail ?? `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
};

/** Fetch the camera, recording and storage status in one round trip. */
export const fetchStatus = (): Promise<Status> => request<Status>('/api/status');

/** Fetch the sessions on disk, newest first. */
export const fetchSessions = (): Promise<{
  sessions_dir: string;
  sessions: SessionSummary[];
}> => request('/api/sessions');

/** Fetch what the page may change. */
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
): Promise<RecordingState> =>
  request<RecordingState>('/api/recording', {
    method: 'PUT',
    body: JSON.stringify({ recording, session: session ?? null }),
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

/** URL of a live preview, for an `<img>` element. */
export const previewUrl = (kind: PreviewKind, width = 640): string =>
  `${controlBase()}/stream/${kind}.mjpg?width=${width}`;

/** Fetch one session in full, enough to play it back. */
export const fetchSession = (sessionId: string): Promise<SessionDetail> =>
  request<SessionDetail>(`/api/sessions/${encodeURIComponent(sessionId)}`);

/**
 * Delete a session and everything in it.
 *
 * @param sessionId Directory name. Refused while that session is recording.
 */
export const deleteSession = (
  sessionId: string,
): Promise<{ deleted: string; freed_bytes: number }> =>
  request(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });

/**
 * URL of one recorded frame, for an `<img>` element.
 *
 * @param sessionId Directory name.
 * @param index The archive's own frame index, not a position in a sequence.
 * @param kind Which stream to render.
 * @param width Width to scale to before encoding.
 */
export const frameUrl = (
  sessionId: string,
  index: number,
  kind: PreviewKind,
  width = 640,
): string =>
  `${controlBase()}/api/sessions/${encodeURIComponent(sessionId)}/frame/${index}.jpg` +
  `?kind=${kind}&width=${width}`;

/** What a frame response says about the frame it carries. */
export type FrameMeta = {
  index: number;
  /** Capture time on the monotonic axis, as recorded. */
  monotonic: number | null;
};

/**
 * Load one recorded frame, with the capture time the server reports for it.
 *
 * @param sessionId Directory name.
 * @param index The archive's own frame index.
 * @param kind Which stream to render.
 * @param width Width to scale to before encoding.
 *
 * Returns an object URL the caller must revoke. The capture time comes from a
 * response header rather than being computed from the index, because frames are
 * not evenly spaced - a set the camera mispaired leaves a gap.
 */
export const loadFrame = async (
  sessionId: string,
  index: number,
  kind: PreviewKind,
  width = 640,
): Promise<{ url: string; meta: FrameMeta }> => {
  const response = await fetch(frameUrl(sessionId, index, kind, width));
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail ?? `${response.status} ${response.statusText}`);
  }
  const monotonic = response.headers.get('X-Capture-Monotonic');
  return {
    url: URL.createObjectURL(await response.blob()),
    meta: {
      index: Number(response.headers.get('X-Frame-Index') ?? index),
      monotonic: monotonic === null ? null : Number(monotonic),
    },
  };
};
