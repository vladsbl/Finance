import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Dev-only proxy onto the FastAPI backend (api/main.py, uvicorn on
    // :8000) -- lets every component call relative paths like
    // fetch('/api/daily-summary') instead of hardcoding an absolute
    // origin, so the same code keeps working unchanged once this becomes
    // a Tauri app or a PWA (only this proxy config would need to move to
    // wherever that shell's dev tooling equivalent is). The FastAPI side
    // already has CORS enabled for this port too (see api/main.py's
    // DEV_ORIGINS), so direct cross-origin calls would also work -- this
    // proxy is the cleaner default regardless.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  // Vitest reads this same config (shares the React/Tailwind plugin setup
  // above) -- no separate vitest.config.ts needed. jsdom gives components
  // a real DOM to render into; setupFiles wires up jest-dom's matchers
  // (toBeInTheDocument, etc.).
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
})
