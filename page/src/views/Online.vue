<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api, type ServerListing } from "@/api/client";
import ServerCard from "@/components/ServerCard.vue";

const servers = ref<ServerListing[]>([]);
const total = ref(0);
const query = ref("");
const error = ref("");
const loading = ref(true);
let controller: AbortController | null = null;

async function load() {
  loading.value = true;
  error.value = "";
  controller?.abort();
  controller = new AbortController();
  try {
    const res = await api.serverList(query.value.trim(), 30, controller.signal);
    servers.value = res.hits;
    total.value = res.total;
  } catch (e) {
    if ((e as Error).name !== "AbortError") error.value = String((e as Error).message || e);
  } finally {
    loading.value = false;
  }
}

async function detail(server: ServerListing) {
  try {
    const full = await api.serverDetail(server.host, server.port);
    const index = servers.value.findIndex((s) => s.host === server.host && s.port === server.port);
    if (index >= 0) servers.value[index] = full;
  } catch (e) {
    error.value = String((e as Error).message || e);
  }
}

onMounted(load);
onUnmounted(() => controller?.abort());
</script>

<template>
  <div>
    <h1 class="page-title">联机</h1>
    <p class="page-sub">收录服务器热度榜与最近状态</p>

    <div class="toolbar">
      <input class="field" v-model="query" placeholder="搜索地址 / 名称 / MOTD" @keyup.enter="load" />
      <button class="ghost" @click="load">搜索</button>
      <span class="muted">共 {{ total }} 台</span>
    </div>

    <div v-if="loading" class="state">加载中……</div>
    <div v-else-if="error" class="state error">加载失败：{{ error }}</div>
    <div v-else-if="servers.length === 0" class="state">热度榜为空（服务端尚未探测出结果）</div>
    <div v-else class="grid">
      <ServerCard v-for="s in servers" :key="s.host + ':' + s.port" :server="s" @detail="detail" />
    </div>
  </div>
</template>
