<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api, humanBytes, type CacheStats } from "@/api/client";
import StatCard from "@/components/StatCard.vue";

const data = ref<CacheStats | null>(null);
const error = ref("");
const loading = ref(true);
let controller: AbortController | null = null;

async function load() {
  loading.value = true;
  error.value = "";
  controller?.abort();
  controller = new AbortController();
  try {
    data.value = await api.cache(controller.signal);
  } catch (e) {
    if ((e as Error).name !== "AbortError") error.value = String((e as Error).message || e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
onUnmounted(() => controller?.abort());

const stats = computed<CacheStats>(() => data.value ?? {});
const usedPct = computed(() => {
  const max = stats.value.max_bytes || 0;
  if (!max) return 0;
  return Math.min(100, Math.round(((stats.value.bytes || 0) / max) * 100));
});
</script>

<template>
  <div>
    <h1 class="page-title">缓存</h1>
    <p class="page-sub">磁盘缓存占用与清理统计 · <button class="ghost" @click="load">刷新</button></p>

    <div v-if="loading" class="state">加载中……</div>
    <div v-else-if="error" class="state error">加载失败：{{ error }}</div>
    <template v-else>
      <div class="card" style="margin-bottom: 16px">
        <div class="stat-label">容量水位（{{ usedPct }}%）</div>
        <div class="meter" style="height: 12px">
          <span :style="{ width: usedPct + '%' }" />
        </div>
        <div class="stat-hint">
          已用 {{ humanBytes(stats.bytes) }} / 上限 {{ humanBytes(stats.max_bytes) }}
        </div>
      </div>
      <div class="grid">
        <StatCard label="缓存占用" :value="humanBytes(stats.bytes)" />
        <StatCard label="单文件上限" :value="humanBytes(stats.max_file_bytes)" />
        <StatCard label="LRU 淘汰" :value="humanBytes(stats.evicted_bytes)" hint="因容量上限被驱逐" />
        <StatCard label="过期清扫" :value="humanBytes(stats.swept_bytes)" hint="TTL 到期清理" />
      </div>
    </template>
  </div>
</template>
