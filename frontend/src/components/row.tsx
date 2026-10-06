/** How a value reads: fine, worth a look, or wrong. */
export type Tone = 'good' | 'warn' | 'bad';

/**
 * One labelled line of a panel's readout, coloured when the value means trouble.
 *
 * @param props The label, the value and its tone.
 * @returns The row.
 */
export const Row = ({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: Tone;
}) => (
  <div className="row">
    <span className="label">{label}</span>
    <span className={`value ${tone ?? ''}`}>{value}</span>
  </div>
);
