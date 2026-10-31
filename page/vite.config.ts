import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// base "/page/"：产物由镜像服务在 /page 前缀下直出，资源路径都带 /page/ 前缀。
// 开发态把 /api 转发到本地镜像服务（默认 127.0.0.1:8081），前端只用相对路径取数。
export default defineConfig({
  base: "/page/",
  plugins: [vue()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8081", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8081", changeOrigin: true },
    },
  },
});
