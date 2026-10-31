<script setup lang="ts">
export interface Column {
  key: string;
  label: string;
  // 可选：把单元格值渲染成字符串（默认原样显示）
  format?: (row: Record<string, unknown>) => string;
}

defineProps<{
  columns: Column[];
  rows: Array<Record<string, unknown>>;
}>();
</script>

<template>
  <div class="card" style="padding: 0; overflow: hidden">
    <table class="data">
      <thead>
        <tr>
          <th v-for="col in columns" :key="col.key">{{ col.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in rows" :key="i">
          <td v-for="col in columns" :key="col.key">
            {{ col.format ? col.format(row) : row[col.key] }}
          </td>
        </tr>
        <tr v-if="rows.length === 0">
          <td :colspan="columns.length" class="muted" style="text-align: center">暂无数据</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
