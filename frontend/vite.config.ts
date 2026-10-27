import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

// /api、/ws 代理——dev（server）与生产静态预览（preview）共用（B1，2026-10-27）
const apiProxy = {
  '/api': {
    target: 'http://localhost:8000',
    changeOrigin: true
  },
  '/ws': {
    target: 'ws://localhost:8000',
    ws: true
  }
}

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    port: 3000,
    proxy: apiProxy
  },
  // B1：vite preview 不带 server.proxy——不单独配 preview.proxy 的话静态模式下
  // /api 全部 404（preview 是独立 server 实例）；SPA 子路由由 vite preview 默认
  // history fallback（appType:'spa'）处理
  preview: {
    port: 3000,
    host: true,
    proxy: apiProxy
  }
})
