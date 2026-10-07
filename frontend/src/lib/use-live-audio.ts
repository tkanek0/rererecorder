import { useEffect, useState } from 'react';

import { listenUrl } from './api';
import { errorMessage } from './use-action';

/** Seconds of audio queued ahead of the playhead when playback (re)starts. */
const LEAD_S = 0.08;

/** Queued audio beyond this is dropped, so a stall cannot leave it lagging. */
const MAX_LAG_S = 0.5;

/** Read the sample rate out of an `audio/L16; rate=...` content type. */
const rateOf = (contentType: string | null): number => {
  const rate = Number(/rate=(\d+)/.exec(contentType ?? '')?.[1]);
  if (!rate) throw new Error(`not L16 audio: ${contentType ?? 'no type'}`);
  return rate;
};

/**
 * Play one channel of the array as it is captured, while a channel is given.
 * Each network chunk is scheduled right after the previous one; arriving
 * late restarts the queue a little ahead, and running too far ahead drops a
 * chunk, which keeps the sound within a fraction of a second of the room.
 *
 * Call it with a channel only after a click: browsers refuse sound until then.
 *
 * @param channel Which channel to play, or null to stay silent and closed.
 * @returns Why it stopped, or null while it plays or is silent.
 */
export const useLiveAudio = (channel: number | null): string | null => {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (channel === null) return;
    const abort = new AbortController();
    const context = new AudioContext();
    let next = 0;
    // A chunk may end halfway through a sample; its first byte waits here.
    let carry: number | null = null;

    const play = (bytes: Uint8Array, rate: number) => {
      let joined = bytes;
      if (carry !== null) {
        joined = new Uint8Array(bytes.length + 1);
        joined[0] = carry;
        joined.set(bytes, 1);
      }
      const usable = joined.length - (joined.length % 2);
      carry = usable < joined.length ? joined[usable] : null;
      if (usable === 0) return;

      const view = new DataView(joined.buffer, joined.byteOffset, usable);
      const buffer = context.createBuffer(1, usable / 2, rate);
      const samples = buffer.getChannelData(0);
      for (let i = 0; i < samples.length; i++) {
        samples[i] = view.getInt16(i * 2, true) / 32768;
      }

      const now = context.currentTime;
      if (next < now) next = now + LEAD_S;
      if (next - now > MAX_LAG_S) return;
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      source.start(next);
      next += buffer.duration;
    };

    const run = async () => {
      setError(null);
      await context.resume();
      const response = await fetch(listenUrl(channel), { signal: abort.signal });
      if (!response.ok || !response.body) {
        throw new Error(`${response.status} ${response.statusText}`);
      }
      const rate = rateOf(response.headers.get('Content-Type'));
      const reader = response.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) throw new Error('the server ended the stream');
        play(value, rate);
      }
    };

    run().catch((cause: unknown) => {
      if (!abort.signal.aborted) setError(errorMessage(cause));
    });

    return () => {
      abort.abort();
      void context.close();
    };
  }, [channel]);

  return channel === null ? null : error;
};
