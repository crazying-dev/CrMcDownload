<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";

const links = [
  { to: "/", label: "概览" },
  { to: "/cache", label: "缓存" },
  { to: "/download", label: "下载热度" },
  { to: "/online", label: "联机" },
  { to: "/skins", label: "皮肤" },
];
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        CrMcDownload
        <small>镜像服务看板</small>
      </div>
      <RouterLink
        v-for="link in links"
        :key="link.to"
        class="nav-item"
        :to="link.to"
      >{{ link.label }}</RouterLink>
    </aside>
    <main class="main">
      <RouterView />
    </main>
  </div>
</template>
