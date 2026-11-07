<script setup lang="ts">
import { useRoute } from "vue-router";
import { computed } from "vue";
import { RouterView } from "vue-router";
import SiteHeader from "@/components/site/SiteHeader.vue";
import SiteFooter from "@/components/site/SiteFooter.vue";

const route = useRoute();
// 看板走另一套侧边栏布局，官网保持顶部导航
const inDashboard = computed(() => route.path.startsWith("/status"));
</script>

<template>
  <RouterView v-if="inDashboard" />
  <div v-else class="site">
    <SiteHeader />
    <main class="site-main">
      <RouterView />
    </main>
    <SiteFooter />
  </div>
</template>
