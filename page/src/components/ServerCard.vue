<script setup lang="ts">
import { computed } from "vue";
import type { ServerListing } from "@/api/client";
import Sparkline from "./Sparkline.vue";

const props = defineProps<{ server: ServerListing }>();
const emit = defineEmits<{ (e: "detail", server: ServerListing): void }>();

function tone(level: number | null): string {
  if (level === null) return "badge-gray";
  if (level < 100) return "badge-green";
  if (level < 300) return "badge-amber";
  return "badge-red";
}

const latencyClass = computed(() => tone(props.server.latency_ms));
const onlinePct = computed(() => {
  if (!props.server.max) return 0;
  return Math.min(100, Math.round((props.server.online / props.server.max) * 100));
});
const historyLatency = computed(() =>
  (props.server.history || []).map((h) => Number(h.latency_ms || 0))
);
</script>

<template>
  <div class="card server-card">
    <div class="server-row">
      <strong>{{ server.label || server.host }}</strong>
      <span v-if="server.version" class="badge badge-blue">{{ server.version }}</span>
      <span :class="['badge', latencyClass]">
        {{ server.ok ? (server.latency_ms ?? 0) + ' ms' : '离线' }}
      </span>
    </div>
    <div class="muted" style="font-size: 12px">
      {{ server.host }}:{{ server.port }}
      <span v-if="!server.ok"> · {{ server.error || "无法连接" }}</span>
    </div>
    <div v-if="server.motd" class="motd">{{ server.motd }}</div>
    <div>
      <div class="stat-label">在线 {{ server.online }} / {{ server.max }}</div>
      <div class="meter"><span :style="{ width: onlinePct + '%' }" /></div>
    </div>
    <Sparkline v-if="historyLatency.length > 1" :values="historyLatency" :height="48" :width="260" />
    <div class="muted" v-if="server.ok && !server.players_visible">
      服务器未公开玩家列表（仅显示人数）
    </div>
    <div class="server-row" style="margin-top: 6px">
      <span class="muted">上报 {{ server.reports }} · 热度 {{ server.hot }}</span>
      <button class="ghost refresh" @click="emit('detail', server)">详情</button>
    </div>
  </div>
</template>

<style scoped>
.motd {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
