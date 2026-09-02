/// <reference types="vite/client" />

/** Environment the page is built with. */
type ImportMetaEnv = {
  /** Where the control plane is, when it is not on this host's default port. */
  readonly VITE_CONTROL_URL?: string;
};

type ImportMeta = {
  readonly env: ImportMetaEnv;
};
