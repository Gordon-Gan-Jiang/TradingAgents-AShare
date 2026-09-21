import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import { execSync } from 'node:child_process'

function runGit(cmd: string): string {
  try {
    return execSync(cmd, { cwd: __dirname, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return ''
  }
}

function getBuildMeta() {
  const commit =
    process.env.VERCEL_GIT_COMMIT_SHA?.slice(0, 7) ||
    runGit('git rev-parse --short HEAD') ||
    'unknown'

  const date =
    (process.env.VERCEL_GIT_COMMIT_TIMESTAMP
      ? new Date(process.env.VERCEL_GIT_COMMIT_TIMESTAMP).toISOString().slice(0, 10)
      : '') ||
    runGit('git show -s --format=%cd --date=format:%Y-%m-%d HEAD') ||
    new Date().toISOString().slice(0, 10)

  return {
    commit,
    date,
    version: `${date}+${commit}`,
  }
}

const buildMeta = getBuildMeta()

// 用 127.0.0.1 避免 macOS 上 localhost 解析到 ::1，而后端 uvicorn 只监听 IPv4。
const backendTarget = process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'

const apiProxy = {
  '/api': {
    target: backendTarget,
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/api/, ''),
  },
  '/v1': { target: backendTarget, changeOrigin: true },
  '/healthz': { target: backendTarget, changeOrigin: true },
  '/openapi.json': { target: backendTarget, changeOrigin: true },
  '/docs': { target: backendTarget, changeOrigin: true },
}

export default defineConfig({
  define: {
    __APP_BUILD_COMMIT__: JSON.stringify(buildMeta.commit),
    __APP_BUILD_DATE__: JSON.stringify(buildMeta.date),
    __APP_BUILD_VERSION__: JSON.stringify(buildMeta.version),
  },
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy: apiProxy,
  },
})
