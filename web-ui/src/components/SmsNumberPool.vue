<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId } from "vue";
import { api, jsonBody } from "../api.js";
import type { SmsConfig, SmsPoolDeleteBody, SmsPoolDeleteResult, SmsPoolImportResult, SmsPoolItem, SmsPoolPage, SmsPoolStatus, SmsPoolSummary } from "../new/api/ops";

// This view has no router or store dependency, so both UIs can mount it directly.
defineProps({ settingsHref: { type: String, default: "#operations" } });
const uid = useId();
const items = ref<SmsPoolItem[]>([]);
const summary = ref<SmsPoolSummary>({ total: 0, available: 0, reserved: 0, cooling: 0, exhausted: 0 });
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const pageInput = ref(1);
const status = ref<SmsPoolStatus | "all">("all");
const queryInput = ref("");
const query = ref("");
const selected = ref<number[]>([]);
const loading = ref(false);
const listError = ref("");
const actionError = ref("");
const notice = ref("");
const text = ref("");
const readingFile = ref(false);
const mutation = ref<"" | "import" | "delete" | "activate">("");
const activated = ref<boolean | null>(null);
const configLoading = ref(false);
const configError = ref("");
const importResult = ref<SmsPoolImportResult | null>(null);
const pendingDelete = ref<SmsPoolDeleteBody | null>(null);
const confirmation = ref<HTMLElement | null>(null);
const now = ref(Date.now() / 1000);
const loadedAt = ref(now.value);
const labels: Record<SmsPoolStatus, string> = { available: "可用", reserved: "使用中", cooling: "冷却中", exhausted: "已用满" };
const placeholder = "2026-09-17|+12025550101|https://168smsapi.com/sms/openai-/demo-01\n2026-09-18|+12025550102|[取码链接](https://168smsapi.com/sms/openai-/demo-02)";
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const controlsDisabled = computed(() => loading.value || !!mutation.value || readingFile.value || !!pendingDelete.value);
const allSelected = computed(() => items.value.length > 0 && items.value.every((item) => selected.value.includes(item.id)));
const someSelected = computed(() => selected.value.length > 0 && !allSelected.value);
let requestId = 0;
let controller: AbortController | undefined;
let configRequestId = 0;
let configController: AbortController | undefined;
let clock: ReturnType<typeof setInterval> | undefined;
let returnFocus: HTMLElement | null = null;
let disposed = false;

async function loadConfig() {
  if (disposed || mutation.value === "activate") return;
  const id = ++configRequestId;
  configController?.abort();
  configController = new AbortController();
  configLoading.value = true;
  configError.value = "";
  try {
    const result = await api("/api/sms-config", { signal: configController.signal }) as SmsConfig;
    if (id !== configRequestId || disposed) return;
    activated.value = result.provider === "imported_pool" && !!result.enabled;
  } catch (cause) {
    if (id !== configRequestId || disposed) return;
    activated.value = null;
    configError.value = cause instanceof Error ? cause.message : "读取接码设置失败";
  } finally {
    if (id === configRequestId && !disposed) configLoading.value = false;
  }
}

async function load() {
  if (disposed) return;
  // An unavailable or slow config endpoint must never block the number list.
  void loadConfig();
  const id = ++requestId;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  listError.value = "";
  selected.value = [];
  try {
    const search = new URLSearchParams({ page: String(page.value), page_size: String(pageSize.value), status: status.value, q: query.value });
    const result = await api(`/api/sms-pool?${search}`, { signal: controller.signal }) as SmsPoolPage;
    if (id !== requestId || disposed) return;
    summary.value = result.summary;
    total.value = result.total;
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize.value));
    // Deleting the final row of the final page should return to a real page.
    if (page.value > lastPage) {
      page.value = lastPage;
      pageInput.value = lastPage;
      await load();
      return;
    }
    items.value = result.items;
    page.value = result.page;
    pageInput.value = result.page;
    loadedAt.value = now.value = Date.now() / 1000;
  } catch (cause) {
    if (id !== requestId || disposed) return;
    items.value = [];
    listError.value = cause instanceof Error ? cause.message : "号码池加载失败，请重试";
  } finally {
    if (id === requestId && !disposed) loading.value = false;
  }
}

async function applyFilters() {
  if (controlsDisabled.value) return;
  query.value = queryInput.value.trim();
  page.value = pageInput.value = 1;
  await load();
}

async function goToPage(target = pageInput.value) {
  if (controlsDisabled.value) return;
  const value = Number(target);
  if (!Number.isInteger(value) || value < 1 || value > pageCount.value) {
    pageInput.value = page.value;
    return;
  }
  page.value = pageInput.value = value;
  await load();
}

function selectPage(checked: boolean) {
  if (controlsDisabled.value || listError.value) return;
  selected.value = checked ? items.value.map((item) => item.id) : [];
}

async function readFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file || controlsDisabled.value) return;
  readingFile.value = true;
  actionError.value = "";
  notice.value = "";
  try {
    const content = await file.text();
    if (disposed) return;
    text.value = content;
    importResult.value = null;
    notice.value = "文件已读入，请检查内容后点击导入。";
  } catch {
    if (!disposed) actionError.value = "文本文件读取失败，请重新选择或直接粘贴内容。";
  } finally {
    readingFile.value = false;
    input.value = "";
  }
}

async function importNumbers() {
  if (controlsDisabled.value || !text.value.trim()) return;
  mutation.value = "import";
  actionError.value = "";
  notice.value = "";
  importResult.value = null;
  try {
    // Preserve markdown links and line numbers; parsing belongs to the server.
    const result = await api("/api/sms-pool/import", { method: "POST", body: jsonBody({ text: text.value }) }) as SmsPoolImportResult;
    if (disposed) return;
    importResult.value = result;
    summary.value = result.summary;
    notice.value = `导入 ${result.imported} 个，重复 ${result.duplicates} 个，无效 ${result.invalid} 行。`;
    if (!result.invalid) text.value = "";
    page.value = pageInput.value = 1;
    await load();
  } catch (cause) {
    if (!disposed) actionError.value = cause instanceof Error ? cause.message : "号码导入失败，请重试";
  } finally {
    mutation.value = "";
  }
}

async function activatePool() {
  if (controlsDisabled.value || activated.value) return;
  // A GET started before this write must not restore the previous provider.
  ++configRequestId;
  configController?.abort();
  configLoading.value = false;
  configError.value = "";
  mutation.value = "activate";
  actionError.value = "";
  try {
    await api("/api/sms-config", { method: "PUT", body: jsonBody({ provider: "imported_pool", enabled: true }) });
    if (!disposed) activated.value = true;
  } catch (cause) {
    if (!disposed) actionError.value = cause instanceof Error ? cause.message : "号码池启用失败，请重试";
  } finally {
    mutation.value = "";
  }
}

async function requestDelete(exhaustedOnly: boolean, event?: Event) {
  if (controlsDisabled.value || listError.value) return;
  const ids = items.value.filter((item) => selected.value.includes(item.id)).map((item) => item.id);
  if (exhaustedOnly ? !summary.value.exhausted : !ids.length) return;
  // Keep a snapshot: confirmation always describes the exact request to send.
  pendingDelete.value = exhaustedOnly ? { exhausted_only: true } : { ids };
  actionError.value = "";
  notice.value = "";
  returnFocus = (event?.currentTarget as HTMLElement) || null;
  await nextTick();
  confirmation.value?.focus();
}

async function cancelDelete() {
  if (mutation.value) return;
  pendingDelete.value = null;
  actionError.value = "";
  await nextTick();
  returnFocus?.focus();
}

async function confirmDelete() {
  if (!pendingDelete.value || mutation.value) return;
  mutation.value = "delete";
  actionError.value = "";
  try {
    const result = await api("/api/sms-pool", { method: "DELETE", body: jsonBody(pendingDelete.value) }) as SmsPoolDeleteResult;
    if (disposed) return;
    pendingDelete.value = null;
    selected.value = [];
    summary.value = result.summary;
    notice.value = `已删除 ${result.deleted} 个，跳过 ${result.skipped} 个。`;
    await load();
  } catch (cause) {
    if (!disposed) actionError.value = cause instanceof Error ? cause.message : "删除失败，请重试";
  } finally {
    mutation.value = "";
    if (!pendingDelete.value && !disposed) {
      await nextTick();
      returnFocus?.focus();
    }
  }
}

function cooldownLabel(item: SmsPoolItem) {
  if (item.status !== "cooling") return "—";
  const seconds = Math.max(0, Math.ceil(item.cooldown_until > 0 ? item.cooldown_until - now.value : item.cooldown_remaining - (now.value - loadedAt.value)));
  if (!seconds) return "已到期，请刷新";
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function lastUsed(value: number) {
  return value ? new Date(value * 1000).toLocaleString() : "尚未使用";
}

onMounted(() => {
  void load();
  clock = setInterval(() => { now.value = Date.now() / 1000; }, 1000);
});
onBeforeUnmount(() => {
  disposed = true;
  ++requestId;
  controller?.abort();
  ++configRequestId;
  configController?.abort();
  if (clock) clearInterval(clock);
});
</script>

<template>
  <section class="sms-number-pool" :aria-labelledby="`${uid}-title`">
    <header class="pool-heading">
      <div><h2 :id="`${uid}-title`">号码池</h2><p>USA +1 · 导入号码及取码链接，统一管理使用状态</p></div>
      <div class="pool-heading-actions">
        <button class="pool-button pool-primary" type="button" :disabled="controlsDisabled || activated" @click="activatePool">{{ mutation === 'activate' ? '启用中…' : activated ? '已启用号码池接码' : '使用号码池接码' }}</button>
        <button class="pool-button" type="button" :disabled="controlsDisabled" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
      </div>
    </header>
    <p class="pool-activation" role="status"><span v-if="configLoading">正在读取已保存的接码启用状态…</span><span v-else-if="activated">已启用导入号码池接码。</span><span v-else><template v-if="activated === false">号码池接码尚未启用。</template>在运维接码设置选择“导入号码池”并启用后生效，也可点击上方按钮启用。</span> <a :href="settingsHref">前往运维接码设置 →</a></p>
    <p v-if="configError" class="pool-error" role="alert">启用状态读取失败：{{ configError }} <button class="pool-button" type="button" :disabled="controlsDisabled || configLoading" @click="loadConfig">重试读取设置</button></p>
    <p class="pool-rule">每号最多成功 3 次，手机验证成功后使用次数 +1；失败不扣次数。每次使用后冷却 1 小时，失败也冷却 1 小时，避免短时间重复使用。</p>

    <details class="pool-import">
      <summary>导入号码 <span>粘贴文本或选择文本文件</span></summary>
      <div class="pool-import-body">
        <label :for="`${uid}-text`">每行：日期|号码|取码URL（支持 Markdown 链接）</label>
        <textarea :id="`${uid}-text`" v-model="text" :placeholder="placeholder" :disabled="controlsDisabled" rows="4" autocomplete="off" spellcheck="false"></textarea>
        <div class="pool-import-actions">
          <label class="pool-file">{{ readingFile ? '读取中…' : '选择文本文件' }}<input type="file" accept=".txt,.md,.csv,.tsv,text/plain,text/markdown" :disabled="controlsDisabled" @change="readFile" /></label>
          <button class="pool-button pool-primary" type="button" :disabled="controlsDisabled || !text.trim()" @click="importNumbers">{{ mutation === 'import' ? '导入中…' : '导入号码' }}</button>
          <small>文件只读入输入框，点击导入后提交。列表不展示取码链接。</small>
        </div>
      </div>
    </details>

    <p v-if="notice" class="pool-notice" role="status">{{ notice }}</p>
    <details v-if="importResult?.errors.length" class="pool-import-errors" open>
      <summary>无效 {{ importResult.invalid }} 行，显示前 {{ importResult.errors.length }} 条原因；修正后可重新导入</summary>
      <ul><li v-for="(item, index) in importResult.errors" :key="index">第 {{ item.line }} 行：{{ item.error }}</li></ul>
    </details>
    <p v-if="actionError" class="pool-error" role="alert">{{ actionError }}</p>

    <div class="pool-summary" aria-label="全部号码统计">
      <span>全部 <b>{{ summary.total }}</b></span>
      <span v-for="(label, key) in labels" :key="key">{{ label }} <b>{{ summary[key] }}</b></span>
    </div>
    <form class="pool-toolbar" @submit.prevent="applyFilters">
      <select v-model="status" aria-label="筛选号码状态" :disabled="controlsDisabled" @change="applyFilters">
        <option value="all">全部状态</option><option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option>
      </select>
      <input v-model="queryInput" type="search" aria-label="搜索号码" placeholder="搜索号码" :disabled="controlsDisabled" />
      <button class="pool-button" type="submit" :disabled="controlsDisabled">搜索</button>
      <div class="pool-delete-actions">
        <button class="pool-button" type="button" :disabled="controlsDisabled || !!listError || !selected.length" @click="requestDelete(false, $event)">删除选中{{ selected.length ? ` (${selected.length})` : '' }}</button>
        <button class="pool-button pool-danger" type="button" :disabled="controlsDisabled || !!listError || !summary.exhausted" @click="requestDelete(true, $event)">删除已用满</button>
      </div>
    </form>

    <div v-if="pendingDelete" ref="confirmation" class="pool-confirm" role="alertdialog" aria-modal="false" :aria-labelledby="`${uid}-confirm-title`" :aria-describedby="`${uid}-confirm-note`" tabindex="-1" @keydown.esc.prevent="cancelDelete">
      <div>
        <strong :id="`${uid}-confirm-title`">{{ pendingDelete.exhausted_only ? `删除全部已用满号码（当前 ${summary.exhausted} 个）？` : `删除选中的 ${pendingDelete.ids?.length} 个号码？` }}</strong>
        <p :id="`${uid}-confirm-note`">{{ pendingDelete.exhausted_only ? '覆盖所有分页与筛选范围，只删除提交时已用满的号码。' : '仅删除本次选中的号码。' }}删除后从号码池移除，请确认后继续。</p>
      </div>
      <div class="pool-confirm-actions"><button class="pool-button" type="button" :disabled="!!mutation" @click="cancelDelete">取消</button><button class="pool-button pool-danger" type="button" :disabled="!!mutation" @click="confirmDelete">{{ mutation === 'delete' ? '删除中…' : '确认删除' }}</button></div>
    </div>

    <p v-if="listError" class="pool-error" role="alert">{{ listError }} <button class="pool-button" type="button" :disabled="controlsDisabled" @click="load">重新加载</button></p>
    <div class="pool-table-wrap" :aria-busy="loading">
      <table class="pool-table">
        <caption class="pool-sr-only">号码池列表；全选仅作用于当前页</caption>
        <thead><tr>
          <th class="pool-check"><input type="checkbox" aria-label="全选本页" :checked="allSelected" :indeterminate="someSelected" :disabled="controlsDisabled || !items.length" @change="selectPage(($event.target as HTMLInputElement).checked)" /></th>
          <th scope="col">号码 / 国家</th><th scope="col">来源日期</th><th scope="col">成功次数</th><th scope="col">状态</th><th scope="col">冷却剩余</th><th scope="col">最近使用</th>
        </tr></thead>
        <tbody>
          <tr v-if="loading"><td colspan="7" class="pool-empty" role="status">正在加载号码…</td></tr>
          <tr v-else-if="!items.length"><td colspan="7" class="pool-empty">{{ listError ? '列表加载失败，请重试' : status !== 'all' || query ? '没有匹配的号码，请调整筛选条件' : '暂无号码，展开上方“导入号码”开始添加' }}</td></tr>
          <tr v-for="item in loading ? [] : items" :key="item.id">
            <td class="pool-check"><input v-model="selected" type="checkbox" :value="item.id" :aria-label="`选择 ${item.phone}`" :disabled="controlsDisabled" /></td>
            <td><b class="pool-phone">{{ item.phone }}</b><small>USA +1</small></td>
            <td>{{ item.source_date || '—' }}</td>
            <td>{{ item.uses }} / {{ item.max_uses }}<small>剩余 {{ item.remaining_uses }} 次</small></td>
            <td><span :class="['pool-status', `pool-${item.status}`]">{{ labels[item.status] || item.status }}</span></td>
            <td class="pool-countdown">{{ cooldownLabel(item) }}</td>
            <td>{{ lastUsed(item.last_used_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="pool-pagination">
      <span>共 {{ total }} 个匹配号码 · 已选本页 {{ selected.length }} 个</span>
      <label>每页 <select v-model.number="pageSize" :disabled="controlsDisabled" @change="applyFilters"><option v-for="size in [10, 20, 50, 100]" :key="size" :value="size">{{ size }}</option></select> 个</label>
      <div class="pool-page-controls">
        <button class="pool-button" type="button" :disabled="controlsDisabled || page <= 1" @click="goToPage(page - 1)">上一页</button>
        <label>第 <input v-model.number="pageInput" type="number" min="1" :max="pageCount" aria-label="跳转页码" :disabled="controlsDisabled" @change="goToPage()" @keydown.enter.prevent="goToPage()" /> / {{ pageCount }} 页</label>
        <button class="pool-button" type="button" :disabled="controlsDisabled || page >= pageCount" @click="goToPage(page + 1)">下一页</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.sms-number-pool {
  --pool-text: var(--text-primary, var(--text, #1e293b));
  --pool-muted: var(--text-secondary, var(--muted, #64748b));
  --pool-line: var(--field-border, var(--line, #d8dfeb));
  --pool-field: var(--field-bg, var(--soft, #f5f7fb));
  --pool-accent: var(--accent, var(--brand, #2563eb));
  --pool-danger: var(--error, var(--red, #dc2626));
  display: grid; gap: 14px; min-width: 0; padding: 20px;
  color: var(--pool-text); background: var(--glass-bg, var(--card, #fff));
  border: 1px solid var(--glass-border, var(--line, #d8dfeb)); border-radius: var(--radius-md, var(--r-lg, 14px));
  font-size: .82rem; line-height: 1.5;
}
.sms-number-pool p, .sms-number-pool h2 { margin: 0; }
.pool-heading, .pool-heading-actions, .pool-import-actions, .pool-toolbar, .pool-delete-actions, .pool-confirm, .pool-confirm-actions, .pool-pagination, .pool-page-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pool-heading, .pool-confirm { justify-content: space-between; }
.pool-heading h2 { font-size: 1rem; font-weight: 650; }
.pool-heading p, .pool-import-actions small, .pool-rule, .pool-pagination { color: var(--pool-muted); font-size: .76rem; }
.pool-activation { color: var(--pool-muted); font-size: .76rem; }.pool-activation a { color: var(--pool-accent); }
.pool-rule { border-left: 3px solid var(--pool-accent); padding: 8px 12px; background: var(--pool-field); border-radius: 4px; }
.pool-import { border: 1px solid var(--pool-line); border-radius: 8px; overflow: hidden; }
.pool-import summary { padding: 10px 12px; cursor: pointer; font-weight: 600; }
.pool-import summary span { margin-left: 8px; color: var(--pool-muted); font-size: .74rem; font-weight: 400; }
.pool-import-body { padding: 0 12px 12px; display: grid; gap: 8px; }
.sms-number-pool textarea, .sms-number-pool select, .sms-number-pool input:not([type="checkbox"]):not([type="file"]) { border: 1px solid var(--pool-line); border-radius: 7px; background: var(--pool-field); color: var(--pool-text); padding: 7px 9px; font: inherit; min-height: 36px; box-sizing: border-box; }
.sms-number-pool textarea { width: 100%; resize: vertical; font-family: var(--mono, monospace); font-size: .76rem; }
.sms-number-pool input[type="checkbox"] { width: 16px; height: 16px; margin: 0; accent-color: var(--pool-accent); vertical-align: middle; }
.sms-number-pool .pool-button { display: inline-flex; align-items: center; justify-content: center; min-height: 36px; padding: 6px 11px; width: auto; border: 1px solid var(--pool-line); border-radius: 7px; background: var(--pool-field); color: var(--pool-text); box-shadow: none; font: inherit; white-space: nowrap; cursor: pointer; }
.sms-number-pool .pool-button:hover:not(:disabled) { border-color: var(--pool-accent); }
.sms-number-pool .pool-primary { color: #fff; background: var(--pool-accent); border-color: var(--pool-accent); }
.sms-number-pool .pool-danger { color: var(--pool-danger); }
.sms-number-pool :disabled { opacity: .55; cursor: default; }
.sms-number-pool :is(button, input, select, textarea, summary):focus-visible, .pool-file:focus-within { outline: 2px solid var(--pool-accent); outline-offset: 2px; }
.pool-file { position: relative; padding: 6px 11px; min-height: 36px; display: inline-flex; align-items: center; border: 1px solid var(--pool-line); border-radius: 7px; overflow: hidden; }
.pool-file input { position: absolute; inset: 0; width: 100%; opacity: 0; cursor: pointer; }
.pool-import-errors { color: var(--pool-danger); font-size: .78rem; }
.pool-import-errors summary { cursor: pointer; }.pool-import-errors ul { max-height: 160px; overflow: auto; padding-left: 22px; overflow-wrap: anywhere; }
.pool-notice { color: var(--pool-accent); }.pool-error { color: var(--pool-danger); overflow-wrap: anywhere; }
.pool-summary { display: flex; flex-wrap: wrap; gap: 8px 20px; color: var(--pool-muted); }.pool-summary b { color: var(--pool-text); margin-left: 4px; font-variant-numeric: tabular-nums; }
.pool-toolbar > input { flex: 1; min-width: 140px; }.pool-delete-actions { margin-left: auto; }
.pool-toolbar > select, .pool-pagination select { width: auto; }
.pool-confirm { padding: 12px; background: var(--pool-field); border: 1px solid var(--pool-danger); border-radius: 8px; }.pool-confirm p { font-size: .76rem; color: var(--pool-muted); margin-top: 4px; }
.pool-table-wrap { overflow-x: auto; border: 1px solid var(--pool-line); border-radius: 8px; }
.pool-table { border-collapse: collapse; width: 100%; min-width: 740px; text-align: left; font-size: .78rem; }
.pool-table th, .pool-table td { padding: 10px 12px; border-bottom: 1px solid var(--pool-line); vertical-align: middle; white-space: nowrap; }
.pool-table th { color: var(--pool-muted); background: var(--pool-field); font-weight: 500; }.pool-table tbody tr:last-child td { border-bottom: 0; }
.pool-table .pool-check { width: 38px; text-align: center; }.pool-table small { display: block; color: var(--pool-muted); margin-top: 2px; font-size: .7rem; }
.pool-phone { font: 500 .8rem var(--mono, monospace); }.pool-countdown { font-variant-numeric: tabular-nums; }
.pool-status { padding: 2px 7px; border-radius: 5px; background: var(--pool-field); }.pool-available { color: var(--success, var(--ok)); }.pool-cooling { color: var(--warning, var(--warn)); }.pool-exhausted { color: var(--pool-muted); }.pool-reserved { color: var(--pool-accent); }
.pool-table .pool-empty { text-align: center; padding: 30px 12px; color: var(--pool-muted); }
.pool-pagination { justify-content: space-between; }.pool-pagination label { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }.pool-page-controls input { width: 64px; }
.pool-sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
@media (max-width: 640px) { .sms-number-pool { padding: 14px; }.pool-delete-actions { margin-left: 0; }.pool-toolbar > input { min-width: 100px; }.pool-pagination { justify-content: flex-start; }.sms-number-pool textarea, .sms-number-pool input:not([type="checkbox"]):not([type="file"]), .sms-number-pool select { font-size: 16px; }.pool-import summary span { display: block; margin-left: 0; } }
</style>
