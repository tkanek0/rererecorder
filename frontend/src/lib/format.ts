/** Formatting shared by the panels, so the same number reads the same way. */

/**
 * Render a byte count in the largest unit that keeps it readable.
 *
 * @param bytes The count, or null when it is not known.
 */
export const bytes = (bytes: number | null | undefined): string => {
  if (bytes === null || bytes === undefined) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
};

/**
 * Render a duration as hours, minutes and seconds.
 *
 * @param seconds The duration, or null when it is not known.
 */
export const duration = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined) return '-';
  const whole = Math.floor(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${seconds.toFixed(1)}s`;
};

/**
 * Render a rate in bytes per second.
 *
 * @param rate The rate, or null when nothing is being written.
 */
export const rate = (rate: number | null | undefined): string =>
  rate === null || rate === undefined ? '-' : `${bytes(rate)}/s`;

/**
 * Render a wall-clock time from epoch seconds.
 *
 * @param epoch Seconds since the epoch, or null.
 */
export const clockTime = (epoch: number | null | undefined): string =>
  epoch === null || epoch === undefined
    ? '-'
    : new Date(epoch * 1000).toLocaleString();
