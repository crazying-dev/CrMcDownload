<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";

const links = [
  { to: "/status", label: "概览" },
  { to: "/status/cache", label: "缓存" },
  { to: "/status/hot", label: "下载热度" },
  { to: "/status/online", label: "联机" },
  { to: "/status/skins", label: "皮肤" },
];
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <RouterLink class="brand" to="/">
        CrMcDownload
        <small>镜像服务看板</small>
      </RouterLink>
      <RouterLink v-for="link in links" :key="link.to" class="nav-item" :to="link.to">
        {{ link.label }}
      </RouterLink>
      <RouterLink class="nav-item back-home" to="/">← 回官网</RouterLink>
    </aside>
    <main class="main">
      <RouterView />
    </main>
  </div>
</template>
