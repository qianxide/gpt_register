<script setup>
import { ref, watch } from "vue";

const props = defineProps({ page: Number, pages: Number, total: Number });
defineEmits(["update:page"]);
const jumpPage = ref(Math.max(1, Number(props.page) || 1));

watch(() => props.page, (value) => {
  jumpPage.value = Math.max(1, Number(value) || 1);
});

function jump(emit) {
  const pages = Math.max(1, Number(props.pages) || 1);
  const value = Math.max(1, Math.min(pages, Math.floor(Number(jumpPage.value) || 1)));
  jumpPage.value = value;
  emit("update:page", value);
}
</script>

<template>
  <div class="vue-pager">
    <button class="btn secondary" :disabled="page <= 1" @click="$emit('update:page', page - 1)">上一页</button>
    <span>第 {{ total ? page : 0 }} / {{ pages }} 页 · 共 {{ total }} 条</span>
    <label class="pager-jump">
      <span>跳至</span>
      <input v-model.number="jumpPage" type="number" min="1" :max="pages" aria-label="跳转到页码" @keyup.enter="jump($emit)" />
      <span>页</span>
      <button class="btn secondary" type="button" @click="jump($emit)">确定</button>
    </label>
    <button class="btn secondary" :disabled="page >= pages" @click="$emit('update:page', page + 1)">下一页</button>
  </div>
</template>
