import { previewUrl, type CameraStatus, type PreviewKind } from '../lib/api';

/** What each preview is called on screen, and what it is for. */
const LABELS: Record<PreviewKind, string> = {
  color: 'colour',
  depth: 'depth',
  ir1: 'infrared left',
  ir2: 'infrared right',
};

type Props = {
  camera: CameraStatus;
};

/**
 * Live preview of the camera.
 *
 * The images are plain `<img>` elements pointed at MJPEG endpoints, so the
 * browser decodes them with no help from this code. Depth is shown beside
 * colour rather than behind a toggle because the failure worth catching during
 * a recording is depth going blank while colour looks perfect.
 */
export const PreviewPanel = ({ camera }: Props) => {
  const kinds: PreviewKind[] = ['color', 'depth'];
  const size = (spec: [number, number, number] | null): string =>
    spec ? `${spec[0]}x${spec[1]} @${spec[2]}` : 'off';

  return (
    <section className="panel preview">
      <h2>
        preview{' '}
        <span className={`dot ${camera.active ? 'live' : ''}`} title="camera" />
      </h2>
      <div className="previews">
        {kinds.map((kind) => (
          <figure key={kind}>
            {/* Keyed by nothing volatile: a changing src would restart the
                stream on every poll, which shows up as a flickering preview. */}
            <img src={previewUrl(kind)} alt={LABELS[kind]} />
            <figcaption>
              <span>{LABELS[kind]}</span>
              <span>
                {kind === 'depth'
                  ? size(camera.streams.depth)
                  : size(camera.streams.color)}
              </span>
            </figcaption>
          </figure>
        ))}
      </div>
      {camera.error ? <p className="error">{camera.error}</p> : null}
      {!camera.active && !camera.error ? (
        <p className="note">
          The camera opens when something is watching. If both panels stay
          black, no device was found.
        </p>
      ) : null}
    </section>
  );
};
