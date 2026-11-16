<script setup lang="ts">
/* --- 分页器 --- */
import { ref, watch } from "vue";

const props = defineProps<{
  page: number;
  pages: number;
  total: number;
}>();

const emit = defineEmits<{
  "update:page": [page: number];
}>();

const jumpPage = ref(Math.max(1, props.page || 1));
watch(() => props.page, (value) => { jumpPage.value = Math.max(1, value || 1); });

function jump() {
  const pages = Math.max(1, props.pages || 1);
  const value = Math.max(1, Math.min(pages, Math.floor(Number(jumpPage.value) || 1)));
  jumpPage.value = value;
  emit("update:page", value);
}
</script>

<template>
  <div class="pager">
    <button class="pager-btn" type="button" :disabled="page <= 1" @click="emit('update:page', page - 1)">上一页</button>
    <span class="pager-info">第 {{ total ? page : 0 }} / {{ pages }} 页 · 共 {{ total }} 条</span>
    <label class="pager-jump">
      <span>跳至</span>
      <input v-model.number="jumpPage" type="number" min="1" :max="pages" aria-label="跳转到页码" @keyup.enter="jump" />
      <span>页</span>
      <button class="pager-btn" type="button" @click="jump">确定</button>
    </label>
    <button class="pager-btn" type="button" :disabled="page >= pages" @click="emit('update:page', page + 1)">下一页</button>
  </div>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 12px 0 4px;
}

.pager-btn {
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--glass-bg);
  color: var(--text-secondary);
  font-size: 0.78rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.pager-btn:hover:not(:disabled) {
  background: var(--glass-bg-hover);
  color: var(--text-primary);
}

.pager-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.pager-info {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.pager-jump {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.pager-jump input {
  width: 58px;
  padding: 6px 7px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--glass-bg);
  color: var(--text-primary);
  text-align: center;
  font-size: 0.78rem;
}
</style>
