<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api, humanBytes, type Metrics } from "@/api/client";
import StatCard from "@/components/StatCard.vue";
import Sparkline from "@/components/Sparkline.vue";

const data = ref<Metrics | null>(null);
const error = ref("");
const loading = ref(true);
let controller: AbortController | null = null;

async function load() {
  loading.value = true;
  error.value = "";
  controller?.abort();
  controller = new AbortController();
  try {
    const res = await api.download(controller.signal);
    data.value = res.metrics;
  } catch (e) {
    if ((e as Error).name !== "AbortError") error.value = String((e as Error).message || e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
onUnmounted(() => controller?.abort());

const metrics = computed<Metrics>(() => data.value ?? {});
const hitRate = computed(() => {
  const hits = metrics.value.hits || 0;
  const misses = metrics.value.misses || 0;
  const total = hits + misses;
  return total ? Math.round((hits / total) * 100) : 0;
});
</script>

<template>
  <div>
    <h1 class="page-title">下载热度</h1>
    <p class="page-sub">镜像命中与回源情况 · <button class="ghost" @click="load">刷新</button></p>

    <div v-if="loading" class="state">加载中……</div>
    <div v-else-if="error" class="state error">加载失败：{{ error }}</div>
    <template v-else>
      <div class="card" style="margin-bottom: 16px">
        <div class="stat-label">命中率（{{ hitRate }}%）</div>
        <div class="meter" style="height: 12px"><span :style="{ width: hitRate + '%' }" /></div>
        <div class="stat-hint">命中越多，回源流量与延迟越低</div>
        <div style="margin-top: 12px">
          <Sparkline :values="[metrics.hits || 0, metrics.misses || 0]" :bars="true" :width="260" :height="90" />
        </div>
        <div class="muted" style="font-size: 12px">左：缓存命中　右：回源</div>
      </div>
      <div class="grid">
        <StatCard label="缓存命中" :value="metrics.hits ?? 0" />
        <StatCard label="回源次数" :value="metrics.misses ?? 0" />
        <StatCard label="回源流量" :value="humanBytes(metrics.upstream_bytes)" />
        <StatCard label="错误" :value="metrics.errors ?? 0" />
      </div>
    </template>
  </div>
</template>
