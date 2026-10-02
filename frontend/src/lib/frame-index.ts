/** Finding the frame that belongs to an instant. */

/**
 * The frame whose capture time is nearest an instant, by binary search.
 *
 * @param times `[index, received_monotonic]` pairs, in time order.
 * @param at The instant to look up.
 * @returns The frame index, clamped to the ends, or null if there are no frames.
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
 * @returns The capture time, or null if the index is absent.
 */
export const timeOfFrame = (
  times: readonly [number, number][],
  index: number,
): number | null => times.find(([i]) => i === index)?.[1] ?? null;
