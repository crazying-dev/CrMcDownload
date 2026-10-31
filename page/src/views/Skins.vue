<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api, type SkinStatus } from "@/api/client";
import StatCard from "@/components/StatCard.vue";

const data = ref<SkinStatus | null>(null);
const error = ref("");
const loading = ref(true);
let controller: AbortController | null = null;

async function load() {
  loading.value = true;
  error.value = "";
  controller?.abort();
  controller = new AbortController();
  try {
    data.value = await api.skins(controller.signal);
  } catch (e) {
    if ((e as Error).name !== "AbortError") error.value = String((e as Error).message || e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
onUnmounted(() => controller?.abort());

const status = computed<SkinStatus>(() => data.value ?? {});
const lastText = computed(() => {
  const ts = status.value.last_updated || 0;
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
});
</script>

<template>
  <div>
    <h1 class="page-title">皮肤</h1>
    <p class="page-sub">镜像托管的自定义皮肤 · <button class="ghost" @click="load">刷新</button></p>

    <div v-if="loading" class="state">加载中……</div>
    <div v-else-if="error" class="state error">加载失败：{{ error }}</div>
    <div v-else class="grid">
      <StatCard label="已存皮肤" :value="status.count ?? 0" hint="服务端 /skin 目录内合法 PNG 数" />
      <StatCard label="最近更新" :value="lastText" />
    </div>
  </div>
</template>
