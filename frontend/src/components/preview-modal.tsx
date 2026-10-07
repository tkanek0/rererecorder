import { useEffect, type ReactNode } from 'react';
import { X } from 'lucide-react';

type Props = {
  title: string;
  onClose: () => void;
  children: ReactNode;
};

/**
 * A preview enlarged over the page. Closed by Escape, a click on the
 * backdrop, or the close button.
 */
export const PreviewModal = ({ title, onClose, children }: Props) => {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <span>{title}</span>
          <button className="close" onClick={onClose} title="close">
            <X size={16} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
};
