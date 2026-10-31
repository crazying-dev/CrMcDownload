import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import Overview from "@/views/Overview.vue";

const routes: RouteRecordRaw[] = [
  { path: "/", name: "overview", component: Overview, meta: { title: "概览" } },
  {
    path: "/cache",
    name: "cache",
    component: () => import("@/views/Cache.vue"),
    meta: { title: "缓存" },
  },
  {
    path: "/download",
    name: "download",
    component: () => import("@/views/Download.vue"),
    meta: { title: "下载热度" },
  },
  {
    path: "/online",
    name: "online",
    component: () => import("@/views/Online.vue"),
    meta: { title: "联机" },
  },
  {
    path: "/skins",
    name: "skins",
    component: () => import("@/views/Skins.vue"),
    meta: { title: "皮肤" },
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

// base 与服务端 /page 前缀一致；构建产物部署在 /page/ 下时路由才不会丢前缀。
export const router = createRouter({
  history: createWebHistory("/page/"),
  routes,
});
