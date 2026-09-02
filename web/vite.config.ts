import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  // Relative, so the built page works wherever the server mounts it.
  base: './',
  server: {
    // Listen on every interface: a recorder on a Pi is driven from a laptop.
    host: '0.0.0.0',
    // Pinned rather than left to drift. 5173, 5175 and 5176 are taken on this
    // machine by other projects; a viewer that moves ports between restarts is
    // a nuisance to share.
    port: 5177,
    strictPort: true,
  },
});
