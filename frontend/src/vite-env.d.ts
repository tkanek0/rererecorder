/// <reference types="vite/client" />

/** Environment the page is built with. `interface`, to merge with vite's. */
interface ImportMetaEnv {
  /** Port the control plane listens on, when it is not the default 8040. */
  readonly VITE_CONTROL_PORT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
