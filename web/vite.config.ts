import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    // Every interface, so the page can be opened from another machine.
    host: '0.0.0.0',
    port: 5177,
    strictPort: true,
  },
});
