import { loadEnv, defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd())

  return {
    server: {
      host: '0.0.0.0',
      port: 8090,
      https: false,
      // Vite 默认校验 Host 头（防 DNS rebinding）。公网隧道域名是随机分配的，
      // 不放行会返回 "Blocked request. This host is not allowed."，导致公网访问全站 403。
      // 可用环境变量 VITE_ALLOWED_HOSTS（逗号分隔）追加自定义域名。
      allowedHosts: (
        env.VITE_ALLOWED_HOSTS ||
        '.serveousercontent.com,.lhr.life,.pinggy.net,.pinggy-free.link,.localhost.run,.trycloudflare.com'
      )
        .split(',')
        .map((s: string) => s.trim())
        .filter(Boolean),
      proxy: {
        '/api': {
          target: env.VITE_PROXY_TARGET || env.VITE_API_BASE_URL || 'http://localhost:7860',
          changeOrigin: true,
        },
        // 对象存储反代：后端返回的资源地址为相对路径（如 /minio/agentchat/icons/x.jpg），
        // 由这里转发到对象存储服务并剥掉 /minio 前缀。
        // 这样无论通过 localhost 还是公网隧道访问，资源都能正确加载。
        '/minio': {
          target: env.VITE_MINIO_TARGET || 'http://minio:9000',
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/minio/, ''),
        },
      },
    },
    css: {
      preprocessorOptions: {
        scss: {
          api: 'modern',
        },
      },
    },
    plugins: [
      vue(),
      AutoImport({ resolvers: [ElementPlusResolver()] }),
      Components({ resolvers: [ElementPlusResolver()] }),
    ],
  }
})
