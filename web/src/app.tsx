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
 * The recorder page.
 *
 * Status is polled rather than pushed. It changes once a second at most and a
 * websocket would be a second thing to keep alive for no gain; the preview,
 * which does need to be continuous, is MJPEG and needs no JavaScript at all.
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

  // The listing only changes when a recording ends, so it is refreshed then
  // rather than polled - a directory of sessions is not free to stat.
  const wasRecording = status?.recording.recording ?? false;
  useEffect(() => {
    if (!wasRecording) refreshSessions();
  }, [wasRecording, refreshSessions]);

  const device = status?.camera.device;

  return (
    <div className="app">
      <header>
        <h1>rererecorder</h1>
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
          {/* Hidden while playing back. Stacking both would push the
              transport controls off screen, and watching the devices live while
              studying a recording is not a thing anyone does - the recording
              keeps running either way, since it holds both devices itself. */}
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
