import { useCallback, useEffect, useState } from 'react';

import {
  fetchSessions,
  fetchSettings,
  fetchStatus,
  type SessionSummary,
  type Settings,
  type Status,
} from './lib/api';
import { DevicesPanel } from './components/devices-panel';
import { PlayerPanel } from './components/player-panel';
import { RecordingPanel } from './components/recording-panel';
import { SessionList } from './components/session-list';
import { StoragePanel } from './components/storage-panel';

/** How often the status is polled, in milliseconds. */
const POLL_MS = 1000;

/**
 * The recorder page. Status is polled once a second rather than pushed; the
 * continuous previews are MJPEG and server-sent events.
 */
export const App = () => {
  const [status, setStatus] = useState<Status | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [offline, setOffline] = useState<string | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);

  const refreshSessions = useCallback(() => {
    fetchSessions()
      .then((result) => setSessions(result.sessions))
      .catch(() => setSessions([]));
    fetchSettings()
      .then(setSettings)
      .catch(() => setSettings(null));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await fetchStatus();
        if (!cancelled) {
          setStatus(next);
          setOffline(null);
        }
      } catch (cause) {
        if (!cancelled) {
          setOffline(cause instanceof Error ? cause.message : String(cause));
        }
      }
    };
    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  // The listing changes only when a recording ends, so refresh then, not on poll.
  const wasRecording = status?.recording.recording ?? false;
  useEffect(() => {
    if (!wasRecording) refreshSessions();
  }, [wasRecording, refreshSessions]);

  const device = status?.camera.device;

  return (
    <div className="app">
      <header>
        <h1>ReReRecorder</h1>
        <span className="sub">
          {device
            ? `${device.name} · ${device.serial} · FW ${device.firmware} · USB ${device.usb_type}`
            : offline
              ? `control plane unreachable: ${offline}`
              : 'looking for a camera'}
        </span>
      </header>

      {status ? (
        <>
          {/* Hidden while playing back, to keep the transport on screen. */}
          {playing ? null : (
            <DevicesPanel
              devices={status.devices}
              settings={settings}
              recording={status.recording.recording}
              onChanged={refreshSessions}
            />
          )}
          <RecordingPanel
            recording={status.recording}
            writeRate={status.storage.write_bytes_per_s}
            onChanged={refreshSessions}
          />
          <StoragePanel
            storage={status.storage}
            settings={settings}
            recording={status.recording.recording}
            onChanged={refreshSessions}
          />
          {playing ? (
            <PlayerPanel
              sessionId={playing}
              onClose={() => setPlaying(null)}
            />
          ) : null}
          <SessionList
            sessions={sessions}
            selected={playing}
            recordingId={
              status.recording.recording ? status.recording.session_id : null
            }
            onSelect={setPlaying}
            onDeleted={() => {
              // The one being played may be the one just deleted.
              setPlaying(null);
              refreshSessions();
            }}
          />
        </>
      ) : (
        <section className="panel" style={{ gridColumn: '1 / -1' }}>
          <p className="note">{offline ?? 'connecting…'}</p>
        </section>
      )}
    </div>
  );
};
