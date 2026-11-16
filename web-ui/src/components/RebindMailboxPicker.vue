<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  providerType: { type: String, default: "auto" },
  disabled: Boolean,
});
const emit = defineEmits(["selection-change"]);

const items = ref([]);
const selected = ref(new Set());
const providerRef = ref("");
const query = ref("");
const loading = ref(false);
const error = ref("");
const total = ref(0);
const offset = ref(0);
const PAGE_SIZE = 50;
let generation = 0;

const providerName = computed(() => ({
  maddy_webui: "Maddy",
  fastmail_api: "Fastmail",
  mailcom_api: "mail.com",
  icloud_api: "iCloud",
  outlook_token: "Outlook",
}[props.providerType] || "邮箱"));
const pageStart = computed(() => total.value ? offset.value + 1 : 0);
const pageEnd = computed(() => Math.min(offset.value + PAGE_SIZE, total.value));
const canPrevious = computed(() => offset.value > 0 && !loading.value);
const canNext = computed(() => offset.value + PAGE_SIZE < total.value && !loading.value);

function emitSelection() {
  emit("selection-change", {
    emails: [...selected.value],
    provider_ref: providerRef.value,
  });
}

async function loadPage(nextOffset = 0) {
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams({
      provider_type: props.providerType || "auto",
      q: query.value.trim(),
      offset: String(Math.max(0, nextOffset)),
      limit: String(PAGE_SIZE),
    });
    const value = await api(`/api/accounts/rebind/targets?${params.toString()}`);
    if (current !== generation) return;
    items.value = Array.isArray(value?.items) ? value.items : [];
    total.value = Number(value?.count || 0);
    offset.value = Math.max(0, nextOffset);
  } catch (reason) {
    if (current === generation) error.value = reason instanceof Error ? reason.message : "邮箱池读取失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}

function toggle(item) {
  if (props.disabled) return;
  const email = String(item?.email || "").trim().toLowerCase();
  const ref = String(item?.provider_ref || "");
  if (!email || !ref) return;
  const next = new Set(selected.value);
  if (next.has(email)) {
    next.delete(email);
    if (!next.size) providerRef.value = "";
  } else {
    if (providerRef.value && providerRef.value !== ref) next.clear();
    if (next.size >= 500) return;
    providerRef.value = ref;
    next.add(email);
  }
  selected.value = next;
  emitSelection();
}

function resetSelection() {
  selected.value = new Set();
  providerRef.value = "";
  offset.value = 0;
  total.value = 0;
  items.value = [];
  emitSelection();
}

function previousPage() {
  if (canPrevious.value) void loadPage(offset.value - PAGE_SIZE);
}

function nextPage() {
  if (canNext.value) void loadPage(offset.value + PAGE_SIZE);
}

watch(() => props.providerType, () => {
  generation += 1;
  resetSelection();
  if (!props.disabled) void loadPage(0);
});
onMounted(() => loadPage(0));
</script>

<template>
  <div class="rebind-mailbox-picker">
    <div class="rebind-mailbox-picker-head">
      <span>当前 {{ providerName }} 邮箱库</span>
      <button type="button" :disabled="props.disabled || loading" @click="loadPage(offset)">{{ loading ? "读取中…" : "刷新" }}</button>
    </div>
    <div class="rebind-mailbox-picker-search">
      <input v-model="query" type="search" autocomplete="off" placeholder="筛选邮箱" :disabled="props.disabled || loading" @keydown.enter.prevent="loadPage(0)" />
      <button type="button" :disabled="props.disabled || loading" @click="loadPage(0)">查询</button>
    </div>
    <div v-if="error" class="rebind-picker-error">{{ error }}</div>
    <div v-else-if="!loading && !items.length" class="rebind-picker-empty">当前没有可选邮箱。mail.com 别名在首次自动匹配成功后会出现在这里。</div>
    <div v-else class="rebind-mailbox-list">
      <label v-for="item in items" :key="`${item.provider_ref}:${item.email}`" class="rebind-mailbox-row">
        <input type="checkbox" :checked="selected.has(item.email)" :disabled="props.disabled" @change="toggle(item)" />
        <span>
          <strong>{{ item.email }}</strong>
          <small>{{ item.label || providerName }} · {{ item.state || "available" }}</small>
        </span>
      </label>
    </div>
    <div class="rebind-picker-footer">
      <span>已选 {{ selected.size }} 个 · {{ pageStart }}-{{ pageEnd }} / {{ total }}</span>
      <button type="button" :disabled="!canPrevious" @click="previousPage">上一页</button>
      <button type="button" :disabled="!canNext" @click="nextPage">下一页</button>
    </div>
  </div>
</template>

<style scoped>
.rebind-mailbox-picker { display: grid; gap: 8px; min-width: 0; padding: 10px; border: 1px solid var(--line, #d6dee9); border-radius: 8px; background: var(--panel-soft, rgba(248, 250, 252, .7)); font-size: 12px; }
.rebind-mailbox-picker-head, .rebind-mailbox-picker-search, .rebind-picker-footer { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.rebind-mailbox-picker-head { justify-content: space-between; color: var(--text-secondary, #475569); }
.rebind-mailbox-picker input[type="search"] { flex: 1 1 180px; min-width: 0; }
.rebind-mailbox-picker input[type="search"], .rebind-mailbox-picker button { box-sizing: border-box; border: 1px solid var(--line, #d6dee9); border-radius: 6px; padding: 6px 8px; background: var(--panel, #fff); color: inherit; font: inherit; }
.rebind-mailbox-picker button { cursor: pointer; }
.rebind-mailbox-list { max-height: 190px; overflow: auto; border-top: 1px solid var(--line, #d6dee9); }
.rebind-mailbox-row { display: flex; align-items: flex-start; gap: 8px; padding: 7px 2px; border-bottom: 1px solid var(--line-subtle, #edf1f5); cursor: pointer; }
.rebind-mailbox-row span { min-width: 0; display: grid; gap: 2px; }
.rebind-mailbox-row strong { overflow-wrap: anywhere; }
.rebind-mailbox-row small, .rebind-picker-footer { color: var(--text-muted, #64748b); }
.rebind-picker-footer span { margin-right: auto; }
.rebind-picker-error { color: var(--error, #dc2626); }
.rebind-picker-empty { color: var(--text-muted, #64748b); line-height: 1.6; }
.rebind-mailbox-picker :disabled { opacity: .55; cursor: not-allowed; }
</style>
