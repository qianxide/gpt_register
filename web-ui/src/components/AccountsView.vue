<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { 
  Check, 
  CheckSquare, 
  ArrowLeftRight,
  Cable,
  Clipboard, 
  ClipboardList, 
  Copy, 
  Download, 
  Eye, 
  EyeOff, 
  KeyRound, 
  Monitor, 
  Pencil, 
  Plus, 
  RefreshCw, 
  ShieldCheck, 
  Trash2, 
  Users,
  Smartphone,
  Upload,
  X
} from "@lucide/vue";
import AppPager from "./AppPager.vue";
import { api, jsonBody } from "../api.js";

const props = defineProps({ 
  accounts: { type: Array, default: () => [] }, 
  accountsLoading: Boolean,
  serverPagination: Boolean,
  selectionRemoval: { type: Object, default: () => ({ keys: [], all: false }) },
  pageTotal: { type: Number, default: 0 },
  pageStats: { type: Object, default: () => ({}) },
  pageState: { type: Object, default: () => ({}) }, 
  accountLoadProgress: { type: Object, default: () => ({ loaded: 0, total: 0 }) }, 
  accountLoadError: { type: String, default: "" }, 
  groups: { type: Array, default: () => [] },
  // The account store exposes whether an RT exists; this separate snapshot
  // tells us whether that RT has actually been written to cliproxy.  The
  // status snapshot is authoritative: a file merely mentioning the same
  // email is not enough when its credentials are incomplete or mismatched.
  relayAccounts: { type: Array, default: () => [] },
  relayLoading: Boolean,
  relayStatusItems: { type: Array, default: () => [] },
  relayStatusLoading: Boolean,
  relayStatusLoaded: Boolean,
  relayStatusError: { type: String, default: "" },
  proxyCountries: { type: Array, default: () => [] }, 
  busy: Boolean, 
  copyingAt: Boolean,
  codexOauthRunning: Boolean, 
  healthProgress: { type: Object, default: () => ({}) }, 
  reauthProgress: { type: Object, default: () => ({}) }, 
  trialProgress: { type: Object, default: () => ({}) },
  refreshAtProgress: { type: Object, default: () => ({}) }
});

const emit = defineEmits([
  "refresh", "health", "resume-health", "resume-trial", "reauth", "security-repair", "password-change", "totp-change", 
  "account-logs", "stop-reauth", "delete", "delete-invalid", "download", "copy-selected-at", "codex-oauth", "sync-relay", "import-accounts", "rebind", 
  "export-selected-at", "export-security", "create-group", "rename-group", "delete-group", 
  "move-group", "export-group", "copy-at", "copy-credential", "copy-value", "trial-check",
  "copy-session", "refresh-at", "page-request"
]);

const query = ref(props.pageState.query ?? "");
const searchNeedle = ref(props.pageState.query ?? "");
const statusFilter = ref(props.pageState.status ?? "all");
const planFilter = ref(props.pageState.plan ?? "all");
const offerFilter = ref(props.pageState.trial ?? "all");
const smsFilter = ref(props.pageState.sms ?? "all");
const atFilter = ref(props.pageState.at ?? "all");
const groupFilter = ref(props.pageState.group ?? "all");
const groupQuery = ref("");
const moveTarget = ref("");
const newGroupName = ref("");
const pageSize = ref(props.pageState.pageSize ?? 20);
const page = ref(props.pageState.page ?? 1);
const selected = ref([]);
const selectionQuantity = ref(100);
const MAX_SCOPED_SELECTION = 10000;
const promoConcurrency = ref(5);
const promoRunning = ref(false);
const promoMessage = ref("");
const copiedFeedback = ref({});
const selectionRows = ref(new Map());
const selectionRelayStatuses = ref(new Map());
const scopedSelection = ref({ signature: "", keys: [] });
const selectionLoading = ref(false);
const selectionError = ref("");
watch(() => props.selectionRemoval, (removal) => {
  if (removal?.all) selected.value = [];
  else if (removal?.keys?.length) {
    const removed = new Set(removal.keys);
    selected.value = selected.value.filter(key => !removed.has(key));
  }
});
// Keep only selected rows between pages; switching pages must preserve selection.
watch(() => [props.accounts, selected.value], ([rows, keys]) => {
  const wanted = new Set(keys);
  const cached = new Map([...selectionRows.value].filter(([key]) => wanted.has(key)));
  for (const row of rows || []) if (wanted.has(row.delete_key)) cached.set(row.delete_key, row);
  selectionRows.value = cached;
}, { deep: true });
const operationRows = computed(() => {
  const rows = new Map((props.accounts || []).map((row) => [row.delete_key, row]));
  for (const [key, row] of selectionRows.value) if (!rows.has(key)) rows.set(key, row);
  return [...rows.values()];
});
function pageCriteria() {
  return { query: searchNeedle.value, status: statusFilter.value, plan: planFilter.value,
    trial: offerFilter.value, sms: smsFilter.value, at: atFilter.value, group: groupFilter.value };
}
let pageRequestQueued = false;
function requestPage() {
  if (!props.serverPagination || pageRequestQueued) return;
  pageRequestQueued = true;
  queueMicrotask(() => {
    pageRequestQueued = false;
    emit("page-request", { ...pageCriteria(), page: page.value, pageSize: pageSize.value });
  });
}
watch([searchNeedle, statusFilter, planFilter, offerFilter, smsFilter, atFilter, groupFilter, pageSize], () => {
  page.value = 1;
  requestPage();
});
watch(page, requestPage);
watch(() => props.pageState.page, (value) => {
  if (value && page.value !== value) page.value = value;
});

function triggerCopied(key) {
  copiedFeedback.value[key] = true;
  setTimeout(() => {
    copiedFeedback.value[key] = false;
  }, 1500);
}

async function copyTextDirect(text, key, fallbackItem, fallbackField) {
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("clipboard copy failed");
    }
    triggerCopied(key);
    emit("copy-value", { text, successMessage: `已复制到剪贴板: ${text}` });
  } catch {
    if (fallbackItem && fallbackField) {
      emit("copy-credential", fallbackItem, fallbackField);
    }
  }
}

watch(() => [props.relayStatusItems, selected.value], ([items, keys]) => {
  const wanted = new Set(keys);
  const cached = new Map([...selectionRelayStatuses.value].filter(([key]) => wanted.has(key)));
  for (const item of items || []) if (wanted.has(item.delete_key)) cached.set(item.delete_key, item.relay_status);
  selectionRelayStatuses.value = cached;
}, { deep: true });
const relayStatusByKey = computed(() => {
  const map = new Map();
  for (const item of props.relayStatusItems || []) {
    const key = String(item?.delete_key || "").trim();
    if (key) map.set(key, String(item?.relay_status || "unknown").trim().toLowerCase() || "unknown");
  }
  return map;
});
// A failed or partial inventory must never turn into a valid empty snapshot.
// Until the strict endpoint has returned successfully, relay filters and
// sync actions stay unavailable instead of treating every RT as missing.
const relayStatusKnown = computed(() => !!props.relayStatusLoaded
  && !props.relayStatusLoading
  && !props.relayStatusError);
function relayStatusFor(item) {
  const key = String(item?.delete_key || "").trim();
  if (key && relayStatusByKey.value.has(key)) return relayStatusByKey.value.get(key);
  if (key && selectionRelayStatuses.value.has(key)) return selectionRelayStatuses.value.get(key);
  return "unknown";
}
function accountHasRelay(item) {
  return relayStatusKnown.value && relayStatusFor(item) === "ready";
}
const relayMissingCount = computed(() => (props.accounts || []).filter(
  (item) => !item?.is_deactivated && item?.has_refresh_token && relayStatusKnown.value && relayStatusFor(item) !== "ready",
).length);

const healthPercent = computed(() => props.healthProgress.total ? Math.round((props.healthProgress.done / props.healthProgress.total) * 100) : (props.healthProgress.requested && !props.healthProgress.running ? 100 : 0));

const accountIndex = computed(() => {
  const rows = operationRows.value;
  const invalidKeys = [];
  const reauthKeys = [];
  const reloginKeys = [];
  const protocolReauthKeys = [];
  const securityRepairKeys = [];
  const securityOperationKeys = [];
  const accessTokenKeys = new Set();
  const relaySyncKeys = new Set();
  const refreshAtKeys = new Set();
  const atRiskKeys = [];
  let usable = 0;
  let deactivated = 0;
  let checked = 0;
  let expiredAt = 0;
  for (const item of rows) {
    const key = item?.delete_key;
    const health = String(item.health_status || "unknown").toLowerCase();
    const isDeactivated = !!item.is_deactivated;
    if (!isDeactivated && ["alive", "limited"].includes(health)) usable += 1;
    if (isDeactivated) deactivated += 1;
    if (health !== "unknown") checked += 1;
    if (!isDeactivated && item.at_expired) expiredAt += 1;
    if (!isDeactivated && health === "invalid" && key) invalidKeys.push(key);
    if (!isDeactivated && key) reloginKeys.push(key);
    if (item.reauth_available && key) {
      reauthKeys.push(key);
      if (item.has_password && item.has_totp) protocolReauthKeys.push(key);
    }
    if (item.security_repair_available && key) securityRepairKeys.push(key);
    const passwordConfirmed = item.password_confirmed !== false
      && !!item.has_password
      && !(item.password_confirmed === undefined
        && !!item.security_repair_available
        && !!item.has_totp);
    if (!isDeactivated && passwordConfirmed && item.has_totp && key) securityOperationKeys.push(key);
    if (item.has_access_token && key) accessTokenKeys.add(key);
    // Only the strict relay-status snapshot can authorize a sync. This keeps
    // a transient load failure from selecting all RT-bearing accounts.
    if (!isDeactivated && item.has_refresh_token && key
      && relayStatusKnown.value && relayStatusFor(item) !== "ready") relaySyncKeys.add(key);
    if (!isDeactivated && key) refreshAtKeys.add(key);
    // 刷新AT/重新登录共用的"待处理"口径：测活失败 ∪ AT过期(或缺失)，未封号。
    if (!isDeactivated && key && (["invalid", "error", "reauth_required"].includes(health) || item.at_expired || !item.has_access_token)) atRiskKeys.push(key);
  }
  return {
    stats: { total: rows.length, usable, deactivated, checked, expiredAt },
    invalidKeys,
    reauthKeys,
    reloginKeys,
    protocolReauthKeys,
    securityRepairKeys,
    securityOperationKeys,
    accessTokenKeys,
    relaySyncKeys,
    refreshAtKeys,
    atRiskKeys,
  };
});

const accountStats = computed(() => props.serverPagination
  ? { ...accountIndex.value.stats, ...props.pageStats,
      total: Number(props.pageStats.total ?? props.pageStats.count ?? 0),
      usable: Number(props.pageStats.usable ?? props.pageStats.active_count ?? 0) }
  : accountIndex.value.stats);
const accountTotal = computed(() => props.serverPagination ? accountStats.value.total : props.accounts.length);
const filteredTotal = computed(() => props.serverPagination ? props.pageTotal : filtered.value.length);
// 刷新AT/重新登录共用的待处理数量（测活失败 ∪ AT过期/缺失，未封号），保证两个按钮数字一致。
const atRiskCount = computed(() => props.serverPagination ? Number(props.pageStats.at_risk_count || 0) : accountIndex.value.atRiskKeys.length);
const accountLoadPercent = computed(() => {
  const total = Number(props.accountLoadProgress?.total || 0);
  const loaded = Number(props.accountLoadProgress?.loaded || props.accounts.length || 0);
  return total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
});
const accountLoadBlocked = computed(() => props.accountsLoading || selectionLoading.value || !!props.accountLoadError);
const invalidKeys = computed(() => accountIndex.value.invalidKeys);
const invalidCount = computed(() => props.serverPagination ? Number(props.pageStats.invalid_count || 0) : invalidKeys.value.length);
// 重新登录支持选中任意未封号账号（含正常账号）；未选中时回落到全部异常账号。
const reloginKeys = computed(() => accountIndex.value.reloginKeys);
const selectedReauthKeys = computed(() => {
  const available = new Set(reloginKeys.value);
  return selected.value.filter((key) => available.has(key));
});
const securityRepairKeys = computed(() => accountIndex.value.securityRepairKeys);
const selectedSecurityRepairKeys = computed(() => {
  const available = new Set(securityRepairKeys.value);
  return selected.value.filter((key) => available.has(key));
});
const securityOperationKeys = computed(() => accountIndex.value.securityOperationKeys);
const selectedSecurityOperationKeys = computed(() => {
  const available = new Set(securityOperationKeys.value);
  return selected.value.filter((key) => available.has(key));
});
const selectedAccessTokenKeys = computed(() => {
  const available = accountIndex.value.accessTokenKeys;
  return selected.value.filter((key) => available.has(key));
});
const selectedRelaySyncKeys = computed(() => {
  const available = accountIndex.value.relaySyncKeys;
  return selected.value.filter((key) => available.has(key));
});

let searchDebounceTimer = null;
watch(query, (val) => {
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    searchNeedle.value = String(val || "").trim().toLowerCase();
  }, 180);
});

const proxyCountriesByUrl = computed(() => {
  const map = new Map();
  for (const item of props.proxyCountries || []) {
    if (item?.url && item?.country) {
      map.set(normalizeProxyUrl(item.url), item.country);
    }
  }
  return map;
});

const proxyCountryCache = new Map();
let regionDisplayNames = null;
const countryLabels = computed(() => {
  const map = new Map();
  for (const item of props.proxyCountries || []) {
    if (item?.country && item?.label) {
      map.set(item.country, item.label);
    }
  }
  return map;
});

const searchTextByItem = computed(() => {
  if (!searchNeedle.value) return null;
  const index = new Map();
  for (const item of props.accounts || []) {
    const proxy = proxyDisplay(item.proxy);
    index.set(item, [
      item.email,
      item.account_id,
      item.group_name,
      item.plan_type,
      item.health_status,
      item.status,
      item.registration_state_label,
      statusName(item),
      item.proxy,
      proxy.label,
      proxy.country,
      item.source_type,
      item.registration_method,
      item.browser_type,
      registrationText(item),
      item.trial_eligibility_label,
      item.trial_eligibility_status,
    ].map((value) => String(value || "").toLowerCase()));
  }
  return index;
});

const reauthPercent = computed(() => props.reauthProgress.total ? Math.round((props.reauthProgress.done / props.reauthProgress.total) * 100) : 0);
const trialPercent = computed(() => props.trialProgress.total ? Math.round((props.trialProgress.done / props.trialProgress.total) * 100) : 0);
const refreshAtPercent = computed(() => props.refreshAtProgress.total ? Math.round((props.refreshAtProgress.done / props.refreshAtProgress.total) * 100) : 0);
const selectedRefreshAtKeys = computed(() => {
  const available = accountIndex.value.refreshAtKeys;
  return selected.value.filter((key) => available.has(key));
});
const reauthModeLabel = computed(() => props.reauthProgress.mode === "protocol" ? "协议" : "浏览器");
const reauthOperationLabel = computed(() => ({
  reauth: "重新登录",
  security_repair: "安全字段补全",
  password_change: "修改密码",
  totp_change: "修改 2FA",
}[props.reauthProgress.operation] || "账号操作"));

const totpCodes = ref({});
const totpLoading = ref({});
const totpNow = ref(Date.now());
let totpTickTimer = null;
onMounted(() => { totpTickTimer = setInterval(() => { totpNow.value = Date.now(); autoRefreshTotps(); }, 1000); });
onBeforeUnmount(() => { if (totpTickTimer) clearInterval(totpTickTimer); if (searchDebounceTimer) clearTimeout(searchDebounceTimer); });
const visibleGroups = computed(() => {
  const needle = groupQuery.value.trim().toLowerCase();
  return needle
    ? props.groups.filter((item) => String(item.name || "").toLowerCase().includes(needle))
    : props.groups;
});
const activeGroup = computed(() => props.groups.find((item) => item.id === groupFilter.value) || null);

const groupKeys = computed(() => {
  if (!activeGroup.value) return [];
  return (props.accounts || [])
    .filter((item) => (item.group_id || "default") === activeGroup.value.id)
    .map((item) => item.delete_key)
    .filter(Boolean);
});
const activeGroupCount = computed(() => Number(activeGroup.value?.account_count || 0));
const groupAllSelected = computed(() => {
  if (props.serverPagination) return activeGroupCount.value > 0 && [...selectionRows.value.values()].filter(row => (row.group_id || "default") === activeGroup.value?.id).length >= activeGroupCount.value;
  if (!groupKeys.value.length) return false;
  const current = new Set(selected.value);
  return groupKeys.value.every((key) => current.has(key));
});

const filteredGroupKeys = computed(() => {
  if (!activeGroup.value) return [];
  return filtered.value.filter((item) => (item.group_id || "default") === activeGroup.value.id).map((item) => item.delete_key).filter(Boolean);
});
const filteredGroupAllSelected = computed(() => {
  if (props.serverPagination && scopedSelection.value.signature === JSON.stringify(pageCriteria())) {
    return scopedSelection.value.keys.length > 0 && scopedSelection.value.keys.every(key => selected.value.includes(key));
  }
  if (props.serverPagination && filteredTotal.value > filteredGroupKeys.value.length) return false;
  if (!filteredGroupKeys.value.length) return false;
  const current = new Set(selected.value);
  return filteredGroupKeys.value.every((key) => current.has(key));
});

const filtered = computed(() => {
  if (props.serverPagination) return props.accounts || [];
  const needle = searchNeedle.value;
  const status = statusFilter.value;
  const plan = planFilter.value;
  const offer = offerFilter.value;
  const sms = smsFilter.value;
  const at = atFilter.value;
  const group = groupFilter.value;
  const textIndex = searchTextByItem.value;

  return (props.accounts || []).filter((item) => {
    if (group !== "all" && (item.group_id || "default") !== group) return false;
    if (status !== "all") {
      const isDeactivated = !!item.is_deactivated;
      const normalizedStatus = isDeactivated ? "deactivated" : (item.health_status || item.status || "unknown").toLowerCase();
      if (status === "security_missing" && !item.security_repair_available) return false;
      if (status === "alive" && normalizedStatus !== "alive") return false;
      if (status === "limited" && normalizedStatus !== "limited") return false;
      if (status === "reauth_required" && normalizedStatus !== "reauth_required") return false;
      if (status === "deactivated" && !isDeactivated) return false;
      if (status === "invalid" && (isDeactivated || normalizedStatus !== "invalid")) return false;
      if (status === "error" && normalizedStatus !== "error") return false;
      if (status === "unknown" && normalizedStatus !== "unknown") return false;
    }
    if (plan !== "all" && planType(item) !== plan) return false;
    if (offer === "trial" && item.trial_eligibility_status !== "eligible") return false;
    if (offer === "no-trial" && item.trial_eligibility_status === "eligible") return false;
    if (sms === "sms" && !item.has_refresh_token) return false;
    if (sms === "no-sms" && item.has_refresh_token) return false;
    if (sms === "sms-no-relay" && (!item.has_refresh_token || !relayStatusKnown.value || relayStatusFor(item) === "ready")) return false;
    if (sms === "sms-relay" && (!item.has_refresh_token || !relayStatusKnown.value || relayStatusFor(item) !== "ready")) return false;
    if (at === "has-at" && !item.has_access_token) return false;
    if (at === "no-at" && item.has_access_token) return false;
    if (!needle) return true;
    const values = textIndex?.get(item);
    return values ? values.some((val) => val.includes(needle)) : true;
  });
});

const pages = computed(() => Math.max(1, Math.ceil(filteredTotal.value / pageSize.value)));
const pageItems = computed(() => {
  if (props.serverPagination) return props.accounts || [];
  const start = (page.value - 1) * pageSize.value;
  return filtered.value.slice(start, start + pageSize.value);
});

const allPageSelected = computed({
  get() {
    if (!pageItems.value.length) return false;
    return pageItems.value.every((item) => selected.value.includes(item.delete_key));
  },
  set(val) {
    const current = new Set(selected.value);
    for (const item of pageItems.value) {
      if (!item.delete_key) continue;
      if (val) current.add(item.delete_key);
      else current.delete(item.delete_key);
    }
    selected.value = [...current];
  },
});

const selectedOnPageCount = computed(() => {
  if (!pageItems.value.length || !selected.value.length) return 0;
  const current = new Set(selected.value);
  return pageItems.value.reduce(
    (count, item) => count + (item.delete_key && current.has(item.delete_key) ? 1 : 0),
    0
  );
});

// 列表刷新完成后剔除已删除账号的选中项，避免删除后再选中时计数残留。
watch(
  () => [props.accounts, props.accountsLoading],
  ([rows, loading]) => {
    if (props.serverPagination || loading || !selected.value.length) return;
    const alive = new Set((rows || []).map((item) => item?.delete_key).filter(Boolean));
    const next = selected.value.filter((key) => alive.has(key));
    if (next.length !== selected.value.length) selected.value = [...next];
  }
);

function clearAllSelected() {
  selected.value = [];
}

function selectCurrentPage() {
  const current = new Set(selected.value);
  for (const item of pageItems.value) {
    if (item.delete_key) current.add(item.delete_key);
  }
  selected.value = [...current];
}

function clearCurrentPage() {
  const pageKeys = new Set(pageItems.value.map((item) => item.delete_key).filter(Boolean));
  selected.value = selected.value.filter((key) => !pageKeys.has(key));
}

function planType(item) {
  const plan = String(item.plan_type || "free").toLowerCase();
  // Team 套餐的 plan_type 可能是 self_serve_business_prolite 等,含 business/team
  if (plan.includes("team") || plan.includes("business")) return "team";
  if (plan.includes("pro")) return "pro";
  if (plan.includes("plus")) return "plus";
  return "free";
}
function planName(item) {
  const plan = String(item.plan_type || "Free").toLowerCase();
  if (plan.includes("team") || plan.includes("business")) return "Team";
  if (plan.includes("pro")) return "Pro";
  if (plan.includes("plus")) return "Plus";
  return "Free";
}
function statusName(item) {
  if (item.is_deactivated) return "已封号留档";
  const map = {
    alive: "可用",
    limited: "限流",
    reauth_required: "待续期",
    invalid: "失效",
    error: "错误",
    unknown: "未检测",
  };
  return map[String(item.health_status || item.status || "unknown").toLowerCase()] || "未检测";
}
function statusClass(item) {
  if (item.is_deactivated) return "deactivated";
  const s = String(item.health_status || item.status || "unknown").toLowerCase();
  return ["alive", "limited", "reauth_required", "invalid", "error"].includes(s) ? s : "unknown";
}
function trialLabel(item) {
  if (item.trial_eligibility_label) return item.trial_eligibility_label;
  if (item.trial_eligibility_status === "eligible") return "符合0元资格";
  if (item.trial_eligibility_status === "ineligible") return "不符合";
  if (item.trial_eligibility_status === "failed") return "检测失败";
  return "未检测";
}
function trialClass(item) {
  if (item.trial_eligibility_status === "eligible") return "alive";
  if (item.trial_eligibility_status === "ineligible") return "limited";
  if (item.trial_eligibility_status === "failed") return "error";
  return "unknown";
}
function registrationText(item) {
  return item.registration_state_label || (item.source_type === "imported" ? "外部导入" : (item.browser_type || "指纹注册"));
}
function quotaText(val) {
  return typeof val === "number" ? `${val} 次` : "-";
}
function mfaText(item) {
  if (item.mfa_enabled) return "已启用";
  if (item.has_totp) return "已存秘钥";
  return "未设置";
}

function totpEntry(item) {
  const entry = totpCodes.value[item.delete_key];
  // Codes live for ~30s; a cached entry older than its window must never be
  // re-copied, otherwise the user pastes a stale (rejected) 2FA code.
  if (!entry) return null;
  if (entry.expiresAt && Date.now() >= entry.expiresAt - 1200) return null;
  return entry;
}
function totpDisplay(item) {
  const entry = totpEntry(item);
  if (entry?.code) {
    const left = Math.max(0, Math.ceil(((entry.expiresAt || 0) - totpNow.value) / 1000));
    return left > 0 ? `${entry.code} (${left}s)` : "获取验证码";
  }
  if (entry?.error) return "获取失败";
  return totpLoading.value[item.delete_key] ? "获取中…" : "获取验证码";
}
async function fetchTotpCode(key) {
  if (totpLoading.value[key]) return null;
  totpLoading.value[key] = true;
  try {
    const res = await api("/api/accounts/credentials", { method: "POST", body: jsonBody({ key, field: "totp_code" }) });
    if (res.code) {
      totpCodes.value[key] = { code: res.code, expiresAt: Date.now() + Math.max(3, res.remaining ?? 30) * 1000 };
      return totpCodes.value[key];
    }
    totpCodes.value[key] = { error: res.message || "解码失败" };
  } catch (err) {
    totpCodes.value[key] = { error: err.message || "解码异常" };
  } finally {
    totpLoading.value[key] = false;
  }
  return null;
}
async function handleTotpClick(item) {
  const entry = totpEntry(item);
  if (entry?.code) {
    copyTextDirect(entry.code, `totp_code_${item.delete_key}`);
    return;
  }
  const fresh = await fetchTotpCode(item.delete_key);
  if (fresh?.code) copyTextDirect(fresh.code, `totp_code_${item.delete_key}`);
}
function autoRefreshTotps() {
  if (document.hidden) return;
  for (const [key, entry] of Object.entries(totpCodes.value)) {
    // 仅自动续期获取成功过的验证码；失败的等用户手动重试
    if (!entry?.expiresAt || entry.error) continue;
    if (Date.now() < entry.expiresAt - 1200) continue;
    fetchTotpCode(key);
  }
}

const deviceDialog = ref({
  open: false, key: "", email: "", loading: false, error: "",
  fingerprint: null, proxy: "", registered_at: "", registration_method: "", device_id: "", has_snapshot: false,
});

async function openDeviceInfo(item) {
  deviceDialog.value = {
    open: true, key: item.delete_key, email: item.email, loading: true, error: "",
    fingerprint: null, proxy: item.proxy || "", registered_at: item.registered_at || "",
    registration_method: "", device_id: "", has_snapshot: false,
  };
  try {
    const res = await api("/api/accounts/device-info", { method: "POST", body: jsonBody({ key: item.delete_key }) });
    deviceDialog.value = {
      ...deviceDialog.value,
      loading: false,
      fingerprint: res.fingerprint || {},
      proxy: res.proxy || deviceDialog.value.proxy,
      registered_at: res.registered_at || deviceDialog.value.registered_at,
      registration_method: res.registration_method || "",
      device_id: res.device_id || "",
      has_snapshot: !!res.has_snapshot,
    };
  } catch (err) {
    deviceDialog.value = { ...deviceDialog.value, loading: false, error: err.message || "加载失败" };
  }
}
function closeDeviceInfo() {
  deviceDialog.value.open = false;
}
function deviceFamilyLabel(fp) {
  const family = String(fp?.browser_family || "firefox").toLowerCase();
  const version = fp?.full_version || fp?.major || "";
  const name = family === "chrome" ? "Chrome" : "Firefox";
  return version ? `${name} ${version}` : name;
}
function deviceTzLabel(fp) {
  if (!fp?.timezone) return "";
  const raw = fp.timezone_offset_min;
  if (raw === undefined || raw === null || raw === "") return fp.timezone;
  const off = Number(raw);
  if (Number.isNaN(off)) return fp.timezone;
  const sign = off <= 0 ? "+" : "-";
  const abs = Math.abs(off);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `${fp.timezone} (UTC${sign}${h}${m ? ":" + String(m).padStart(2, "0") : ""})`;
}
const deviceGroups = computed(() => {
  const fp = deviceDialog.value.fingerprint || {};
  const d = deviceDialog.value;
  const mem = fp.device_memory ? `${fp.device_memory} GB` : "";
  return [
    {
      title: "浏览器环境",
      rows: [
        ["浏览器", deviceFamilyLabel(fp)],
        ["TLS 指纹", fp.impersonate],
        ["User-Agent", fp.user_agent],
        ["Vendor", fp.navigator_vendor],
        ["Sec-CH-UA", fp.sec_ch_ua],
      ],
    },
    {
      title: "屏幕与硬件",
      rows: [
        ["分辨率", fp.screen],
        ["像素比", fp.device_pixel_ratio],
        ["CPU 线程", fp.hardware_concurrency],
        ["内存", mem],
        ["色深", fp.color_depth ? `${fp.color_depth} bit` : ""],
        ["触点", fp.max_touch_points],
        ["平台", fp.platform ? `${fp.platform} · Win ${fp.platform_version || "10"}` : ""],
      ],
    },
    {
      title: "图形 (WebGL)",
      rows: [
        ["Vendor", fp.webgl_vendor],
        ["Renderer", fp.webgl_renderer],
      ],
    },
    {
      title: "地区环境",
      rows: [
        ["时区", deviceTzLabel(fp)],
        ["Locale", fp.locale],
        ["语言", fp.accept_language],
      ],
    },
    {
      title: "注册上下文",
      rows: [
        ["注册方式", d.registration_method === "protocol" ? "协议注册" : d.registration_method || ""],
        ["代理", d.proxy],
        ["Device ID", d.device_id || fp.device_id],
        ["Profile ID", fp.profile_id],
        ["Sentinel SID", fp.sentinel_sid],
        ["注册时间", d.registered_at ? new Date(d.registered_at).toLocaleString("zh-CN", { hour12: false }) : ""],
      ],
    },
  ].map((group) => ({ ...group, rows: group.rows.filter((row) => String(row[1] || "").trim()) }));
});

function fmtTime(value) {
  if (!value) return "-";
  try { return new Date(value).toLocaleString("zh-CN", { hour12: false }); } catch { return value; }
}
function fmtShortTime(value) {
  if (!value) return "-";
  try {
    const d = new Date(value);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
  } catch { return value; }
}
function isStale(value) {
  if (!value) return false;
  try { return Date.now() - new Date(value).getTime() > 3 * 24 * 3600 * 1000; } catch { return false; }
}
function lastRefreshAt(item) {
  const candidates = [item?.health_checked_at, item?.last_token_refresh_at]
    .filter(Boolean)
    .map(value => ({ value, time: new Date(value).getTime() }))
    .filter(entry => Number.isFinite(entry.time));
  if (!candidates.length) return item?.last_token_refresh_at || item?.health_checked_at || null;
  return candidates.reduce((latest, entry) => entry.time > latest.time ? entry : latest).value;
}
function lastInvalidAt(item) {
  const failed = item.is_deactivated || ["invalid", "deactivated", "error", "token_invalid"].includes(String(item.health_status || item.status || "").toLowerCase());
  return failed ? (item.updated_at || item.health_checked_at || null) : null;
}
function normalizeProxyUrl(value) {
  let proxy = String(value || "").trim().replace(/^\ufeff/, "").replace(/[,;]+$/, "");
  if (!proxy || proxy.startsWith("#")) return "";
  if (!proxy.includes("://")) {
    const parts = proxy.split(":");
    if (parts.length === 4 && /^\d+$/.test(parts[1])) {
      const [host, port, username, password] = parts;
      proxy = `http://${username}:${password}@${host}:${port}`;
    } else if (parts.length === 4 && /^\d+$/.test(parts[3])) {
      const [username, password, host, port] = parts;
      proxy = `http://${username}:${password}@${host}:${port}`;
    } else {
      proxy = `http://${proxy}`;
    }
  }
  return proxy.replace(/^socks:\/\//i, "socks5h://").replace(/^socks4a?:\/\//i, "socks4://");
}
function countryFromProxyUrl(value) {
  const raw = String(value || "");
  if (proxyCountryCache.has(raw)) return proxyCountryCache.get(raw);
  const normalized = normalizeProxyUrl(value);
  const configured = proxyCountriesByUrl.value.get(normalized);
  if (configured) {
    proxyCountryCache.set(raw, configured);
    return configured;
  }
  let decoded = raw;
  try { decoded = decodeURIComponent(decoded); } catch {}
  const patterns = [
    /(?:^|[_-])g[-_=]?([a-z]{2})(?=[_:@/?&-]|$)/i,
    /(?:region|country|countrycode|cc)[-_=:/]?([a-z]{2})(?=[_:@/?&-]|$)/i,
    /[?&](?:region|country|country_code|cc)=([a-z]{2})(?:[&#]|$)/i,
  ];
  for (const pattern of patterns) {
    const match = decoded.match(pattern);
    if (match) {
      const country = match[1].toUpperCase();
      proxyCountryCache.set(raw, country);
      return country;
    }
  }
  proxyCountryCache.set(raw, "");
  return "";
}
function proxyDisplay(value) {
  if (!String(value || "").trim()) return { country: "US", label: "本地·美国", local: true };
  const country = countryFromProxyUrl(value);
  if (!country) return { country: "", label: "未识别" };
  let label = countryLabels.value.get(country) || "";
  if (!label && typeof Intl?.DisplayNames === "function") {
    try {
      regionDisplayNames ||= new Intl.DisplayNames(["zh-CN"], { type: "region" });
      label = regionDisplayNames.of(country) || "";
    } catch {}
  }
  return { country, label: label || country };
}
function createGroup() {
  const name = newGroupName.value.trim();
  if (!name) return;
  emit("create-group", name);
  newGroupName.value = "";
}
function renameGroup(group) {
  if (!group) return;
  emit("rename-group", group.id, group.name);
}
function deleteGroup(group) {
  if (!group) return;
  emit("delete-group", group.id, group.name);
}
async function fetchMatchingRows(criteria, maxRows = null) {
  const requested = maxRows !== null && maxRows !== undefined && Number.isFinite(Number(maxRows))
    ? Math.max(1, Math.min(MAX_SCOPED_SELECTION, Math.floor(Number(maxRows))))
    : null;
  const relayFilter = ["sms-relay", "sms-no-relay"].includes(criteria.sms);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const rows = [];
    let revision;
    let total = 0;
    let stable = true;
    for (let offset = 0; ;) {
      const remaining = requested ? requested - rows.length : 200;
      const requestLimit = relayFilter ? 200 : Math.min(20000, Math.max(1, remaining));
      const params = new URLSearchParams({ ...criteria, limit: String(requestLimit), offset: String(offset) });
      if (relayFilter) params.set("sms", "sms");
      const data = await api(`/api/accounts?${params}`);
      if (offset && data.revision !== revision) { stable = false; break; }
      revision = data.revision;
      total = Number(data.total ?? data.count ?? 0);
      const sourceRows = data.items || [];
      if (relayFilter && sourceRows.length) {
        const snapshot = await api("/api/accounts/relay-status", { method: "POST", body: jsonBody({ keys: sourceRows.map(row => row.delete_key) }) });
        const byKey = new Map((snapshot.items || []).map(row => [row.delete_key, row.relay_status]));
        rows.push(...sourceRows.filter(row => criteria.sms === "sms-relay" ? byKey.get(row.delete_key) === "ready" : byKey.get(row.delete_key) !== "ready"));
      } else {
        rows.push(...sourceRows);
      }
      if (requested && rows.length >= requested) return rows.slice(0, requested);
      offset += sourceRows.length;
      if (offset >= total || !sourceRows.length) break;
    }
    if (!stable || (!relayFilter && rows.length < total)) continue;
    return rows;
  }
  throw new Error("账号更新较频繁，请重试选择");
}
async function toggleScopedSelection(filteredScope) {
  if (!activeGroup.value || selectionLoading.value) return;
  selectionLoading.value = true;
  selectionError.value = "";
  const groupId = activeGroup.value.id;
  try {
    const rows = props.serverPagination
      ? await fetchMatchingRows(filteredScope ? pageCriteria() : { group: groupId })
      : (props.accounts || []).filter(row => (row.group_id || "default") === groupId && (!filteredScope || filtered.value.includes(row)));
    // Ignore a selection request after navigating to another group.
    if (activeGroup.value?.id !== groupId) return;
    const keys = rows.map(row => row.delete_key).filter(Boolean);
    if (filteredScope) scopedSelection.value = { signature: JSON.stringify(pageCriteria()), keys };
    const next = new Set(selected.value);
    const shouldSelect = !keys.length || !keys.every(key => next.has(key));
    const cached = new Map(selectionRows.value);
    for (const row of rows) {
      if (shouldSelect) { next.add(row.delete_key); cached.set(row.delete_key, row); }
      else { next.delete(row.delete_key); cached.delete(row.delete_key); }
    }
    selectionRows.value = cached;
    selected.value = [...next];
  } catch (error) { selectionError.value = error.message || "选择账号失败"; }
  finally { selectionLoading.value = false; }
}
function toggleActiveGroupSelection() { return toggleScopedSelection(true); }
function toggleGroupSelection() { return toggleScopedSelection(false); }
async function selectFilteredQuantity() {
  if (selectionLoading.value || !filteredTotal.value) return;
  const requested = Math.min(
    MAX_SCOPED_SELECTION,
    Math.max(1, Math.floor(Number(selectionQuantity.value) || 1)),
  );
  selectionQuantity.value = requested;
  selectionLoading.value = true;
  selectionError.value = "";
  try {
    const rows = props.serverPagination
      ? await fetchMatchingRows(pageCriteria(), requested)
      : filtered.value.slice(0, requested);
    const cached = new Map();
    const keys = [];
    for (const row of rows) {
      const key = row?.delete_key;
      if (!key || cached.has(key)) continue;
      cached.set(key, row);
      keys.push(key);
    }
    selectionRows.value = cached;
    selected.value = keys;
    scopedSelection.value = keys.length === filteredTotal.value
      ? { signature: JSON.stringify(pageCriteria()), keys: [...keys] }
      : { signature: "", keys: [] };
    if (keys.length < requested) selectionError.value = `当前筛选只有 ${keys.length} 个账号，已全部选中`;
  } catch (error) {
    selectionError.value = error?.message || "按数量选择账号失败";
  } finally {
    selectionLoading.value = false;
  }
}
async function deleteInvalid() {
  if (selectionLoading.value) return;
  selectionLoading.value = true;
  selectionError.value = "";
  try {
    const keys = props.serverPagination
      ? (await fetchMatchingRows({ status: "invalid" })).map(row => row.delete_key).filter(Boolean)
      : invalidKeys.value;
    if (keys.length) emit("delete-invalid", keys);
  } catch (error) { selectionError.value = error.message || "读取失效账号失败"; }
  finally { selectionLoading.value = false; }
}
async function checkPromoSelected() {
  if (!selected.value.length || promoRunning.value) return;
  promoRunning.value = true;
  promoMessage.value = `正在检测 ${selected.value.length} 个账号…`;
  try {
    const concurrency = Math.max(1, Math.floor(Number(promoConcurrency.value) || 1));
    promoConcurrency.value = concurrency;
    const result = await api("/api/accounts/promo-check-bulk", {
      method: "POST",
      body: jsonBody({
        keys: [...selected.value],
        attempts: 2,
        concurrency,
      }),
    });
    promoMessage.value = `优惠检测完成：${result.promo || 0} 个发现优惠，${result.failed || 0} 个失败`;
    emit("refresh", groupFilter.value === "all" ? "" : groupFilter.value);
  } catch (error) {
    promoMessage.value = error?.message || "优惠检测失败";
  } finally {
    promoRunning.value = false;
  }
}
function moveSelected() {
  if (!selected.value.length || !moveTarget.value) return;
  emit("move-group", [...selected.value], moveTarget.value);
  selected.value = [];
}
</script>

<template>
  <section class="card accounts-panel">
    <div class="card-h">
      <div>
        <h2>账号记录</h2>
        <p class="subtle">搜索、筛选、测活并导出已注册账号，全字段支持直接点击复制。</p>
        <p v-if="accountsLoading" class="account-load-status" aria-live="polite">正在加载账号：{{ accountLoadProgress.loaded || accounts.length }} / {{ accountLoadProgress.total || "…" }}（{{ accountLoadPercent }}%），加载完成后才能执行批量测活。</p>
        <p v-if="selectionError" class="account-load-status account-load-error" role="alert">{{ selectionError }}</p>
        <p v-if="accountLoadError" class="account-load-status account-load-error" role="alert">{{ accountLoadError }}；当前显示 {{ accounts.length }} / {{ accountLoadProgress.total || "未知" }} 条。</p>
      </div>
      <button class="btn secondary" :disabled="busy || accountsLoading" @click="emit('refresh', groupFilter === 'all' ? '' : groupFilter)">
        <RefreshCw :size="14" aria-hidden="true" />
        刷新列表
      </button>
    </div>

    <div class="accounts-workspace">
      <!-- 账号分组管理侧边栏 -->
      <aside class="account-group-sidebar" aria-label="账号分组">
        <div class="account-group-sidebar-head">
          <div><strong>账号分组</strong><span>{{ groups.length }} 个分组</span></div>
        </div>
        <label class="account-group-search">
          <span aria-hidden="true">⌕</span>
          <input v-model="groupQuery" type="search" placeholder="搜索分组" />
        </label>
        <div class="account-group-select-actions">
          <button
            :class="['account-group-select-all', { active: groupAllSelected }]"
            type="button"
            :disabled="!activeGroup || !(serverPagination ? activeGroupCount : groupKeys.length) || busy || selectionLoading"
            :title="activeGroup ? `${groupAllSelected ? '取消选择' : '选中'}“${activeGroup.name}”内的全部账号（忽略其他筛选条件）` : '请先选择一个账号分组'"
            @click="toggleGroupSelection"
          >
            <CheckSquare :size="14" aria-hidden="true" />
            <span>{{ groupAllSelected ? "取消全选此分组" : "全选此分组" }}</span>
            <em>{{ serverPagination ? activeGroupCount : groupKeys.length }}</em>
          </button>
          <button
            :class="['account-group-select-all', 'filtered', { active: filteredGroupAllSelected }]"
            type="button"
            :disabled="!activeGroup || !filteredTotal || busy || selectionLoading"
            :title="activeGroup ? `${filteredGroupAllSelected ? '取消选择' : '选中'}“${activeGroup.name}”内符合当前筛选条件的账号` : '请先选择一个账号分组'"
            @click="toggleActiveGroupSelection"
          >
            <CheckSquare :size="14" aria-hidden="true" />
            <span>{{ filteredGroupAllSelected ? "取消筛选全选" : "全选筛选结果" }}</span>
            <em>{{ serverPagination ? filteredTotal : filteredGroupKeys.length }}</em>
          </button>
        </div>
        <div class="account-group-actions">
          <button type="button" title="导出该组邮箱凭证" aria-label="导出该组邮箱凭证" :disabled="!activeGroup || busy" @click="activeGroup && emit('export-group', activeGroup.id)"><Download :size="14" aria-hidden="true" /></button>
          <button type="button" title="重命名分组" aria-label="重命名分组" :disabled="!activeGroup || busy" @click="renameGroup(activeGroup)"><Pencil :size="14" aria-hidden="true" /></button>
          <button class="danger" type="button" title="删除分组" aria-label="删除分组" :disabled="!activeGroup || groups.length <= 1 || busy" @click="deleteGroup(activeGroup)"><Trash2 :size="14" aria-hidden="true" /></button>
        </div>
        <div class="account-group-create">
          <input v-model.trim="newGroupName" maxlength="48" placeholder="新分组名称" @keyup.enter="createGroup" />
          <button type="button" title="新建分组" aria-label="新建分组" :disabled="!newGroupName.trim() || busy" @click="createGroup"><Plus :size="16" aria-hidden="true" /></button>
        </div>
        <div class="account-group-list">
          <div :class="['account-group-row', { active: groupFilter === 'all' }]">
            <button class="account-group-main" type="button" @click="groupFilter = 'all'">
              <span class="account-group-icon"><Users :size="14" aria-hidden="true" /></span>
              <span class="account-group-copy"><b>全部账号</b><small>所有分组</small></span>
              <em>{{ serverPagination ? accountTotal : (accountsLoading ? (accountLoadProgress.total || accounts.length) : accounts.length) }}</em>
            </button>
          </div>
          <div v-for="group in visibleGroups" :key="group.id" :class="['account-group-row', { active: groupFilter === group.id }]">
            <button class="account-group-main" type="button" @click="groupFilter = group.id">
              <span class="account-group-icon"><Users :size="14" aria-hidden="true" /></span>
              <span class="account-group-copy"><b>{{ group.name }}</b><small>{{ groupFilter === group.id ? '当前分组' : '账号分组' }}</small></span>
              <em>{{ group.account_count || 0 }}</em>
            </button>
          </div>
          <div v-if="!visibleGroups.length" class="account-group-empty">没有匹配的分组</div>
        </div>
      </aside>

      <!-- 账号主体列表与操作区 -->
      <div class="accounts-main">
        <!-- 进度看板 -->
        <div v-if="healthProgress.requested || healthProgress.total" class="health-progress" :class="{ active: healthProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ healthProgress.running ? "账号测活进行中" : (healthProgress.pendingCount ? "测活任务已中断" : "最近一次测活完成") }}</strong>
            <span>{{ healthProgress.done }} / {{ healthProgress.total }} 实际检测 · {{ healthPercent }}%</span>
            <button v-if="!healthProgress.running && healthProgress.pendingCount" class="btn secondary reauth-stop-btn" type="button" @click="emit('resume-health')">继续检测剩余 {{ healthProgress.pendingCount }} 个</button>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${healthPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="healthProgress.current">当前：{{ healthProgress.current }}</span>
            <span>请求记录 {{ healthProgress.requested || 0 }}</span>
            <span>实际检测 {{ healthProgress.checked || healthProgress.done || 0 }}</span>
            <span>跳过已封号 {{ healthProgress.skippedKnownDeactivated || 0 }}</span>
            <span>实际并发 {{ healthProgress.workers || healthProgress.requestedWorkers || 0 }} / 请求 {{ healthProgress.requestedWorkers || healthProgress.workers || 0 }}</span>
            <span v-if="healthProgress.throttled">注册优先</span>
            <span>正常 {{ healthProgress.alive || 0 }}</span>
            <span>限流 {{ healthProgress.limited || 0 }}</span>
            <span>待续期 {{ healthProgress.reauthRequired || 0 }}</span>
            <span>其它失效 {{ healthProgress.invalidOther || 0 }}</span>
            <span>错误 {{ healthProgress.error || 0 }}</span>
            <span v-if="!healthProgress.running && healthProgress.message" class="health-progress-warning">{{ healthProgress.message }}</span>
          </div>
        </div>

        <div v-if="reauthProgress.total" class="health-progress" :class="{ active: reauthProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ reauthProgress.stopping ? `正在停止${reauthOperationLabel}` : (reauthProgress.running ? `${reauthOperationLabel}进行中` : (reauthProgress.lastStatus === "cancelled" || reauthProgress.cancelled ? `${reauthOperationLabel}已停止` : `最近一次${reauthOperationLabel}完成`)) }} · {{ reauthModeLabel }} · 并发 {{ reauthProgress.concurrency || 3 }}</strong>
            <span>{{ reauthProgress.done }} / {{ reauthProgress.total }} · {{ reauthPercent }}%</span>
            <button v-if="reauthProgress.running" class="btn danger reauth-stop-btn" type="button" :disabled="reauthProgress.stopping" @click="emit('stop-reauth')">{{ reauthProgress.stopping ? "停止中…" : `停止${reauthOperationLabel}` }}</button>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${reauthPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="reauthProgress.current">处理中：{{ reauthProgress.current }}</span>
            <span>成功 {{ reauthProgress.success || 0 }}</span>
            <span>失败 {{ reauthProgress.failed || 0 }}</span>
            <span>已停止 {{ reauthProgress.cancelled || 0 }}</span>
          </div>
          <div v-if="reauthProgress.last" class="health-progress-meta">
            <span>最近完成：{{ reauthProgress.last }}</span>
            <span>{{ reauthProgress.lastStatus === "success" ? "成功" : "失败" }}</span>
            <span v-if="reauthProgress.lastMessage">{{ reauthProgress.lastMessage }}</span>
          </div>
        </div>

        <div v-if="trialProgress.total" class="health-progress" :class="{ active: trialProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ trialProgress.running ? "0元资格检测中" : (trialProgress.pendingCount ? "0元资格检测已中断" : "最近一次0元资格检测完成") }}</strong>
            <span>{{ trialProgress.done }} / {{ trialProgress.total }} · {{ trialPercent }}%</span>
            <button v-if="!trialProgress.running && trialProgress.pendingCount" class="btn secondary reauth-stop-btn" type="button" @click="emit('resume-trial')">继续检测剩余 {{ trialProgress.pendingCount }} 个</button>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${trialPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="trialProgress.current">当前：{{ trialProgress.current }}</span>
            <span>符合资格 {{ trialProgress.eligible || 0 }}</span>
            <span>不符合 {{ trialProgress.ineligible || 0 }}</span>
            <span>失败 {{ trialProgress.failed || 0 }}</span>
            <span>已停止 {{ trialProgress.stopped || 0 }}</span>
            <span v-if="!trialProgress.running && trialProgress.message" class="health-progress-warning">{{ trialProgress.message }}</span>
          </div>
        </div>

        <div v-if="refreshAtProgress.total" class="health-progress" :class="{ active: refreshAtProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ refreshAtProgress.running ? "刷新 AT 进行中" : "最近一次刷新 AT 完成" }}</strong>
            <span>{{ refreshAtProgress.done }} / {{ refreshAtProgress.total }} · {{ refreshAtPercent }}%</span>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${refreshAtPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="refreshAtProgress.current">当前：{{ refreshAtProgress.current }}</span>
            <span>已刷新 {{ refreshAtProgress.refreshed || 0 }}</span>
            <span>AT未变化 {{ refreshAtProgress.unchanged || 0 }}</span>
            <span>失败 {{ refreshAtProgress.failed || 0 }}</span>
            <span>已停止 {{ refreshAtProgress.stopped || 0 }}</span>
          </div>
        </div>

        <!-- 筛选栏 -->
        <div class="account-filters">
          <label class="search-field">
            <span aria-hidden="true">⌕</span>
            <input v-model="query" type="search" aria-label="搜索账号" placeholder="搜索邮箱、注册方式、状态、ID或代理" />
          </label>
          <select v-model="statusFilter" aria-label="账号状态筛选">
            <option value="all">全部状态</option>
            <option value="alive">可用</option>
            <option value="limited">限流</option>
            <option value="reauth_required">待续期</option>
            <option value="deactivated">已封号</option>
            <option value="invalid">失效</option>
            <option value="error">错误</option>
            <option value="unknown">未检测</option>
            <option value="security_missing">缺安全字段</option>
          </select>
          <select v-model="planFilter" aria-label="账号套餐筛选">
            <option value="all">全部套餐</option>
            <option value="free">Free</option>
            <option value="plus">Plus</option>
            <option value="pro">Pro</option>
            <option value="team">Team</option>
          </select>
          <select v-model="offerFilter" aria-label="优惠筛选">
            <option value="all">全部</option>
            <option value="trial">有试用</option>
            <option value="no-trial">无试用</option>
          </select>
          <select v-model="smsFilter" aria-label="接码筛选">
            <option value="all">全部接码</option>
            <option value="sms">已接码</option>
            <option value="no-sms">未接码</option>
            <option value="sms-no-relay" :disabled="!relayStatusKnown">已接码 · 未入中转{{ relayStatusLoading ? "（读取中…）" : relayStatusError ? "（不可用）" : `（${serverPagination ? "本页" : ""}${relayMissingCount}）` }}</option>
            <option value="sms-relay" :disabled="!relayStatusKnown">已接码 · 已入中转</option>
          </select>
          <select v-model="atFilter" aria-label="Access Token 筛选">
            <option value="all">全部 AT</option>
            <option value="has-at">有 AT</option>
            <option value="no-at">无 AT</option>
          </select>
          <select v-model.number="pageSize" aria-label="每页数量">
            <option :value="10">10 / 页</option>
            <option :value="20">20 / 页</option>
            <option :value="50">50 / 页</option>
            <option :value="100">100 / 页</option>
          </select>
          <label class="account-filter-quantity" title="按当前全部筛选条件，从第一页开始选择指定数量的账号">
            <span>选择数量</span>
            <input v-model.number="selectionQuantity" type="number" min="1" :max="MAX_SCOPED_SELECTION" inputmode="numeric" aria-label="按筛选结果选择账号数量" />
          </label>
          <button class="btn secondary account-filter-select" type="button" :disabled="!filteredTotal || busy || selectionLoading" @click="selectFilteredQuantity">
            {{ selectionLoading ? "选择中…" : "按数量选中" }}
          </button>
          <span class="filter-result">显示 {{ filteredTotal }} / {{ accountTotal }} · 可用 {{ accountStats.usable }}<template v-if="relayStatusLoading"> · 中转状态读取中…</template><template v-else-if="relayStatusError"> · 中转状态读取失败</template><template v-else-if="relayStatusKnown"> · {{ serverPagination ? "本页" : "" }}已接码未入中转 {{ relayMissingCount }}</template></span>
        </div>

        <!-- 批量操作栏 -->
        <div class="bulkbar account-actions">
          <button class="btn primary" type="button" :disabled="!selected.length || busy || reauthProgress.running" title="把选中账号带入邮箱换绑页" @click="emit('rebind', selected)"><ArrowLeftRight :size="14" aria-hidden="true" />换绑（{{ selected.length }}）</button>
          <label class="checkline"><input v-model="allPageSelected" type="checkbox" />全选本页</label>
          <button class="btn secondary" type="button" :disabled="!selected.length || busy" title="取消所有分页中已选择的账号" @click="clearAllSelected"><X :size="14" aria-hidden="true" />取消（{{ selected.length }}）</button>
          <button class="btn primary" :disabled="!accountTotal || busy" :title="selected.length ? '下载选中的账号' : '下载全部账号'" @click="emit('download', selected.length ? selected : null)"><Download :size="14" aria-hidden="true" />{{ selected.length ? `下载选中(${selected.length})` : "下载全部" }}</button>
          <button class="btn secondary" :disabled="!selectedAccessTokenKeys.length || busy || copyingAt" title="复制选中账号的完整 Access Token" @click="emit('copy-selected-at', selected)"><Copy :size="14" aria-hidden="true" />{{ copyingAt ? "读取AT中" : "复制选中AT" }}</button>
          <button class="btn secondary" type="button" :disabled="!selected.length || busy || codexOauthRunning" title="对选中账号接码并查看批量日志" @click="emit('codex-oauth', selected)"><Smartphone :size="14" aria-hidden="true" />{{ codexOauthRunning ? "接码中…" : `接码(${selected.length})` }}</button>
          <button class="btn secondary" type="button" :disabled="!selectedRelaySyncKeys.length || busy || codexOauthRunning" :title="selectedRelaySyncKeys.length ? `使用已保存的 AT/RT 补齐 ${selectedRelaySyncKeys.length} 个账号的中转 JSON，不重新登录或取号` : '请先选择已接码且持有 RT 的账号'" @click="emit('sync-relay', selectedRelaySyncKeys)"><Cable :size="14" aria-hidden="true" />{{ codexOauthRunning ? "处理中…" : `补同步中转(${selectedRelaySyncKeys.length})` }}</button>
          <button class="btn secondary" type="button" :disabled="busy || codexOauthRunning" title="粘贴 邮箱----密码----2FA 导入新账号" @click="emit('import-accounts')"><Upload :size="14" aria-hidden="true" />导入账号</button>
          <button class="btn secondary" :disabled="(!selectedRefreshAtKeys.length && !atRiskCount) || busy || refreshAtProgress.running" :title="selectedRefreshAtKeys.length ? `刷新选中 ${selectedRefreshAtKeys.length} 个账号的 AT（无Session自动跳过）` : `刷新全部待处理账号（测活失败或AT过期/缺失共 ${atRiskCount} 个，无Session自动跳过已封号）`" @click="emit('refresh-at', selectedRefreshAtKeys.length ? { keys: selectedRefreshAtKeys } : { all_accounts: true })">{{ refreshAtProgress.running ? "刷新AT中…" : (selectedRefreshAtKeys.length ? `刷新AT选中(${selectedRefreshAtKeys.length})` : `刷新AT(${atRiskCount})`) }}</button>
          <button class="btn secondary" :disabled="!selectedAccessTokenKeys.length || busy" title="导出选中账号的完整 Access Token" @click="emit('export-selected-at', selected)">导出选中AT</button>
          <button class="btn secondary" :disabled="!selected.length || busy" title="导出选中账号、密码和完整 TOTP 密钥" @click="emit('export-security', selected)">导出账号+密码+2FA</button>
          <button class="btn secondary" :disabled="!accountTotal || busy || accountLoadBlocked || healthProgress.running" :title="selected.length ? '测活选中的账号' : '测活全部账号(跳过已封号)'" @click="emit('health', selected.length ? selected : null)">{{ healthProgress.running ? "测活中…" : (selected.length ? `测活选中(${selected.length})` : "测活全部") }}</button>
          <button class="btn secondary" :disabled="!accountTotal || busy || accountLoadBlocked" :title="selected.length ? '检测选中账号的 Free 资格' : '检测全部账号的 Free 资格'" @click="emit('trial-check', selected.length ? { keys: selected, force: false } : { all_accounts: true, force: false })">{{ selected.length ? `检测资格选中(${selected.length})` : "检测全部 Free 资格" }}</button>
          <button class="btn secondary" type="button" :disabled="!selected.length || busy || promoRunning" title="查询选中账号可用的优惠活动" @click="checkPromoSelected">{{ promoRunning ? "查询优惠中…" : `查优惠(${selected.length})` }}</button>
          <label class="checkline" title="优惠检测同时处理的账号数">优惠并发 <input v-model.number="promoConcurrency" type="number" min="1" step="1" aria-label="优惠检测并发数" style="width: 72px;" /></label>
          <span v-if="promoMessage" class="filter-result">{{ promoMessage }}</span>

          <button class="btn secondary" :disabled="(!selectedReauthKeys.length && !atRiskCount) || busy || reauthProgress.running" :title="selectedReauthKeys.length ? `重新登录选中的 ${selectedReauthKeys.length} 个账号（协议方式需有密码和2FA）` : `重新登录全部待处理账号(${atRiskCount})，与刷新AT同一口径`" @click="emit('reauth', { keys: selectedReauthKeys })">{{ reauthProgress.running ? "登录中…" : (selectedReauthKeys.length ? `重新登录选中(${selectedReauthKeys.length})` : `重新登录全部(${atRiskCount})`) }}</button>
          <button class="btn primary" :disabled="!selectedSecurityRepairKeys.length || busy || reauthProgress.running" @click="emit('security-repair', { keys: selectedSecurityRepairKeys })">补全安全字段（{{ selectedSecurityRepairKeys.length }}）</button>
          <button class="btn secondary" :disabled="!selectedSecurityOperationKeys.length || busy || reauthProgress.running" @click="emit('password-change', { keys: selectedSecurityOperationKeys })">修改密码（{{ selectedSecurityOperationKeys.length }}）</button>
          <button class="btn secondary" :disabled="!selectedSecurityOperationKeys.length || busy || reauthProgress.running" @click="emit('totp-change', { keys: selectedSecurityOperationKeys })">修改 2FA（{{ selectedSecurityOperationKeys.length }}）</button>
          
          <button class="btn danger" :disabled="!invalidCount || busy || selectionLoading" @click="deleteInvalid"><Trash2 :size="14" aria-hidden="true" />删除失效（{{ invalidCount }}）</button>
          <select v-model="moveTarget" aria-label="移动目标分组"><option v-for="group in groups" :key="group.id" :value="group.id">移至：{{ group.name }}</option></select>
          <button class="btn secondary" :disabled="!selected.length || !moveTarget || busy" @click="moveSelected">移动选中</button>
          <button class="btn danger" :disabled="!selected.length || busy" @click="emit('delete', selected)">删除选中</button>
          <button class="btn danger" :disabled="!accountTotal || busy" @click="emit('delete', null)">全部删除</button>
        </div>

        <!-- 账号表格：9 列分组布局 -->
        <div class="table-wrap acct-wrap">
          <table class="acct">
            <thead>
              <tr>
                <th class="col-check"></th>
                <th class="col-account">账号</th>
                <th class="col-group">分组 / 方式</th>
                <th class="col-status">状态</th>
                <th class="col-cred">凭据 · 点击复制</th>
                <th class="col-totp">验证码</th>
                <th class="col-meta">代理 / 时间</th>
                <th class="col-actions">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!pageItems.length">
                <td colspan="8" class="muted account-empty" style="text-align: center; padding: 32px;">没有匹配的账号记录</td>
              </tr>
              <tr v-for="item in pageItems" :key="item.delete_key">
                <!-- 选择框 -->
                <td data-label="选择">
                    <input v-model="selected" type="checkbox" :value="item.delete_key" :aria-label="`选择账号 ${item.email || item.account_id || ''}`" />
                </td>

                <!-- 账号：邮箱 + ID -->
                <td class="col-account" data-label="账号">
                  <div class="acct-cell-main">
                    <button
                      class="acct-email interactive-email"
                      type="button"
                      :title="`点击复制邮箱: ${item.email}`"
                      @click="copyTextDirect(item.email, `email_${item.delete_key}`)"
                    >
                      <strong>{{ item.email || "-" }}</strong>
                      <component :is="copiedFeedback[`email_${item.delete_key}`] ? Check : Copy" :size="12" aria-hidden="true" />
                    </button>
                    <div class="acct-id-row">
                      <button
                        v-if="item.account_id"
                        class="copy-pill-btn"
                        type="button"
                        :title="`点击复制 Account ID: ${item.account_id}`"
                        @click="copyTextDirect(item.account_id, `acc_id_${item.delete_key}`)"
                      >
                        <span class="acct-id-text">{{ item.account_id }}</span>
                        <component :is="copiedFeedback[`acc_id_${item.delete_key}`] ? Check : Copy" :size="11" aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                </td>

                <!-- 分组 / 注册方式 -->
                <td class="col-group" data-label="分组 / 方式">
                  <div class="acct-cell-stack">
                    <span class="acct-group-name" :title="item.group_name || '默认分组'">{{ item.group_name || "默认分组" }}</span>
                    <span class="acct-reg-source">{{ registrationText(item) }}</span>
                  </div>
                </td>

                <!-- 状态：套餐 + 测活 + 0元资格 -->
                <td class="col-status" data-label="状态">
                  <div class="acct-cell-stack">
                    <span :class="['capsule-badge', 'acct-plan', planType(item)]" :title="`测活识别套餐：${planName(item)}`">
                      {{ planName(item) }}
                    </span>
                    <span :class="['status-pill', statusClass(item)]">
                      <i></i>
                      {{ statusName(item) }}
                    </span>
                    <span :class="['status-pill', 'status-pill-sub', trialClass(item)]" :title="item.trial_eligibility_error || item.trial_eligibility_reason || ''">
                      <i></i>
                      {{ trialLabel(item) }}
                    </span>
                    <span v-if="item.health_error" class="acct-health-error" :title="item.health_error">{{ item.health_error }}</span>
                  </div>
                </td>

                <!-- 凭据：密码 / 2FA / AT -->
                <td class="col-cred" data-label="凭据">
                  <div class="acct-cell-stack">
                    <button
                      v-if="item.has_password"
                      class="copy-pill-btn cred-chip"
                      type="button"
                      title="点击直接复制完整密码"
                      @click="emit('copy-credential', item, 'password')"
                    >
                      <em>密码</em>
                      <span>{{ item.password_masked || "••••••••" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <span v-else class="cred-missing">密码缺失</span>
                    <button
                      v-if="item.has_totp"
                      class="copy-pill-btn cred-chip"
                      type="button"
                      title="点击直接复制完整 2FA 密钥"
                      @click="emit('copy-credential', item, 'totp_secret')"
                    >
                      <em>2FA</em>
                      <span>{{ item.totp_secret_masked || "已存秘钥" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <span v-else class="cred-missing">2FA 未设置</span>
                    <button
                      class="copy-pill-btn cred-chip"
                      type="button"
                      :disabled="!item.has_access_token || copyingAt"
                      :title="item.has_access_token ? '点击复制 Access Token' : '该账号没有 AT'"
                      @click="emit('copy-at', item)"
                    >
                      <em>AT</em>
                      <span>{{ !item.has_access_token ? "缺失" : copyingAt ? "读取中…" : "复制 Token" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <button
                      class="copy-pill-btn cred-chip"
                      type="button"
                      :disabled="!item.has_session_token || copyingAt"
                      :title="item.has_session_token ? '点击复制 Session Token' : '该账号没有 Session'"
                      @click="emit('copy-session', item)"
                    >
                      <em>Session</em>
                      <span>{{ !item.has_session_token ? "缺失" : copyingAt ? "读取中…" : "复制 Token" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <span :class="['sms-state-chip', item.has_refresh_token ? 'is-ok' : 'is-none']" :title="item.has_refresh_token ? '已接码：账号已持有 RT' : '未接码：账号还没有 RT，可在操作列点手机图标补发'">
                      <Smartphone :size="11" aria-hidden="true" />{{ item.has_refresh_token ? "已接码" : "未接码" }}
                    </span>
                    <span
                      v-if="item.has_refresh_token && relayStatusKnown"
                      :class="['sms-state-chip', accountHasRelay(item) ? 'is-ok' : 'is-none']"
                      :title="accountHasRelay(item) ? '中转已存在该账号凭据' : '账号已有 RT，但中转目录没有对应凭据，可用“已接码 · 未入中转”筛选后补同步'"
                    >
                      <Cable :size="11" aria-hidden="true" />{{ accountHasRelay(item) ? "已入中转" : "未入中转" }}
                    </span>
                    <small v-if="item.c2a_upload_status" :class="['capsule-badge', 'acct-c2a', item.c2a_upload_status === 'success' ? 'plus' : 'free']">
                      c2a {{ item.c2a_upload_status === 'success' ? '已上传' : '失败' }}
                    </small>
                  </div>
                </td>

                <!-- 2FA 实时验证码 -->
                <td class="col-totp" data-label="验证码">
                  <button
                    v-if="item.has_totp"
                    :class="['copy-pill-btn', 'totp-btn', { copied: copiedFeedback[`totp_code_${item.delete_key}`] }]"
                    type="button"
                    :disabled="totpLoading[item.delete_key]"
                    :title="totpEntry(item)?.error || '点击获取并复制 6 位实时 2FA 验证码'"
                    @click="handleTotpClick(item)"
                  >
                    <strong>{{ totpDisplay(item) }}</strong>
                    <component :is="copiedFeedback[`totp_code_${item.delete_key}`] ? Check : Copy" :size="11" aria-hidden="true" />
                  </button>
                  <span v-else class="cred-missing">-</span>
                </td>

                <!-- 代理 / 时间 -->
                <td class="col-meta" data-label="代理 / 时间">
                  <div class="acct-meta">
                    <span class="acct-proxy" :title="item.proxy || '本地美国'">{{ proxyDisplay(item.proxy).label }}</span>
                    <span class="acct-time" :title="`注册时间：${fmtTime(item.registered_at || item.updated_at)}`">
                      <em>注册</em>{{ fmtShortTime(item.registered_at || item.updated_at) }}
                    </span>
                    <small class="acct-time" :class="{ 'is-stale': isStale(lastRefreshAt(item)) }" :title="`最近刷新：${fmtTime(lastRefreshAt(item))}${isStale(lastRefreshAt(item)) ? '（已超过 3 天，数据可能失效）' : ''}`">
                      <em>刷新</em>{{ fmtShortTime(lastRefreshAt(item)) }}
                    </small>
                    <small
                      :class="['acct-time', { 'is-failed': lastInvalidAt(item) }]"
                      :title="lastInvalidAt(item) ? `最后失效：${fmtTime(lastInvalidAt(item))}` : '账号当前状态有效'"
                    >
                      <em>失效</em>{{ lastInvalidAt(item) ? fmtShortTime(lastInvalidAt(item)) : "-" }}
                    </small>
                  </div>
                </td>

                <!-- 操作按钮组 -->
                <td class="col-actions" data-label="操作">
                  <div class="account-row-actions">
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || refreshAtProgress.running || !item.delete_key || item.is_deactivated || !item.has_session_token"
                      title="刷新AT"
                      aria-label="刷新 Access Token"
                      @click="emit('refresh-at', { keys: [item.delete_key] })"
                    >
                      <RefreshCw :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || codexOauthRunning || !item.delete_key || item.is_deactivated"
                      title="接码"
                      aria-label="补发 RT"
                      @click="emit('codex-oauth', [item])"
                    >
                      <Smartphone :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || codexOauthRunning || !item.delete_key || item.is_deactivated || !item.has_refresh_token || !relayStatusKnown || accountHasRelay(item)"
                      :title="!relayStatusKnown ? '中转状态尚未成功读取' : accountHasRelay(item) ? '中转已有严格匹配凭据' : '补同步中转'"
                      aria-label="补同步中转"
                      @click="emit('sync-relay', [item])"
                    >
                      <Cable :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn primary row-icon-btn"
                      type="button"
                      :disabled="busy || reauthProgress.running || !item.delete_key || item.is_deactivated"
                      title="换绑邮箱"
                      aria-label="换绑邮箱"
                      @click="emit('rebind', [item.delete_key])"
                    >
                      <ArrowLeftRight :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || !item.delete_key"
                      title="查看账号操作日志"
                      aria-label="查看账号操作日志"
                      @click="emit('account-logs', item)"
                    >
                      <ClipboardList :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || !item.delete_key"
                      title="查看设备信息（注册环境 / 指纹）"
                      aria-label="查看设备信息"
                      @click="openDeviceInfo(item)"
                    >
                      <Monitor :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || reauthProgress.running || !item.delete_key || item.password_confirmed === false || !item.has_password || !item.has_totp || item.is_deactivated || (item.password_confirmed === undefined && item.security_repair_available && item.has_totp)"
                      title="修改密码"
                      aria-label="修改密码"
                      @click="emit('password-change', { keys: [item.delete_key], concurrency: 1 })"
                    >
                      <KeyRound :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || reauthProgress.running || !item.delete_key || item.password_confirmed === false || !item.has_password || !item.has_totp || item.is_deactivated || (item.password_confirmed === undefined && item.security_repair_available && item.has_totp)"
                      title="修改 TOTP 2FA"
                      aria-label="修改 TOTP 2FA"
                      @click="emit('totp-change', { keys: [item.delete_key], concurrency: 1 })"
                    >
                      <ShieldCheck :size="13" aria-hidden="true" />
                    </button>
                  </div>
                </td>
              </tr>
</tbody>
          </table>
        </div>

        <div class="account-page-footer">
          <div class="account-page-selection" aria-label="当前页选择操作">
            <button class="btn secondary" type="button" :disabled="!pageItems.length || allPageSelected || busy" title="选择当前页显示的全部账号，并保留其他页的选择" @click="selectCurrentPage"><CheckSquare :size="14" aria-hidden="true" />全选本页</button>
            <button class="btn secondary" type="button" :disabled="!selectedOnPageCount || busy" title="取消当前页账号的选择，并保留其他页的选择" @click="clearCurrentPage"><X :size="14" aria-hidden="true" />取消本页</button>
          </div>
          <AppPager v-model:page="page" :pages="pages" :total="filteredTotal" />
        </div>
      </div>
    </div>

    <Teleport to="body">
      <Transition name="dialog-fade">
        <div v-if="deviceDialog.open" class="app-dialog-mask" @click.self="closeDeviceInfo">
          <section class="app-dialog device-info-dialog" role="dialog" aria-modal="true" aria-label="设备信息">
            <div class="app-dialog-head">
              <div>
                <span class="app-dialog-kicker"><Monitor :size="12" aria-hidden="true" />设备信息</span>
                <h2>{{ deviceDialog.email }}</h2>
                <small>注册环境 · 当时使用的指纹快照</small>
              </div>
              <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="closeDeviceInfo"><X :size="16" aria-hidden="true" /></button>
            </div>
            <div class="device-info-body">
              <div v-if="deviceDialog.loading" class="empty-card">加载注册环境中…</div>
              <div v-else-if="deviceDialog.error" class="empty-card">加载失败：{{ deviceDialog.error }}</div>
              <div v-else-if="!deviceGroups.some((g) => g.rows.length)" class="empty-card">该账号没有注册环境记录（可能为浏览器注册或历史导入账号）</div>
              <template v-else>
                <p v-if="!deviceDialog.has_snapshot" class="device-info-note">该账号注册较早，以下信息由历史字段还原；新注册账号将保存完整指纹快照</p>
                <div class="device-info-grid">
                  <div v-for="group in deviceGroups" v-show="group.rows.length" :key="group.title" class="device-info-group">
                    <h3>{{ group.title }}</h3>
                    <div v-for="row in group.rows" :key="row[0]" class="device-kv">
                      <em>{{ row[0] }}</em>
                      <span :title="String(row[1])">{{ row[1] }}</span>
                    </div>
                  </div>
                </div>
              </template>
            </div>
            <div class="app-dialog-actions">
              <button class="btn secondary" type="button" @click="closeDeviceInfo">关闭</button>
            </div>
          </section>
        </div>
      </Transition>
    </Teleport>
  </section>
</template>
