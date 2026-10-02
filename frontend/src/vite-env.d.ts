/// <reference types="vite/client" />

/** Environment the page is built with. */
type ImportMetaEnv = {
  /** Port the control plane listens on, when it is not the default 8040. */
  readonly VITE_CONTROL_PORT?: string;
  /** Whole control plane URL, when it is not on the host serving this page. */
  readonly VITE_CONTROL_URL?: string;
};

type ImportMeta = {
  readonly env: ImportMetaEnv;
};
