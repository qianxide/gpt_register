<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  disabled: Boolean,
});
const emit = defineEmits(["append"]);

const rows = ref([]);
const selected = ref(new Map());
const groups = ref([]);
const groupId = ref("");
const emailSuffix = ref("");
const loading = ref(false);
const groupLoading = ref(false);
const error = ref("");
const groupError = ref("");
const query = ref("");
const offset = ref(0);
const total = ref(0);
const selectedGroupRows = ref([]);
const groupSelectionTotal = ref(0);
const groupSelectBusy = ref(false);
const pageSize = ref(10);
const selectionQuantity = ref(100);
const MAX_SELECTION = 10000;
let generation = 0;

const filteredRows = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return rows.value;
  return rows.value.filter((row) => [row.email, row.account_id, row.group_name]
    .some((value) => String(value || "").toLowerCase().includes(needle)));
});
const selectedCount = computed(() => selected.value.size);
const activeGroup = computed(() => groups.value.find((item) => String(item.id) === groupId.value) || null);
const groupAccountCount = computed(() => Number(activeGroup.value?.account_count ?? 0));
const groupAllSelected = computed(() => {
  const keys = selectedGroupRows.value.map(rowKey).filter(Boolean);
  return groupSelectionTotal.value > 0
    && keys.length === groupSelectionTotal.value
    && keys.every((key) => selected.value.has(key));
});
const pageKeys = computed(() => filteredRows.value.map(rowKey).filter(Boolean));
const pageAllSelected = computed(() => pageKeys.value.length > 0 && pageKeys.value.every((key) => selected.value.has(key)));
const pageStart = computed(() => total.value ? offset.value + 1 : 0);
const pageEnd = computed(() => Math.min(offset.value + pageSize.value, total.value));
const canPrevious = computed(() => offset.value > 0 && !loading.value);
const canNext = computed(() => offset.value + pageSize.value < total.value && !loading.value);

async function loadPage(nextOffset = offset.value) {
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams({
      limit: String(pageSize.value),
      offset: String(Math.max(0, nextOffset)),
    });
    if (groupId.value) params.set("group_id", groupId.value);
    if (emailSuffix.value) params.set("email_suffix", emailSuffix.value);
    const value = await api(`/api/accounts?${params.toString()}`);
    if (current !== generation) return;
    rows.value = Array.isArray(value?.items) ? value.items : [];
    total.value = Number(value?.total ?? value?.count ?? 0);
    offset.value = Math.max(0, nextOffset);
  } catch (reason) {
    if (current === generation) error.value = reason instanceof Error ? reason.message : "账号库读取失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}

async function loadGroups() {
  groupLoading.value = true;
  groupError.value = "";
  try {
    const value = await api("/api/account-groups");
    const next = Array.isArray(value?.items) ? value.items.filter((item) => item && item.id) : [];
    groups.value = next;
    if (groupId.value && !next.some((item) => String(item.id) === groupId.value)) {
      groupId.value = "";
    }
  } catch (reason) {
    groupError.value = reason instanceof Error ? reason.message : "分组读取失败";
  } finally {
    groupLoading.value = false;
  }
}

function clearSelection() {
  selected.value = new Map();
  selectedGroupRows.value = [];
  groupSelectionTotal.value = 0;
}

function changeGroup() {
  clearSelection();
  query.value = "";
  void loadPage(0);
}

function changeEmailSuffix() {
  clearSelection();
  offset.value = 0;
  void loadPage(0);
}

function changePageSize() {
  offset.value = 0;
  void loadPage(0);
}

function refreshPicker() {
  void Promise.all([loadGroups(), loadPage(offset.value)]);
}

function rowKey(row) {
  return String(row?.delete_key || row?.account_id || row?.email || "");
}

async function loadGroupRows() {
  if (props.disabled || !groupId.value || groupSelectBusy.value) return [];
  groupSelectBusy.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams({
      limit: String(MAX_SELECTION),
      offset: "0",
      group_id: groupId.value,
    });
    if (emailSuffix.value) params.set("email_suffix", emailSuffix.value);
    const value = await api(`/api/accounts?${params.toString()}`);
    const count = Number(value?.total ?? value?.count ?? 0);
    if (count > MAX_SELECTION) {
      throw new Error(`当前分组有 ${count} 个账号，单批最多选择 ${MAX_SELECTION} 个`);
    }
    const nextRows = Array.isArray(value?.items) ? value.items : [];
    const next = new Map(selected.value);
    for (const row of nextRows) {
      const key = rowKey(row);
      if (key) next.set(key, row);
    }
    selectedGroupRows.value = nextRows;
    groupSelectionTotal.value = count;
    selected.value = next;
    return nextRows;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "分组账号读取失败";
    selectedGroupRows.value = [];
    return [];
  } finally {
    groupSelectBusy.value = false;
  }
}

async function selectQuantity() {
  if (props.disabled || !groupId.value || groupSelectBusy.value) return;
  const requested = Math.min(MAX_SELECTION, Math.max(1, Math.floor(Number(selectionQuantity.value) || 1)));
  selectionQuantity.value = requested;
  groupSelectBusy.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams({
      limit: String(requested),
      offset: "0",
      group_id: groupId.value,
    });
    if (emailSuffix.value) params.set("email_suffix", emailSuffix.value);
    const value = await api(`/api/accounts?${params.toString()}`);
    const count = Number(value?.total ?? value?.count ?? 0);
    const nextRows = (Array.isArray(value?.items) ? value.items : []).filter((row) => rowKey(row));
    if (!nextRows.length) {
      error.value = "当前分组和邮箱后缀没有可选邮箱";
      return;
    }
    const currentGroupKeys = new Set(selectedGroupRows.value.map(rowKey).filter(Boolean));
    const next = new Map(selected.value);
    currentGroupKeys.forEach((key) => next.delete(key));
    nextRows.forEach((row) => next.set(rowKey(row), row));
    selectedGroupRows.value = nextRows;
    groupSelectionTotal.value = count;
    selected.value = next;
    if (count < requested) error.value = `当前筛选只有 ${count} 个邮箱，已全部选中`;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "按数量选择邮箱失败";
  } finally {
    groupSelectBusy.value = false;
  }
}

async function toggleGroupSelection() {
  if (props.disabled || !groupId.value || groupSelectBusy.value) return;
  const keys = selectedGroupRows.value.map(rowKey).filter(Boolean);
  if (groupAllSelected.value && keys.length) {
    const next = new Map(selected.value);
    keys.forEach((key) => next.delete(key));
    selected.value = next;
    selectedGroupRows.value = [];
    return;
  }
  await loadGroupRows();
}

function togglePageSelection() {
  if (props.disabled || loading.value) return;
  const pageRows = filteredRows.value.filter((row) => rowKey(row));
  if (!pageRows.length) return;
  const next = new Map(selected.value);
  if (pageAllSelected.value) {
    pageRows.forEach((row) => next.delete(rowKey(row)));
    selected.value = next;
    return;
  }
  const additions = pageRows.filter((row) => !next.has(rowKey(row)));
  if (next.size + additions.length > MAX_SELECTION) {
    error.value = "单批最多选择 " + MAX_SELECTION + " 个账号";
    return;
  }
  additions.forEach((row) => next.set(rowKey(row), row));
  selected.value = next;
}

function toggle(row) {
  if (props.disabled) return;
  const key = rowKey(row);
  if (!key) return;
  const next = new Map(selected.value);
  if (next.has(key)) next.delete(key);
  else if (next.size < MAX_SELECTION) next.set(key, row);
  else {
    error.value = `单批最多选择 ${MAX_SELECTION} 个账号`;
    return;
  }
  selected.value = next;
}

function appendSelected() {
  if (props.disabled || !selected.value.size) return;
  const values = [...selected.value.values()]
    .map((row) => String(row.email || "").trim().toLowerCase())
    .filter(Boolean);
  if (values.length) emit("append", values);
  clearSelection();
}

function previousPage() {
  if (canPrevious.value) void loadPage(offset.value - pageSize.value);
}

function nextPage() {
  if (canNext.value) void loadPage(offset.value + pageSize.value);
}

watch(() => props.disabled, (value) => {
  if (value) clearSelection();
});
onMounted(() => {
  void loadGroups();
  void loadPage(0);
});
</script>

<template>
  <details class="rebind-account-picker">
    <summary>从账号库选择当前账号</summary>
    <div class="rebind-picker-toolbar">
      <label class="rebind-picker-group">
        <span>账号分组</span>
        <select v-model="groupId" :disabled="props.disabled || loading || groupLoading" @change="changeGroup">
          <option value="">全部分组</option>
          <option v-for="group in groups" :key="group.id" :value="group.id">
            {{ group.name || "未命名分组" }}（{{ group.account_count ?? 0 }}）
          </option>
        </select>
      </label>
      <label class="rebind-picker-page-size">
        <span>每页</span>
        <select v-model.number="pageSize" :disabled="props.disabled || loading || groupLoading" @change="changePageSize">
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
      <label class="rebind-picker-group">
        <span>邮箱后缀</span>
        <select v-model="emailSuffix" :disabled="props.disabled || loading || groupLoading" @change="changeEmailSuffix">
          <option value="">全部后缀</option>
          <option value="dr.com">dr.com</option>
          <option value="icloud.com">icloud.com</option>
        </select>
      </label>
      <input v-model="query" type="search" autocomplete="off" placeholder="筛选当前页邮箱" :disabled="props.disabled || loading" />
      <button type="button" :disabled="props.disabled || loading || groupLoading" @click="refreshPicker">{{ loading || groupLoading ? "读取中…" : "刷新" }}</button>
    </div>
    <div class="rebind-picker-selectbar">
      <button
        class="rebind-picker-select-all"
        type="button"
        :disabled="props.disabled || loading || !pageKeys.length"
        @click="togglePageSelection"
      >
        {{ pageAllSelected ? "取消全选本页" : "全选本页" }}（{{ pageKeys.length }}）
      </button>
      <button
        class="rebind-picker-select-all"
        type="button"
        :disabled="props.disabled || !groupId || !groupAccountCount || loading || groupSelectBusy"
        @click="toggleGroupSelection"
      >
        {{ groupSelectBusy ? "读取分组中…" : (groupAllSelected ? "取消全选此分组" : "全选此分组") }}
        <span v-if="groupId">（{{ groupAccountCount }}）</span>
      </button>
      <label class="rebind-picker-quantity">
        <span>数量</span>
        <input v-model.number="selectionQuantity" type="number" min="1" :max="MAX_SELECTION" inputmode="numeric" :disabled="props.disabled || loading || groupLoading || groupSelectBusy" />
      </label>
      <button
        class="rebind-picker-select-all"
        type="button"
        :disabled="props.disabled || !groupId || loading || groupSelectBusy"
        @click="selectQuantity"
      >
        {{ groupSelectBusy ? "选择中…" : "按数量选中" }}
      </button>
      <span v-if="groupId" class="rebind-picker-select-hint">全选后可一次加入当前输入</span>
    </div>
    <div v-if="groupError" class="rebind-picker-error">{{ groupError }}</div>
    <div v-if="error" class="rebind-picker-error">{{ error }}</div>
    <div v-else-if="!loading && !filteredRows.length" class="rebind-picker-empty">当前页没有可选账号</div>
    <div v-else class="rebind-picker-list">
      <label v-for="row in filteredRows" :key="rowKey(row)" class="rebind-picker-row">
        <input type="checkbox" :checked="selected.has(rowKey(row))" :disabled="props.disabled" @change="toggle(row)" />
        <span>
          <strong>{{ row.email || "-" }}</strong>
          <small>{{ row.group_name || "默认分组" }}<template v-if="row.account_id"> · {{ row.account_id }}</template></small>
        </span>
      </label>
    </div>
    <div class="rebind-picker-footer">
      <button type="button" :disabled="props.disabled || !selectedCount" @click="appendSelected">加入当前输入（{{ selectedCount }}）</button>
      <span>{{ pageStart }}-{{ pageEnd }} / {{ total }}</span>
      <button type="button" :disabled="!canPrevious" @click="previousPage">上一页</button>
      <button type="button" :disabled="!canNext" @click="nextPage">下一页</button>
    </div>
  </details>
</template>

<style scoped>
.rebind-account-picker { margin-top: 4px; border: 1px solid var(--line, #d6dee9); border-radius: 8px; padding: 8px 10px; font-size: 12px; }
.rebind-account-picker[open] { min-height: 320px; }
.rebind-account-picker summary { cursor: pointer; color: var(--text-secondary, #64748b); }
.rebind-picker-toolbar, .rebind-picker-footer { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.rebind-picker-selectbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.rebind-picker-select-all { box-sizing: border-box; border: 1px solid var(--line, #d6dee9); border-radius: 6px; padding: 6px 9px; background: var(--panel, #fff); color: inherit; font: inherit; cursor: pointer; }
.rebind-picker-quantity { display: inline-flex; align-items: center; gap: 5px; color: var(--text-muted, #64748b); white-space: nowrap; }
.rebind-picker-quantity input { width: 76px; box-sizing: border-box; border: 1px solid var(--line, #d6dee9); border-radius: 6px; padding: 6px 8px; background: var(--panel, #fff); color: inherit; font: inherit; }
.rebind-picker-select-hint { color: var(--text-muted, #64748b); }
.rebind-picker-group { display: flex; align-items: center; gap: 5px; flex: 1 1 180px; min-width: 0; }
.rebind-picker-group span { color: var(--text-muted, #64748b); white-space: nowrap; }
.rebind-picker-group select { flex: 1; min-width: 0; }
.rebind-picker-page-size { display: flex; align-items: center; gap: 5px; white-space: nowrap; color: var(--text-muted, #64748b); }
.rebind-picker-page-size select { box-sizing: border-box; border: 1px solid var(--line, #d6dee9); border-radius: 6px; padding: 6px 8px; background: var(--panel, #fff); color: inherit; font: inherit; }
.rebind-picker-toolbar input { flex: 1 1 170px; min-width: 0; }
.rebind-picker-toolbar input, .rebind-picker-toolbar select, .rebind-picker-toolbar button, .rebind-picker-footer button { box-sizing: border-box; border: 1px solid var(--line, #d6dee9); border-radius: 6px; padding: 6px 8px; background: var(--panel, #fff); color: inherit; font: inherit; }
.rebind-picker-toolbar button, .rebind-picker-footer button { cursor: pointer; }
.rebind-picker-list { min-height: 220px; max-height: 340px; overflow: auto; margin-top: 8px; border-top: 1px solid var(--line, #d6dee9); }
.rebind-picker-row { display: flex; align-items: flex-start; gap: 8px; padding: 7px 2px; border-bottom: 1px solid var(--line-subtle, #edf1f5); cursor: pointer; }
.rebind-picker-row span { min-width: 0; display: grid; gap: 2px; }
.rebind-picker-row strong { overflow-wrap: anywhere; }
.rebind-picker-row small, .rebind-picker-footer span { color: var(--text-muted, #64748b); }
.rebind-picker-footer { justify-content: flex-end; }
.rebind-picker-footer span { margin-right: auto; }
.rebind-picker-error { color: var(--error, #dc2626); margin-top: 8px; }
.rebind-picker-empty { color: var(--text-muted, #64748b); padding: 12px 2px 4px; }
.rebind-picker-toolbar :disabled, .rebind-picker-selectbar :disabled, .rebind-picker-footer :disabled { opacity: .55; cursor: not-allowed; }
</style>
