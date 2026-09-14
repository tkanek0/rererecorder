/** Finding the frame that belongs to an instant. */

/**
 * The frame whose capture time is nearest an instant.
 *
 * @param times `[index, received_monotonic]` pairs, in time order.
 * @param at The instant to look up.
 *
 * A binary search rather than a scan: playback asks this on every animation
 * frame, and an hour of recording is 108,000 entries.
 *
 * Returns the index, or null if there are no frames. Instants outside the
 * recording clamp to its ends, which is what seeking past either edge should
 * do.
 */
export const nearestFrame = (
  times: readonly [number, number][],
  at: number,
): number | null => {
  if (times.length === 0) return null;
  let low = 0;
  let high = times.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (times[mid][1] < at) low = mid + 1;
    else high = mid;
  }
  // `low` is the first frame at or after `at`; the one before may be closer.
  const after = times[low];
  if (low === 0) return after[0];
  const before = times[low - 1];
  return at - before[1] <= after[1] - at ? before[0] : after[0];
};

/**
 * The capture time of a frame index.
 *
 * @param times `[index, received_monotonic]` pairs, in time order.
 * @param index The frame to look up.
 */
export const timeOfFrame = (
  times: readonly [number, number][],
  index: number,
): number | null => times.find(([i]) => i === index)?.[1] ?? null;
