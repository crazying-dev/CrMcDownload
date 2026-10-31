<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api, humanBytes, humanDuration, type Overview, type Metrics, type ServersOverview } from "@/api/client";
import StatCard from "@/components/StatCard.vue";

const data = ref<Overview | null>(null);
const error = ref("");
const loading = ref(true);
let controller: AbortController | null = null;

async function load() {
  loading.value = true;
  error.value = "";
  controller?.abort();
  controller = new AbortController();
  try {
    data.value = await api.overview(controller.signal);
  } catch (e) {
    if ((e as Error).name !== "AbortError") error.value = String((e as Error).message || e);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
onUnmounted(() => controller?.abort());

const metrics = (): Metrics => data.value?.metrics ?? {};
const servers = (): ServersOverview => data.value?.servers ?? {};
</script>

<template>
  <div>
    <h1 class="page-title">概览</h1>
    <p class="page-sub">镜像服务运行状态总览 · <button class="ghost" @click="load">刷新</button></p>

    <div v-if="loading" class="state">加载中……</div>
    <div v-else-if="error" class="state error">加载失败：{{ error }}</div>
    <div v-else class="grid">
      <StatCard label="运行时长" :value="humanDuration(metrics().uptime)" />
      <StatCard label="缓存命中" :value="metrics().hits ?? 0" hint="命中缓存的请求数" />
      <StatCard label="回源次数" :value="metrics().misses ?? 0" hint="未命中、回源拉取" />
      <StatCard label="错误" :value="metrics().errors ?? 0" hint="回源/代理失败" />
      <StatCard label="回源流量" :value="humanBytes(metrics().upstream_bytes)" hint="实际从上游下载的字节" />
      <StatCard
        label="收录服务器"
        :value="servers().servers ?? 0"
        :hint="`在线 ${servers().online_servers ?? 0} · 玩家 ${servers().players ?? 0}`"
      />
    </div>
  </div>
</template>
