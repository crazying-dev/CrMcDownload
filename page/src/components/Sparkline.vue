<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    values: number[];
    width?: number;
    height?: number;
    color?: string;
    bars?: boolean;
  }>(),
  { width: 320, height: 80, color: "var(--accent)", bars: false }
);

const geometry = computed(() => {
  const data = (props.values || []).map((v) => Number(v) || 0);
  if (data.length === 0) return { points: "", rects: [], max: 0 };
  const max = Math.max(...data, 1);
  const stepX = data.length > 1 ? props.width / (data.length - 1) : props.width;
  const points = data.map(
    (v, i) => `${(i * stepX).toFixed(1)},${(props.height - (v / max) * props.height).toFixed(1)}`
  );
  const barW = props.width / Math.max(1, data.length);
  const rects = data.map((v, i) => ({
    x: i * barW,
    y: props.height - (v / max) * props.height,
    w: Math.max(1, barW - 2),
    h: (v / max) * props.height,
  }));
  return { points: points.join(" "), rects, max };
});
</script>

<template>
  <svg
    :width="width"
    :height="height"
    :viewBox="`0 0 ${width} ${height}`"
    preserveAspectRatio="none"
    role="img"
  >
    <template v-if="bars">
      <rect
        v-for="(r, i) in geometry.rects"
        :key="i"
        :x="r.x"
        :y="r.y"
        :width="r.w"
        :height="r.h"
        :fill="color"
        rx="2"
      />
    </template>
    <polyline
      v-else
      :points="geometry.points"
      fill="none"
      :stroke="color"
      stroke-width="2"
      stroke-linejoin="round"
      stroke-linecap="round"
    />
  </svg>
</template>
