import http from 'node:http'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Vite 8.2.0's built-in server.proxy (http-proxy under the hood) was
// observed on 2026-08-25 to intermittently -- but reproducibly -- corrupt
// or hang on larger JSON responses (/api/graph, /api/tickers: 100-220KB)
// while streaming the proxied response back to the client, even though the
// FastAPI backend answered every one of those requests correctly and
// instantly (confirmed via its own access log). Smaller /api/* responses
// and large NON-proxied static assets (e.g. the >1MB
// react-force-graph-2d dev bundle) were unaffected, which narrows this to
// the proxy's response-streaming path specifically, not a general
// large-response or backend problem. Reading the backend's response into
// a buffer and writing it out in one shot (instead of relying on the
// built-in proxy's pipe/backpressure handling) reliably avoided it in
// testing -- revert to the simpler `server.proxy` declarative config
// above (see git history) once this is fixed upstream in a later Vite
// release.
function bufferedApiProxy(): Plugin {
  return {
    name: 'buffered-api-proxy',
    configureServer(server) {
      server.middlewares.use('/api', (req, res) => {
        const upstream = http.request(
          { host: 'localhost', port: 8000, path: `/api${req.url}`, method: req.method, headers: req.headers },
          (upstreamRes) => {
            const chunks: Buffer[] = []
            upstreamRes.on('data', (chunk) => chunks.push(chunk))
            upstreamRes.on('end', () => {
              res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers)
              res.end(Buffer.concat(chunks))
            })
          },
        )
        upstream.on('error', (err) => {
          res.writeHead(502, { 'Content-Type': 'text/plain' })
          res.end(`Proxy error: ${err.message}`)
        })
        req.pipe(upstream)
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), bufferedApiProxy()],
  // Vitest reads this same config (shares the React/Tailwind plugin setup
  // above) -- no separate vitest.config.ts needed. jsdom gives components
  // a real DOM to render into; setupFiles wires up jest-dom's matchers
  // (toBeInTheDocument, etc.).
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
})
