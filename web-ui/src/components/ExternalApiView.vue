<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId, watch } from "vue";
import { api, jsonBody } from "../api.js";

// Shared by both shells: authentication is handled exclusively by ../api.js.
type Service = "upi" | "scan";
type Tab = "upi" | "public" | "scan";
type PaymentPlatform = "none" | "foarge" | "masi" | "seashore";
type Secret = "cdk" | "entry_proxy" | "exit_proxy" | "api_key" | "bearer_token" | "foarge_credential" | "masi_cdk_ticket" | "seashore_cdk" | "foarge_cdk" | "masi_cdk";
interface SecretField { key: Secret; label: string; hint: string }
type Settings = Record<Service, { base_url: string; [key: string]: string | boolean }>;
type JobStatus = "queued" | "submitting" | "running" | "done" | "failed" | "cancelled" | "unknown" | "cancelling";
interface Job {
  id: string; batch_id: string; position: number; status: JobStatus; job_id?: string;
  queue_position?: number; created_at?: number | string; updated_at?: number | string; error?: string; account_email?: string;
  payment_platform?: PaymentPlatform;
  result?: { payment_link?: string; scan_link?: string; provider_redirect_url?: string; verification_url?: string; qr_code?: string; payment_status?: string; payment_submit_status?: string; payment_submit_id?: string; payment_submit_error?: string; [key: string]: unknown };
  retryable: false;
}
interface ScanConfig { available: boolean; unit_price: string; max_batch: number; accepting_supplier_count: number }
interface Quote { available: boolean; unit_price: string; count: number; total_amount: string; max_batch: number }
interface Submission {
  id: string; status: "submitted" | "unknown" | "failed" | "submitting"; retryable: boolean; error?: string;
  retry_after?: number; updated_at?: number | string;
  data?: { batch_no?: string; order_count?: number; total_amount?: string; balance?: string; frozen_balance?: string; idempotent?: boolean; orders?: unknown[] };
}
interface ScanOrder {
  id?: string; order_no?: string; order_id?: string; status: string; link?: string;
  result?: unknown; failure_reason?: string; charge_amount?: number | string; unit_price?: number | string;
}
interface PageState<T> { items: T[]; total: number; page: number; pageSize: number; loading: boolean; loaded: boolean; error: string }
interface UpiDraft { request_id: string; sessions_text: string; use_promo: boolean; count: number; payment_platform: PaymentPlatform }
interface FuseDraft { codes: readonly string[]; label: string }
interface FuseResult { duplicate: boolean; transferred_points: string; target: { remaining_uses: string; total_uses?: string; enabled?: boolean } }
interface ScanPayload { request_id: string; links: string[]; unit_price: string }
type PipelineBatchSize = 10 | 20 | 30 | 50;
interface PipelineGroup { id: string; name: string; account_count?: number }
interface PipelinePreviewAccount { position: number; key: string; email: string; eligible: boolean; issue?: string }
interface PipelinePreview { group: { id: string; name: string }; batch_size: PipelineBatchSize; total_count: number; selected_count?: number; eligible_count: number; skipped_count: number; total_batches: number; selected_keys?: string[] | null; accounts?: PipelinePreviewAccount[]; skipped?: { position: number; email: string; issue: string }[]; skipped_truncated?: number }
interface PipelineItem { position: number; email: string; status: string; issue?: string; upi_status?: string; link?: string; scan_status?: string; error?: string; updated_at?: number | string }
interface PipelineTask { id: string; request_id: string; group_id: string; group_name: string; batch_size: PipelineBatchSize; status: string; unit_price: string; upi_provider?: "legacy" | "public"; scan_provider?: "cdk_scan" | "seashore" | "foarge" | "masi" | "auto"; total_count: number; selected_count?: number | null; eligible_count: number; skipped_count: number; total_batches: number; current_batch: number; error?: string; last_credits?: Record<string, unknown>; counts: { total: number; queued: number; extracting: number; extracted: number; scanning: number; submitted: number; failed: number; skipped: number; unknown: number; finished: number; not_selected: number }; created_at?: number | string; updated_at?: number | string }
type ProviderKey = "seashore" | "foarge" | "cdk_scan" | "masi";
interface ProviderCheckState { loading: boolean; checked: boolean; account: Record<string, unknown> | null; error: string }
interface ProviderMeta { key: ProviderKey; label: string; description: string; secret: Secret; endpoint: string; docs?: string; docsLabel?: string }

const uid = useId();
const activeTab = ref<Tab>("upi");
const visible = ref(typeof document === "undefined" || !document.hidden);
const tabs: { key: Tab; label: string }[] = [
  { key: "upi", label: "旧版 UPI 提链" },
  { key: "public", label: "公益 UPI 提链" },
  { key: "scan", label: "CDK-Scan 扫码" },
];
const services: { key: Service; label: string; url: string; fields: SecretField[] }[] = [
  { key: "upi", label: "旧版 UPI 提链", url: "https://upi.43-255-159-106.sslip.io", fields: [
    { key: "cdk", label: "CDK", hint: "提链额度凭据" },
    { key: "entry_proxy", label: "入口代理", hint: "必填，包含认证信息的完整代理地址" },
    { key: "exit_proxy", label: "出口代理", hint: "必填，包含认证信息的完整代理地址" },
  ] },
  { key: "scan", label: "CDK-Scan 扫码", url: "https://scan.fishmail.vip", fields: [
    { key: "api_key", label: "CDK-Scan API Key / CDK", hint: "CDK-Scan 报价、提交与查询订单必填" },
    { key: "bearer_token", label: "Bearer Token（可选）", hint: "仅查询余额时需要，API Key 单独没有余额接口" },
  ] },
];
const publicProxyFields: SecretField[] = [
  { key: "entry_proxy", label: "公益 UPI 入口代理", hint: "公益提链必填，包含认证信息的完整代理地址" },
  { key: "exit_proxy", label: "公益 UPI 出口代理", hint: "公益提链必填，包含认证信息的完整代理地址" },
];
const publisherSecretFields: SecretField[] = [
  { key: "seashore_cdk", label: "Seashore 发布者 CDK", hint: "公益批量任务提交到 Seashore 发布者 API" },
  { key: "foarge_cdk", label: "Foarge 发布者 PBK", hint: "公益批量任务提交到 Foarge 发布者 API" },
  { key: "masi_cdk", label: "Masi 发布者 CDK", hint: "公益批量任务提交到 Masi API" },
];
const paymentPlatforms: { key: PaymentPlatform; label: string; fields: SecretField[] }[] = [
  { key: "none", label: "只提链", fields: [] },
  { key: "foarge", label: "Foarge", fields: [{ key: "foarge_credential", label: "Foarge 凭证", hint: "与鉴权方式对应的 PBK / API Key / CDK Session" }] },
  { key: "masi", label: "Masi", fields: [{ key: "masi_cdk_ticket", label: "Masi CDK Ticket", hint: "自动支付所需的 CDK Ticket" }] },
  { key: "seashore", label: "Seashore", fields: [{ key: "seashore_cdk", label: "Seashore CDK", hint: "自动支付所需的 CDK" }] },
];
const foargeAuthModes = [{ key: "pbk", label: "PBK" }, { key: "api_key", label: "API Key" }, { key: "cdk_session", label: "CDK Session" }];
const seashoreApiBase = "https://seashore.lol/api/publisher";
const upiDefaults = {
  payment_platform: "none" as PaymentPlatform, foarge_auth_mode: "pbk", foarge_api_base: "https://foarge.com/api/publisher/v1",
  masi_api_base: "https://masi.cc.cd", masi_require_online_workers: true,
};
const upiOptions = reactive({ ...upiDefaults });
const settings = ref<Settings | null>(null);
const settingsLoading = ref(false);
const settingsSaving = ref<Service | "">("");
const settingsError = ref("");
const settingsNotice = ref("");
const secrets = reactive<Record<Secret, string>>({ cdk: "", entry_proxy: "", exit_proxy: "", api_key: "", bearer_token: "", foarge_credential: "", masi_cdk_ticket: "", seashore_cdk: "", foarge_cdk: "", masi_cdk: "" });
const clearSecrets = reactive<Record<Secret, boolean>>({ cdk: false, entry_proxy: false, exit_proxy: false, api_key: false, bearer_token: false, foarge_credential: false, masi_cdk_ticket: false, seashore_cdk: false, foarge_cdk: false, masi_cdk: false });
const providerMeta: ProviderMeta[] = [
  { key: "seashore", label: "Seashore 发布者 API", description: "提交邮箱、Access Token 和支付链接。", secret: "seashore_cdk", endpoint: "/api/external/seashore/check", docs: "https://seashore.lol/publisher-api", docsLabel: "接口文档" },
  { key: "foarge", label: "Foarge 发布方 API", description: "先创建 UPI 任务，再提交 Access Token 和支付长链接。", secret: "foarge_cdk", endpoint: "/api/external/foarge/check", docs: "https://foarge.com/publisher/publisher-docs.html", docsLabel: "接口文档" },
  { key: "cdk_scan", label: "CDK-Scan API", description: "提交支付链接和对应 AT，使用任务级幂等键防止重复扣费。", secret: "api_key", endpoint: "/api/external/cdk-scan/check", docs: "https://wzyp.cn/shop/L8JH75L1", docsLabel: "购买 / 配置" },
  { key: "masi", label: "Masi UPI API", description: "通过 X-CDK 提交支付链接、完整 AT 和邮箱。", secret: "masi_cdk", endpoint: "/api/external/masi/check", docs: "https://masi.cc.cd/api-docs", docsLabel: "接口文档" },
];
const providerChecks = reactive<Record<ProviderKey, ProviderCheckState>>({
  seashore: { loading: false, checked: false, account: null, error: "" },
  foarge: { loading: false, checked: false, account: null, error: "" },
  cdk_scan: { loading: false, checked: false, account: null, error: "" },
  masi: { loading: false, checked: false, account: null, error: "" },
});
const providerTargets: { key: Exclude<PipelineTask["scan_provider"], "auto" | undefined> | "auto"; label: string }[] = [
  { key: "auto", label: "自动切换" }, { key: "seashore", label: "Seashore" }, { key: "foarge", label: "Foarge" }, { key: "cdk_scan", label: "CDK-Scan" }, { key: "masi", label: "Masi" },
];
const providerStatusBusy = computed(() => providerMeta.some(({ key }) => providerChecks[key].loading));
const settingsBusy = computed(() => settingsLoading.value || !!settingsSaving.value);
const savedUpiOptions = computed(() => ({
  payment_platform: (settings.value?.upi.payment_platform ?? upiDefaults.payment_platform) as PaymentPlatform,
  foarge_auth_mode: String(settings.value?.upi.foarge_auth_mode ?? upiDefaults.foarge_auth_mode),
  foarge_api_base: String(settings.value?.upi.foarge_api_base ?? upiDefaults.foarge_api_base),
  masi_api_base: String(settings.value?.upi.masi_api_base ?? upiDefaults.masi_api_base),
  masi_require_online_workers: (settings.value?.upi.masi_require_online_workers ?? upiDefaults.masi_require_online_workers) === true,
}));
function serviceSecretFields(service: Service) {
  return [...services.find((item) => item.key === service)!.fields,
    ...(service === "upi" ? paymentPlatforms.flatMap((platform) => platform.fields) : publisherSecretFields)];
}
function visibleSecretFields(service: Service) {
  return [...services.find((item) => item.key === service)!.fields, ...(service === "upi" ? paymentPlatforms.find((platform) => platform.key === upiOptions.payment_platform)?.fields || [] : [])];
}
function serviceUrl(service: { key: Service; url: string }) {
  if (service.key === "scan") return String(settings.value?.scan.cdk_scan_api_base || service.url);
  return service.url;
}
function providerInfo(key: ProviderKey) { return providerMeta.find((item) => item.key === key)!; }
function providerConfigured(key: ProviderKey) {
  const field = providerInfo(key).secret;
  return Boolean(settings.value?.scan?.[`has_${field}`]);
}
function providerValue(account: Record<string, unknown> | null, keys: string[]) {
  if (!account) return undefined;
  const containers: unknown[] = [account, account.ticket, account.account, account.data, account.cdk];
  for (const container of containers) {
    if (!container || typeof container !== "object") continue;
    for (const key of keys) {
      const value = (container as Record<string, unknown>)[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
  }
  return undefined;
}
function providerCount(value: unknown) {
  if (value === undefined || value === null || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString("zh-CN") : String(value);
}
function providerConnection(key: ProviderKey) {
  const state = providerChecks[key];
  if (state.loading) return "验证中…";
  if (state.account) return "已连接";
  return providerConfigured(key) ? "未连接" : "未配置";
}
function providerMetric(key: ProviderKey, index: number) {
  const account = providerChecks[key].account;
  if (!account) return "—";
  if (key === "seashore") {
    if (index === 0) return providerCount(providerValue(account, ["remaining_uses", "available_uses", "uses_remaining"]));
    return `${providerCount(providerValue(account, ["uses_held", "uses_reserved", "pending_uses"]))} / ${providerCount(providerValue(account, ["uses_consumed", "used_uses"]))}`;
  }
  if (key === "foarge") {
    if (index === 0) return providerCount(providerValue(account, ["uses_remaining", "remaining_uses", "available_uses", "balance", "available"]));
    return String(providerValue(account, ["status", "task_status"]) || "可提交");
  }
  if (key === "cdk_scan") return index === 0 ? providerCount(providerValue(account, ["total_orders"])) : "单条幂等";
  return index === 0
    ? providerCount(providerValue(account, ["available_uses", "remaining_uses", "uses_remaining"]))
    : providerCount(providerValue(account, ["pending_uses", "uses_held", "uses_reserved"]));
}
function providerMetricLabels(key: ProviderKey) {
  if (key === "seashore") return ["剩余次数", "已占用 / 已消耗"];
  if (key === "foarge") return ["剩余次数", "任务状态"];
  if (key === "cdk_scan") return ["历史订单", "提交方式"];
  return ["可用次数", "预占次数"];
}
const upiOptionsDirty = computed(() => (Object.keys(upiDefaults) as (keyof typeof upiDefaults)[]).some((key) => upiOptions[key] !== savedUpiOptions.value[key]));
const upiSettingsDirty = computed(() => upiOptionsDirty.value || serviceSecretFields("upi").some(({ key }) => !!secrets[key].trim() || clearSecrets[key]));
const upiReady = computed(() => !!settings.value?.upi.has_cdk && !!settings.value?.upi.has_entry_proxy && !!settings.value?.upi.has_exit_proxy);
const upiPaymentReady = computed(() => {
  const platform = paymentPlatforms.find((item) => item.key === savedUpiOptions.value.payment_platform);
  return !!platform && platform.fields.every(({ key }) => !!settings.value?.upi[`has_${key}`]);
});
const backgroundPaused = computed(() => settingsBusy.value || upiBusy.value || fuseBusy.value);
const settingsActionBusy = computed(() => backgroundPaused.value || providerStatusBusy.value || scanBusy.value || !!retrying.value || !!jobBusy.value || quoteBusy.value || creditsBusy.value || balanceBusy.value);
const scanReady = computed(() => !!settings.value?.scan.has_api_key);
const terminalJobs = new Set(["done", "failed", "cancelled", "unknown"]);
const activeOrders = new Set(["pending", "claimed", "processing"]);
const statusLabels: Record<string, string> = {
  queued: "排队中", submitting: "提交中", running: "提链中", done: "已完成", failed: "失败", cancelled: "已取消",
  unknown: "结果未知", cancelling: "取消中", submitted: "已提交", pending: "待接单", claimed: "已接单", processing: "处理中",
  completed: "已完成", success: "成功", paid: "已支付", expired: "已过期",
  paused: "已暂停", stopping: "暂停中", extracting: "提链中", extracted: "待扫码", scanning: "扫码提交中",
};
let disposed = false;
let mounted = false;
let timer: ReturnType<typeof setTimeout> | undefined;
let settingsController: AbortController | undefined;
let settingsVersion = 0;
let configController: AbortController | undefined;
let configVersion = 0;
let pipelineTimer: ReturnType<typeof setTimeout> | undefined;

function errorText(cause: unknown) { return cause instanceof Error ? cause.message : "请求失败，请稍后手动重试"; }
function errorStatus(cause: unknown) { return Number((cause as { status?: number })?.status) || 0; }
function validationFailure(cause: unknown) { return [400, 402, 403, 409, 413, 422].includes(errorStatus(cause)); }
function localTime(value: number | string | undefined) {
  if (value === undefined || value === null || value === "") return "—";
  const numeric = typeof value === "number" || /^\d+(?:\.\d+)?$/.test(value);
  const date = new Date(numeric ? Number(value) * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "—";
}
function showValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}
function statusLabel(status: string) { return statusLabels[status] || status || "—"; }
function statusTone(status: string) {
  return ["done", "submitted", "completed", "success", "paid"].includes(status) ? "success" :
    ["failed", "unknown"].includes(status) ? "warning" : "neutral";
}
function paymentPlatformLabel(platform: string = "none") { return paymentPlatforms.find((item) => item.key === platform)?.label || `未知平台（${platform}）`; }
function paymentPlan(platform: PaymentPlatform) {
  return platform === "none" ? "只提链，不自动支付。" : `${paymentPlatformLabel(platform)}：提链后会自动提交支付；提交成功后仍需确认最终到账状态。`;
}
function paymentSubmitLabel(status?: string) {
  const labels: Record<string, string> = { idle: "尚未提交", pending: "等待提交", queued: "排队中", submitting: "提交中", submitted: "已提交，待确认到账", success: "已提交，待确认到账", succeeded: "已提交，待确认到账", failed: "提交失败", error: "提交失败", unknown: "提交结果未知", skipped: "未提交" };
  return status ? labels[status] || `未知状态（${status}）` : "暂无提交状态";
}
function paymentStatusLabel(status?: string) {
  const labels: Record<string, string> = { paid: "已到账", completed: "已到账", awaiting_payment: "等待支付", unpaid: "未支付", pending: "待确认到账", processing: "支付处理中", submitted: "已提交，待确认到账", activation_pending: "支付后等待激活", verification_unavailable: "暂未核实到账", failed: "支付失败", expired: "已过期", payment_timeout: "支付超时", plus_not_received: "尚未到账", unknown: "到账结果未知" };
  return status ? labels[status] || `未知状态（${status}）` : "尚未确认到账";
}
function newRequestId() {
  // randomUUID is absent on many HTTP deployments; getRandomValues also works there.
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function safeHttpUrl(value: unknown) {
  if (typeof value !== "string" || !value.trim() || /[\\\u0000-\u0020\u007f]/.test(value.trim())) return "";
  try {
    const url = new URL(value.trim());
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}
function supportedScanLink(value: unknown) {
  const safe = safeHttpUrl(value);
  if (!safe) return "";
  const url = new URL(safe);
  if (safe.length > 2048 || (url.port && !["80", "443"].includes(url.port))) return "";
  const stripe = url.hostname === "payments.stripe.com" && url.pathname.startsWith("/upi/instructions/") && url.pathname.length > "/upi/instructions/".length;
  const nicepay = ["pay.nicepay.co.kr", "www.pay.nicepay.co.kr"].includes(url.hostname) && url.pathname.length > 1;
  return stripe || nicepay ? safe : "";
}
function resultScanLink(result: Job["result"]) {
  for (const link of [result?.scan_link, result?.provider_redirect_url, result?.verification_url, result?.payment_link]) {
    const supported = supportedScanLink(link);
    if (supported) return supported;
  }
  return "";
}
function qrImageUrl(value: unknown) {
  if (typeof value !== "string") return "";
  // SVG is rendered only as an image, never inserted as HTML or an inline SVG.
  if (/^data:image\/(?:png|svg\+xml);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) || /^data:image\/svg\+xml(?:;charset=utf-8|;utf8)?,\S+$/i.test(value)) return value;
  return safeHttpUrl(value);
}
const qrErrors = reactive<Record<string, boolean>>({});

const pipelineGroups = ref<PipelineGroup[]>([]);
const pipelineGroupId = ref("");
const pipelineBatchSize = ref<PipelineBatchSize>(20);
const pipelineUnitPrice = ref("");
const pipelinePreview = ref<PipelinePreview | null>(null);
const pipelineSelectedKeys = ref<string[]>([]);
const pipelineSelectionInitialized = ref(false);
const pipelineSelectionDirty = ref(false);
const pipelineAccountPage = ref(1);
const pipelineAccountPageSize = ref(10);
const pipelineAccountPages = computed(() => Math.max(1, Math.ceil((pipelinePreview.value?.accounts?.length || 0) / pipelineAccountPageSize.value)));
const pipelineAccountPageItems = computed(() => {
  const accounts = pipelinePreview.value?.accounts || [];
  const start = (pipelineAccountPage.value - 1) * pipelineAccountPageSize.value;
  return accounts.slice(start, start + pipelineAccountPageSize.value);
});
const pipelineAccountRange = computed(() => {
  const total = pipelinePreview.value?.accounts?.length || 0;
  if (!total) return "0 / 0";
  const start = (pipelineAccountPage.value - 1) * pipelineAccountPageSize.value + 1;
  return `${start}-${Math.min(total, start + pipelineAccountPageSize.value - 1)} / ${total}`;
});
const pipelineTask = ref<PipelineTask | null>(null);
const pipelineItems = ref<PipelineItem[]>([]);
const pipelineBusy = ref(false);
const pipelineError = ref("");
const pipelineNotice = ref("");
const pipelineGroupsLoading = ref(false);
const pipelineGroupsLoaded = ref(false);
const pipelineUpiProvider = ref<"legacy" | "public">("public");
const pipelineScanProvider = ref<"cdk_scan" | "seashore" | "foarge" | "masi" | "auto">("cdk_scan");
const pipelineTaskBusy = computed(() => pipelineBusy.value || !!pipelineTask.value && ["queued", "running", "stopping"].includes(pipelineTask.value.status));
const pipelineUpiReady = computed(() => pipelineUpiProvider.value === "public"
  ? !!settings.value?.upi.has_entry_proxy && !!settings.value?.upi.has_exit_proxy
  : upiReady.value);
const pipelineScanReady = computed(() => pipelineScanProvider.value === "seashore"
  ? !!settings.value?.scan.has_seashore_cdk
  : pipelineScanProvider.value === "foarge"
    ? !!settings.value?.scan.has_foarge_cdk
    : pipelineScanProvider.value === "masi"
      ? !!settings.value?.scan.has_masi_cdk
      : pipelineScanProvider.value === "auto"
        ? !!settings.value?.scan.has_seashore_cdk || !!settings.value?.scan.has_foarge_cdk || !!settings.value?.scan.has_api_key || !!settings.value?.scan.has_masi_cdk
        : scanReady.value);
const pipelineFinished = computed(() => {
  const counts = pipelineTask.value?.counts;
  return counts ? counts.finished >= counts.total : false;
});

function stopPipelineTimer() {
  if (pipelineTimer !== undefined) clearTimeout(pipelineTimer);
  pipelineTimer = undefined;
}
function schedulePipelinePoll() {
  stopPipelineTimer();
  if (disposed || !pipelineTask.value || !["queued", "running", "stopping"].includes(pipelineTask.value.status)) return;
  pipelineTimer = setTimeout(() => { pipelineTimer = undefined; void loadPipelineStatus(); }, 3000);
}
function pipelineEligibleKeys() {
  return (pipelinePreview.value?.accounts || []).filter((item) => item.eligible).map((item) => item.key);
}
const pipelineEligibleKeysValue = computed(() => pipelineEligibleKeys());
const pipelinePageEligibleKeys = computed(() => pipelineAccountPageItems.value.filter((item) => item.eligible).map((item) => item.key));
const pipelinePageSelectedCount = computed(() => {
  const selected = new Set(pipelineSelectedKeys.value);
  return pipelinePageEligibleKeys.value.filter((key) => selected.has(key)).length;
});
const pipelinePageAllSelected = computed(() => pipelinePageEligibleKeys.value.length > 0 && pipelinePageSelectedCount.value === pipelinePageEligibleKeys.value.length);
const pipelineGroupAllSelected = computed(() => {
  const keys = pipelineEligibleKeysValue.value;
  const selected = new Set(pipelineSelectedKeys.value);
  return keys.length > 0 && keys.every((key) => selected.has(key));
});
function pipelineSelectionForRequest(): string[] | undefined {
  if (!pipelineSelectionInitialized.value) return undefined;
  // Keep the user's explicit selection even when it contains every eligible
  // account.  This is what prevents an accidental whole-group extraction and
  // keeps the server task scoped to exactly what was checked in the picker.
  return [...pipelineSelectedKeys.value];
}
function applyPipelineResponse(value: any) {
  if (!value?.task) return;
  pipelineTask.value = value.task as PipelineTask;
  pipelineItems.value = Array.isArray(value.items) ? value.items : [];
  schedulePipelinePoll();
}
async function loadPipelineGroups() {
  if (disposed || pipelineGroupsLoading.value) return;
  pipelineGroupsLoading.value = true;
  pipelineError.value = "";
  try {
    const result = await api("/api/account-groups") as { items?: PipelineGroup[] };
    if (disposed) return;
    pipelineGroups.value = Array.isArray(result.items) ? result.items : [];
    if (!pipelineGroups.value.some((group) => group.id === pipelineGroupId.value)) pipelineGroupId.value = pipelineGroups.value[0]?.id || "";
    pipelineGroupsLoaded.value = true;
  } catch (cause) {
    if (!disposed) pipelineError.value = errorText(cause);
  } finally {
    if (!disposed) pipelineGroupsLoading.value = false;
  }
}
async function previewPipeline() {
  if (pipelineBusy.value) return;
  if (!pipelineGroupsLoaded.value) await loadPipelineGroups();
  if (!pipelineGroupId.value) { pipelineError.value = "请先选择账号分组。"; return; }
  if (pipelineSelectionInitialized.value && pipelineSelectedKeys.value.length === 0) { pipelineError.value = "请至少选择一个可处理账号。"; return; }
  pipelineBusy.value = true; pipelineError.value = ""; pipelineNotice.value = "";
  try {
    const body: Record<string, unknown> = { group_id: pipelineGroupId.value, batch_size: pipelineBatchSize.value };
    const selected = pipelineSelectionForRequest();
    if (selected !== undefined) body.account_keys = selected;
    pipelinePreview.value = await api("/api/external/pipeline/preview", { method: "POST", body: jsonBody(body) }) as PipelinePreview;
    pipelineAccountPage.value = 1;
    if (!pipelineSelectionInitialized.value) {
      // A preview must never silently select the whole group.  The operator
      // explicitly chooses a page or the whole group with the controls below.
      pipelineSelectedKeys.value = [];
      pipelineSelectionInitialized.value = true;
    }
    pipelineSelectionDirty.value = false;
  } catch (cause) { if (!disposed) pipelineError.value = errorText(cause); }
  finally { if (!disposed) pipelineBusy.value = false; }
}
async function loadPipelineStatus() {
  const taskId = pipelineTask.value?.id;
  if (disposed || !taskId || pipelineBusy.value) return;
  try {
    const result = await api(`/api/external/pipeline/status?task_id=${encodeURIComponent(taskId)}&page=1&page_size=100&selected_only=1`);
    if (!disposed) applyPipelineResponse(result);
  } catch (cause) {
    if (!disposed) { pipelineError.value = errorText(cause); stopPipelineTimer(); }
  }
}
async function startPipeline() {
  if (pipelineBusy.value) return;
  // The batch surface is deliberately the public UPI surface.  Legacy UPI
  // remains available only in the separate manual tab, so a stale draft or
  // an older browser cannot silently submit a legacy task from here.
  pipelineUpiProvider.value = "public";
  if (pipelineSelectionDirty.value || !pipelinePreview.value || pipelinePreview.value.group.id !== pipelineGroupId.value || pipelinePreview.value.batch_size !== pipelineBatchSize.value) {
    await previewPipeline();
    if (!pipelinePreview.value) return;
  }
  if (!pipelinePreview.value.eligible_count) { pipelineError.value = "当前分组没有可提交的完整账号。"; return; }
  if (pipelineSelectedKeys.value.length === 0) { pipelineError.value = "请至少选择一个可处理账号。"; return; }
  pipelineBusy.value = true; pipelineError.value = ""; pipelineNotice.value = "";
  try {
    const body: Record<string, unknown> = {
      request_id: newRequestId(), group_id: pipelineGroupId.value, batch_size: pipelineBatchSize.value,
      use_promo: usePromo.value, unit_price: pipelineUnitPrice.value.trim(),
      upi_provider: "public", scan_provider: pipelineScanProvider.value,
    };
    const selected = pipelineSelectionForRequest();
    if (selected !== undefined) body.account_keys = selected;
    const result = await api("/api/external/pipeline/start", { method: "POST", body: jsonBody(body) });
    applyPipelineResponse(result);
    pipelinePreview.value = null;
    pipelineNotice.value = "整组任务已启动，失败账号会记录后继续处理下一批。";
  } catch (cause) { if (!disposed) pipelineError.value = errorText(cause); }
  finally { if (!disposed) pipelineBusy.value = false; }
}
function resetPipelineSelection() {
  pipelinePreview.value = null;
  pipelineSelectedKeys.value = [];
  pipelineSelectionInitialized.value = false;
  pipelineSelectionDirty.value = false;
  pipelineAccountPage.value = 1;
}
function togglePipelineAccount(key: string, checked: boolean) {
  const next = new Set(pipelineSelectedKeys.value);
  if (checked) next.add(key); else next.delete(key);
  pipelineSelectedKeys.value = [...next];
  pipelineSelectionDirty.value = true;
}
function togglePipelinePage(checked: boolean) {
  const next = new Set(pipelineSelectedKeys.value);
  for (const key of pipelinePageEligibleKeys.value) {
    if (checked) next.add(key); else next.delete(key);
  }
  pipelineSelectedKeys.value = [...next];
  pipelineSelectionDirty.value = true;
}
function togglePipelineGroup(checked: boolean) {
  pipelineSelectedKeys.value = checked ? [...pipelineEligibleKeysValue.value] : [];
  pipelineSelectionDirty.value = true;
}
function onPipelineGroupChange() { resetPipelineSelection(); }
function changePipelineAccountPage(delta: number) {
  pipelineAccountPage.value = Math.max(1, Math.min(pipelineAccountPages.value, pipelineAccountPage.value + delta));
}
function changePipelineAccountPageSize() {
  pipelineAccountPage.value = 1;
}
async function pipelineAction(action: "stop" | "resume") {
  const taskId = pipelineTask.value?.id;
  if (!taskId || pipelineBusy.value) return;
  pipelineBusy.value = true; pipelineError.value = "";
  try {
    const result = await api(`/api/external/pipeline/${encodeURIComponent(taskId)}/${action}`, { method: "POST", body: jsonBody({}) });
    applyPipelineResponse(result);
    pipelineNotice.value = action === "stop" ? "已请求暂停，当前远端回执核对完成后停止继续提交。" : "任务已恢复。";
  } catch (cause) { if (!disposed) pipelineError.value = errorText(cause); }
  finally { if (!disposed) pipelineBusy.value = false; }
}
function onPipelineToggle(event: Event) {
  if ((event.target as HTMLDetailsElement).open && !pipelineGroupsLoaded.value) void loadPipelineGroups();
}

async function loadSettings(resetUpiOptions = false) {
  if (disposed) return;
  const preserveDraft = !resetUpiOptions && !!settings.value && upiSettingsDirty.value;
  settingsController?.abort();
  settingsController = new AbortController();
  const version = ++settingsVersion;
  settingsLoading.value = true;
  settingsError.value = "";
  try {
    const result = await api("/api/external/settings", { signal: settingsController.signal }) as Settings;
    if (disposed || version !== settingsVersion) return;
    settings.value = result;
    if (!preserveDraft) Object.assign(upiOptions, savedUpiOptions.value);
  } catch (cause) {
    if (disposed || version !== settingsVersion) return;
    settings.value = null;
    settingsError.value = errorText(cause);
  } finally {
    if (!disposed && version === settingsVersion) settingsLoading.value = false;
  }
}
async function refreshSettings() {
  if (settingsActionBusy.value) return;
  stopBackground();
  ++scanRevision;
  scanQuote.value = null;
  upiConfirmation.value = null;
  credits.value = null;
  balance.value = null;
  await loadSettings();
  await loadActive();
}
async function saveSettings(service: Service) {
  if (disposed || settingsActionBusy.value) return;
  const patch: Record<string, string | boolean> = {};
  for (const field of serviceSecretFields(service)) {
    if (clearSecrets[field.key]) patch[`clear_${field.key}`] = true;
    else if (secrets[field.key].trim()) patch[field.key] = secrets[field.key].trim();
  }
  settingsNotice.value = "";
  if (!Object.keys(patch).length && (service !== "upi" || !upiOptionsDirty.value)) { settingsNotice.value = "没有配置变更，空输入会沿用已保存值。"; return; }
  if (service === "upi") {
    if (!paymentPlatforms.some((item) => item.key === upiOptions.payment_platform) || !foargeAuthModes.some((item) => item.key === upiOptions.foarge_auth_mode)) {
      settingsError.value = "请选择有效的支付平台和 Foarge 鉴权方式。"; return;
    }
    if ((upiOptions.payment_platform === "foarge" && !safeHttpUrl(upiOptions.foarge_api_base)) || (upiOptions.payment_platform === "masi" && !safeHttpUrl(upiOptions.masi_api_base))) {
      settingsError.value = "支付平台 API 地址需要有效的 HTTP(S) 地址。"; return;
    }
    // The fixed Seashore base is display-only and is never sent as an override.
    Object.assign(patch, upiOptions);
  }
  settingsSaving.value = service;
  settingsError.value = "";
  stopBackground();
  ++scanRevision;
  scanQuote.value = null;
  upiConfirmation.value = null;
  try {
    await api("/api/external/settings", { method: "PUT", body: jsonBody({ [service]: patch }) });
    if (disposed) return;
    for (const field of serviceSecretFields(service)) {
      secrets[field.key] = "";
      clearSecrets[field.key] = false;
    }
    credits.value = null;
    balance.value = null;
    scanConfig.value = null;
    settingsNotice.value = "配置已保存，输入已清空。留空可继续沿用已保存凭据。";
    await loadSettings(service === "upi");
  } catch (cause) {
    if (!disposed) settingsError.value = errorText(cause);
  } finally {
    if (!disposed) {
      settingsSaving.value = "";
      await loadActive();
    }
  }
}

async function verifyProvider(key: ProviderKey) {
  if (disposed || providerChecks[key].loading || backgroundPaused.value || scanBusy.value || !!retrying.value || !!jobBusy.value || quoteBusy.value || creditsBusy.value || balanceBusy.value) return;
  const meta = providerInfo(key);
  const state = providerChecks[key];
  state.loading = true;
  state.error = "";
  try {
    const draft = secrets[meta.secret].trim();
    if (draft) {
      await api("/api/external/settings", { method: "PUT", body: jsonBody({ scan: { [meta.secret]: draft } }) });
      clearSecrets[meta.secret] = false;
      await loadSettings();
    }
    if (!providerConfigured(key)) throw new Error("请先填写并保存该平台凭据。");
    const result = await api(meta.endpoint, { method: "POST", body: jsonBody({}) }) as { account?: Record<string, unknown> };
    state.account = (result?.account && typeof result.account === "object" ? result.account : result) as Record<string, unknown>;
    state.checked = true;
  } catch (cause) {
    state.account = null;
    state.checked = false;
    state.error = errorText(cause);
  } finally {
    state.loading = false;
  }
}

async function clearProvider(key: ProviderKey) {
  if (disposed || providerChecks[key].loading || backgroundPaused.value || scanBusy.value || !!retrying.value || !!jobBusy.value || quoteBusy.value || creditsBusy.value || balanceBusy.value) return;
  const meta = providerInfo(key);
  const state = providerChecks[key];
  state.loading = true;
  state.error = "";
  try {
    await api("/api/external/settings", { method: "PUT", body: jsonBody({ scan: { [`clear_${meta.secret}`]: true } }) });
    secrets[meta.secret] = "";
    clearSecrets[meta.secret] = false;
    state.account = null;
    state.checked = false;
    await loadSettings();
  } catch (cause) {
    state.error = errorText(cause);
  } finally {
    state.loading = false;
  }
}

async function refreshProviderStatuses() {
  if (disposed || settingsActionBusy.value) return;
  await Promise.allSettled(providerMeta.filter(({ key }) => providerConfigured(key)).map(({ key }) => verifyProvider(key)));
}

function createPager<T>(service: Service, path: string, readPage: (data: any) => { items: T[]; total: number }, allowed = () => true) {
  const state = reactive({ items: [] as T[], total: 0, page: 1, pageSize: 20, loading: false, loaded: false, error: "" }) as PageState<T>;
  const pageCount = computed(() => Math.max(1, Math.ceil(state.total / state.pageSize)));
  let controller: AbortController | undefined;
  let version = 0;
  function cancelRead() { ++version; controller?.abort(); state.loading = false; }
  async function load(refresh = false) {
    if (disposed || backgroundPaused.value || !visible.value || activeTab.value !== service || !allowed() || state.loading) return;
    const current = ++version;
    controller = new AbortController();
    state.loading = true;
    state.error = "";
    let correctedPage = false;
    try {
      const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
      if (service === "upi") params.set("refresh", String(refresh && !!settings.value?.upi.has_cdk));
      const data = readPage(await api(`${path}?${params}`, { signal: controller.signal }));
      if (disposed || current !== version) return;
      state.total = Math.max(0, Number(data.total) || 0);
      if (state.page > pageCount.value) {
        state.page = pageCount.value;
        state.items = [];
        correctedPage = true;
      } else {
        state.items = data.items || [];
        state.loaded = true;
      }
    } catch (cause) {
      if (!disposed && current === version) state.error = errorText(cause);
    } finally {
      if (!disposed && current === version) {
        state.loading = false;
        if (correctedPage) await load(refresh);
      }
    }
  }
  async function go(page: number) {
    if (backgroundPaused.value || state.loading || jobBusy.value || scanBusy.value || retrying.value || !Number.isInteger(page) || page < 1 || page > pageCount.value || page === state.page) return;
    state.page = page;
    state.items = [];
    state.loaded = false;
    await load(true);
  }
  return { state, pageCount, load, go, cancelRead };
}
const jobPager = createPager<Job>("upi", "/api/external/upi/jobs", (data) => data);
const jobs = jobPager.state;
const orderPager = createPager<ScanOrder>("scan", "/api/external/scan/orders", (data) => ({ items: data.items, total: data.pagination.total }), () => scanReady.value);
const orders = orderPager.state;
const submissionPager = createPager<Submission>("scan", "/api/external/scan/submissions", (data) => data, () => scanReady.value);
const submissions = submissionPager.state;
const historyOpen = ref(false);

// Canonicalize by access token so a raw token and its Session JSON are one job.
function parseSessions(text: string) {
  const fail = (error: string) => ({ sessions_text: "", count: 0, duplicates: 0, error });
  const input = text.trim();
  if (!input) return fail("");
  let entries: unknown[];
  try {
    const parsed = JSON.parse(input);
    entries = Array.isArray(parsed) ? parsed : [parsed];
  } catch {
    if (input.startsWith("[")) return fail("Session JSON 数组格式有误，请检查括号与逗号。");
    entries = input.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  }
  const tokens = new Set<string>();
  let duplicates = 0;
  for (let index = 0; index < entries.length; index++) {
    let entry = entries[index];
    if (typeof entry === "string" && /^[{\["]/.test(entry.trim())) {
      try { entry = JSON.parse(entry); } catch { return fail(`第 ${index + 1} 条 Session JSON 格式有误。`); }
    }
    const value = typeof entry === "string" ? entry :
      entry && typeof entry === "object" && !Array.isArray(entry) ? (entry as any).accessToken || (entry as any).access_token : null;
    const token = typeof value === "string" ? value.trim() : "";
    if (!token || /\s/.test(token)) return fail(`第 ${index + 1} 条缺少有效的 accessToken / access_token 或独立 token。`);
    if (tokens.has(token)) duplicates++;
    else tokens.add(token);
  }
  if (tokens.size < 1 || tokens.size > 100) return fail("每批需要 1–100 条去重后的 Session。");
  return { sessions_text: [...tokens].join("\n"), count: tokens.size, duplicates, error: "" };
}
const upiText = ref("");
const usePromo = ref(true);
const upiInput = computed(() => parseSessions(upiText.value));
const upiConfirmation = ref<Readonly<UpiDraft> | null>(null);
const upiBusy = ref(false);
const upiUnknown = ref(false);
const upiReconciled = ref(false);
const upiReceipt = ref<{ request_id: string; batch_id?: string; count: number; payment_platform: PaymentPlatform } | null>(null);
const upiError = ref("");
const upiNotice = ref("");
const upiLocked = computed(() => upiBusy.value || upiUnknown.value || !!upiConfirmation.value);
const credits = ref<Record<string, unknown> | null>(null);
const creditsBusy = ref(false);
const creditsError = ref("");
async function loadCredits() {
  if (disposed || creditsBusy.value || backgroundPaused.value || !settings.value?.upi.has_cdk) return;
  creditsBusy.value = true;
  creditsError.value = "";
  try {
    const result = await api("/api/external/upi/credits", { method: "POST", body: jsonBody({}) });
    if (!disposed) credits.value = result.data;
  } catch (cause) { if (!disposed) creditsError.value = errorText(cause); }
  finally { if (!disposed) creditsBusy.value = false; }
}
function prepareUpi() {
  if (disposed || upiLocked.value || settingsActionBusy.value || !upiReady.value) return;
  upiError.value = "";
  upiNotice.value = "";
  if (upiSettingsDirty.value) { upiError.value = "UPI 配置有未保存更改，请先保存配置，再核对本批支付平台。"; return; }
  if (!upiPaymentReady.value) { upiError.value = "请先保存所选支付平台的凭证。"; return; }
  if (upiInput.value.error || !upiInput.value.count) { upiError.value = upiInput.value.error || "请先输入 Session。"; return; }
  try {
    upiConfirmation.value = Object.freeze({ request_id: newRequestId(), sessions_text: upiInput.value.sessions_text, use_promo: usePromo.value, count: upiInput.value.count, payment_platform: savedUpiOptions.value.payment_platform });
  } catch { upiError.value = "浏览器随机数服务不可用，请更换浏览器后提交。"; }
}
function cancelUpiConfirmation() { if (!upiBusy.value) upiConfirmation.value = null; }
async function confirmUpi() {
  const draft = upiConfirmation.value;
  if (disposed || !draft || upiUnknown.value || settingsActionBusy.value || !upiReady.value || !upiPaymentReady.value) return;
  if (upiSettingsDirty.value || draft.payment_platform !== savedUpiOptions.value.payment_platform) {
    upiError.value = "UPI 配置已更改，请先保存配置，再重新核对本批支付平台。"; return;
  }
  upiBusy.value = true;
  upiError.value = "";
  stopBackground();
  upiReceipt.value = { request_id: draft.request_id, count: draft.count, payment_platform: draft.payment_platform };
  try {
    const result = await api("/api/external/upi/jobs", { method: "POST", body: jsonBody({ request_id: draft.request_id, sessions_text: draft.sessions_text, use_promo: draft.use_promo, payment_platform: draft.payment_platform }) });
    if (disposed) return;
    if (result?.ok !== true || !result.batch_id) throw new Error("提交回执不完整，请核对远端任务。");
    upiReceipt.value = { request_id: draft.request_id, count: draft.count, batch_id: result.batch_id, payment_platform: draft.payment_platform };
    upiNotice.value = `${draft.count > 1 ? "已批量提交到提链平台" : "已提交到提链平台"}：${draft.count} 条。${paymentPlan(draft.payment_platform)}`;
    upiText.value = "";
  } catch (cause) {
    if (!disposed) {
      // A POST timeout can still mean the remote job exists. Never replay it.
      upiUnknown.value = !validationFailure(cause);
      upiError.value = validationFailure(cause)
        ? `${errorText(cause)} 本次提交校验失败，请修正后重新核对计费。`
        : `${errorText(cause)} 提交结果未知，请在提链服务核对，不要重新提交这些 Session。`;
    }
  } finally {
    if (!disposed) {
      upiConfirmation.value = null;
      upiBusy.value = false;
      await loadActive();
    }
  }
}
function finishUpiReconciliation() {
  if (!upiUnknown.value || !upiReconciled.value || upiBusy.value) return;
  upiText.value = "";
  upiUnknown.value = false;
  upiReconciled.value = false;
  upiError.value = "";
  upiNotice.value = "已结束本次核对。新批次请使用尚未提交的 Session。";
}

const fuseText = ref("");
const fuseLabel = ref("");
const fuseDraft = ref<Readonly<FuseDraft> | null>(null);
const fuseBusy = ref(false);
const fuseUnknown = ref(false);
const fuseResult = ref<FuseResult | null>(null);
const fuseError = ref("");
const fuseLocked = computed(() => fuseBusy.value || !!fuseDraft.value);
const fuseInput = computed(() => {
  const codes = fuseText.value.split(/\r?\n/).map((code) => code.trim()).filter(Boolean);
  const label = fuseLabel.value.trim();
  const error = codes.length && (codes.length < 2 || codes.length > 100) ? "每次融合需要 2–100 张 CDK，每行一张。" :
    codes.some((code) => /\s/.test(code)) ? "每行请只填写一张完整 CDK。" :
    new Set(codes).size !== codes.length ? "CDK 存在重复，请检查保留卡和源卡。" :
    label.length > 120 ? "标签最多 120 个字符。" : "";
  return { codes, label, error };
});
function maskFuseTarget(code: string) { return code.length > 8 ? `${code.slice(0, 3)}••••${code.slice(-3)}` : "••••"; }
function validFuseAmount(value: unknown): value is number | string {
  return (typeof value === "number" || (typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value))) && Number.isFinite(Number(value)) && Number(value) >= 0;
}
function prepareFuse() {
  if (disposed || fuseLocked.value || settingsActionBusy.value) return;
  fuseError.value = "";
  fuseResult.value = null;
  if (fuseInput.value.error || fuseInput.value.codes.length < 2) { fuseError.value = fuseInput.value.error || "请填写至少两张 CDK。"; return; }
  fuseDraft.value = Object.freeze({ codes: Object.freeze([...fuseInput.value.codes]), label: fuseInput.value.label });
}
function editFuse() {
  if (disposed || fuseBusy.value) return;
  if (fuseDraft.value) {
    fuseText.value = fuseDraft.value.codes.join("\n");
    fuseLabel.value = fuseDraft.value.label;
  }
  fuseDraft.value = null;
  fuseUnknown.value = false;
  fuseError.value = "";
  fuseResult.value = null;
}
async function confirmFuse() {
  const draft = fuseDraft.value;
  if (disposed || !draft || settingsActionBusy.value) return;
  fuseBusy.value = true;
  fuseError.value = "";
  stopBackground();
  try {
    const result = await api("/api/external/upi/fuse", { method: "POST", body: jsonBody({ codes: draft.codes, label: draft.label }) });
    if (disposed) return;
    const target = result?.target;
    if (result?.ok !== true || typeof result.duplicate !== "boolean" || !validFuseAmount(result.transferred_points) || !validFuseAmount(target?.remaining_uses)) {
      throw new Error("融合回执不完整。");
    }
    fuseResult.value = {
      duplicate: result.duplicate, transferred_points: String(result.transferred_points),
      target: { remaining_uses: String(target.remaining_uses),
        ...(validFuseAmount(target.total_uses) ? { total_uses: String(target.total_uses) } : {}),
        ...(typeof target.enabled === "boolean" ? { enabled: target.enabled } : {}),
      },
    };
    fuseText.value = "";
    fuseLabel.value = "";
    fuseDraft.value = null;
    fuseUnknown.value = false;
  } catch (cause) {
    if (!disposed) {
      const detail = draft.codes.reduce((message, code) => message.split(code).join(maskFuseTarget(code)), errorText(cause));
      fuseUnknown.value = fuseUnknown.value || (cause as { code?: string })?.code === "FUSION_UNKNOWN" || !validationFailure(cause);
      fuseError.value = fuseUnknown.value
        ? `${detail} 融合结果未知，原组合与顺序已保留。可手动重试同一组合；更换组合前请先核对上次结果。`
        : `${detail} 本次融合校验失败，请检查后重新确认。`;
    }
  } finally {
    if (!disposed) { fuseBusy.value = false; await loadActive(); }
  }
}
const jobBusy = ref("");
const jobActionError = ref("");
const pendingCancel = ref("");
function canCancel(job: Job) { return !terminalJobs.has(job.status) && job.status !== "cancelling"; }
async function jobAction(job: Job, action: "refresh" | "cancel") {
  if (disposed || jobBusy.value || jobs.loading || backgroundPaused.value || (action === "cancel" && (pendingCancel.value !== job.id || !canCancel(job)))) return;
  jobBusy.value = job.id;
  jobActionError.value = "";
  try {
    const result = await api(`/api/external/upi/jobs/${encodeURIComponent(job.id)}/${action}`, { method: "POST", body: jsonBody({}) }) as Job;
    if (disposed) return;
    const index = jobs.items.findIndex((item) => item.id === job.id);
    if (index >= 0) jobs.items.splice(index, 1, result);
    pendingCancel.value = "";
  } catch (cause) { if (!disposed) jobActionError.value = errorText(cause); }
  finally { if (!disposed) jobBusy.value = ""; }
}

const scanConfig = ref<ScanConfig | null>(null);
const configLoading = ref(false);
const configError = ref("");
const scanText = ref("");
const quoteBusy = ref(false);
const scanQuote = ref<{ quote: Quote; links: string[]; revision: number } | null>(null);
const scanBusy = ref(false);
const scanError = ref("");
const scanNotice = ref("");
const scanAttempt = ref<Submission | null>(null);
const scanPayload = ref<Readonly<ScanPayload> | null>(null);
const scanMissing = ref(false);
const retrying = ref("");
const retryDeadlines = reactive<Record<string, number>>({});
const retryClock = ref(Date.now());
let retryTimer: ReturnType<typeof setTimeout> | undefined;
function retrySeconds(id: string) { return Math.max(0, Math.ceil(((retryDeadlines[id] || 0) - retryClock.value) / 1000)); }
function stopRetryClock() { if (retryTimer !== undefined) clearTimeout(retryTimer); retryTimer = undefined; }
function scheduleRetryClock() {
  stopRetryClock();
  retryClock.value = Date.now();
  if (mounted && !disposed && !backgroundPaused.value && visible.value && activeTab.value === "scan" && Object.values(retryDeadlines).some((deadline) => deadline > retryClock.value)) {
    retryTimer = setTimeout(scheduleRetryClock, 1000);
  }
}
function rememberRetryDelay(submission: Submission) {
  if (["submitted", "failed"].includes(submission.status)) { delete retryDeadlines[submission.id]; return; }
  if (!(submission.retry_after > 0)) return;
  const timestamp = typeof submission.updated_at === "number" || /^\d+(?:\.\d+)?$/.test(String(submission.updated_at))
    ? Number(submission.updated_at) * 1000 : Date.parse(String(submission.updated_at));
  retryDeadlines[submission.id] = (Number.isFinite(timestamp) ? timestamp : Date.now()) + submission.retry_after * 1000;
}
watch(() => [activeTab.value, visible.value, backgroundPaused.value, ...Object.values(retryDeadlines)], scheduleRetryClock, { flush: "sync" });
const scanLocked = computed(() => scanBusy.value || !!retrying.value || ["unknown", "submitting"].includes(scanAttempt.value?.status || ""));
let scanRevision = 0;
watch(scanText, () => { ++scanRevision; scanQuote.value = null; }, { flush: "sync" });
const scanInput = computed(() => {
  const lines = scanText.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const links: string[] = [];
  for (let index = 0; index < lines.length; index++) {
    const link = supportedScanLink(lines[index]);
    if (!link) return { links: [], duplicates: 0, error: `第 ${index + 1} 行需要 Stripe UPI 或 nicepay 的 http(s) 链接。` };
    if (!links.includes(link)) links.push(link);
  }
  const max = scanConfig.value?.max_batch;
  return { links, duplicates: lines.length - links.length, error: max && links.length > max ? `当前平台每批最多 ${max} 条。` : "" };
});
async function loadScanConfig() {
  if (disposed || backgroundPaused.value || !scanReady.value || configLoading.value || activeTab.value !== "scan" || !visible.value) return;
  const version = ++configVersion;
  configController = new AbortController();
  configLoading.value = true;
  configError.value = "";
  try {
    const result = await api("/api/external/scan/config", { signal: configController.signal }) as ScanConfig;
    if (disposed || version !== configVersion) return;
    scanConfig.value = result;
    scanQuote.value = null;
  } catch (cause) {
    if (!disposed && version === configVersion) { scanConfig.value = null; configError.value = errorText(cause); }
  } finally { if (!disposed && version === configVersion) configLoading.value = false; }
}
async function quoteScan() {
  if (disposed || quoteBusy.value || scanLocked.value || backgroundPaused.value || !scanReady.value || !scanConfig.value?.available || configLoading.value) return;
  scanError.value = "";
  scanNotice.value = "";
  scanQuote.value = null;
  if (scanInput.value.error || !scanInput.value.links.length) { scanError.value = scanInput.value.error || "请先输入付款链接。"; return; }
  const links = [...scanInput.value.links];
  const revision = scanRevision;
  quoteBusy.value = true;
  try {
    const quote = await api("/api/external/scan/quote", { method: "POST", body: jsonBody({ links }) }) as Quote;
    if (disposed || revision !== scanRevision) return;
    if (!quote.available) { scanError.value = "平台当前未开放接单，请稍后刷新平台状态。"; scanConfig.value = { ...scanConfig.value!, available: false }; return; }
    if (quote.count !== links.length || quote.count < 1 || quote.count > quote.max_batch || typeof quote.unit_price !== "string" || !/^\d+(?:\.\d+)?$/.test(quote.unit_price) || !/^\d+(?:\.\d+)?$/.test(String(quote.total_amount))) {
      scanError.value = "报价内容不完整或数量不一致，请重新获取报价。"; return;
    }
    scanQuote.value = { quote, links, revision };
  } catch (cause) { if (!disposed && revision === scanRevision) scanError.value = errorText(cause); }
  finally { if (!disposed) quoteBusy.value = false; }
}
async function confirmScan() {
  const confirmed = scanQuote.value;
  if (disposed || !confirmed || confirmed.revision !== scanRevision || scanLocked.value || backgroundPaused.value || !scanReady.value || !confirmed.quote.available || !scanConfig.value?.available) return;
  try {
    const payload = { request_id: newRequestId(), links: [...confirmed.links], unit_price: confirmed.quote.unit_price };
    Object.freeze(payload.links);
    scanPayload.value = Object.freeze(payload);
    scanQuote.value = null;
    await sendScan(payload.request_id, () => api("/api/external/scan/orders", { method: "POST", body: jsonBody(payload) }));
  } catch (cause) { if (!disposed) scanError.value = errorText(cause); }
}
async function sendScan(id: string, send: () => Promise<any>, mode: "create" | "retry" = "create") {
  if (scanBusy.value || backgroundPaused.value || disposed) return;
  scanBusy.value = true;
  scanError.value = "";
  scanNotice.value = "";
  scanMissing.value = false;
  scanAttempt.value = { id, status: "submitting", retryable: false };
  try {
    const response = await send();
    if (disposed) return;
    const submission = response?.submission as Submission;
    if (!submission || submission.id !== id || !["submitted", "unknown", "failed", "submitting"].includes(submission.status)) throw new Error("提交回执不完整，请使用原提交编号核对。");
    scanAttempt.value = submission;
    rememberRetryDelay(submission);
    if (submission.status === "submitted") {
      scanText.value = "";
      scanPayload.value = null;
      scanNotice.value = `扫码订单已提交${submission.data?.batch_no ? `，批次 ${submission.data.batch_no}` : ""}。`;
    } else if (submission.status === "failed") {
      scanPayload.value = null;
      scanError.value = submission.error || "本次业务提交失败，修正后请重新报价，使用新的提交编号。";
    }
  } catch (cause) {
    if (!disposed) {
      if (validationFailure(cause)) {
        scanAttempt.value = { id, status: "failed", retryable: false, error: errorText(cause) };
        scanPayload.value = null;
        scanError.value = `${errorText(cause)} 请修正后重新获取报价。`;
      } else {
        scanAttempt.value = { id, status: "unknown", retryable: true, error: errorText(cause) };
        if (errorStatus(cause) === 429) {
          const stated = Number((cause as { retry_after?: number }).retry_after) || Number(errorText(cause).match(/(\d+)\s*(?:秒|seconds?)/i)?.[1]) || 60;
          retryDeadlines[id] = Date.now() + Math.max(1, stated) * 1000;
        }
        // Only an explicit 404 from the retry endpoint proves no local record exists.
        scanMissing.value = mode === "retry" && errorStatus(cause) === 404 && scanPayload.value?.request_id === id;
      }
    }
  } finally {
    if (!disposed) {
      scanBusy.value = false;
      historyOpen.value = true;
      await Promise.allSettled([orderPager.load(), submissionPager.load()]);
    }
  }
}
async function retryCurrentScan() {
  const payload = scanPayload.value;
  if (disposed || !payload || scanAttempt.value?.id !== payload.request_id || scanAttempt.value.status !== "unknown" || !scanAttempt.value.retryable || retrySeconds(payload.request_id) > 0 || scanBusy.value || retrying.value || backgroundPaused.value || !scanReady.value) return;
  // The create endpoint only returns an existing unknown record. Retry its stored intent.
  await sendScan(payload.request_id, () => api(`/api/external/scan/submissions/${encodeURIComponent(payload.request_id)}/retry`, { method: "POST", body: jsonBody({}) }), "retry");
}
async function resendMissingScan() {
  const payload = scanPayload.value;
  if (disposed || !scanMissing.value || !payload || scanAttempt.value?.id !== payload.request_id || scanAttempt.value.status !== "unknown" || retrySeconds(payload.request_id) > 0 || scanBusy.value || retrying.value || backgroundPaused.value || !scanReady.value) return;
  // A second explicit click after a confirmed 404 may send the original frozen body.
  await sendScan(payload.request_id, () => api("/api/external/scan/orders", { method: "POST", body: jsonBody(payload) }));
}
async function retrySubmission(submission: Submission) {
  if (disposed || submission.status !== "unknown" || !submission.retryable || retrySeconds(submission.id) > 0 || scanBusy.value || retrying.value || backgroundPaused.value || !scanReady.value || (scanLocked.value && scanAttempt.value?.id !== submission.id)) return;
  retrying.value = submission.id;
  scanQuote.value = null;
  if (scanAttempt.value?.id !== submission.id) scanPayload.value = null;
  try {
    await sendScan(submission.id, () => api(`/api/external/scan/submissions/${encodeURIComponent(submission.id)}/retry`, { method: "POST", body: jsonBody({}) }), "retry");
  } finally { if (!disposed) retrying.value = ""; }
}
function reconcileScanHistory() {
  submissions.items.forEach(rememberRetryDelay);
  const found = submissions.items.find((item) => item.id === scanAttempt.value?.id);
  if (!found || scanBusy.value || retrying.value || !["unknown", "submitting"].includes(scanAttempt.value?.status || "")) return;
  scanAttempt.value = found;
  scanMissing.value = false;
  if (["submitted", "failed"].includes(found.status)) {
    scanPayload.value = null;
    if (found.status === "submitted") { scanText.value = ""; scanNotice.value = "提交记录已确认本次扫码提交成功。"; }
  }
}
watch(() => submissions.items, reconcileScanHistory);
async function toggleHistory(event: Event) {
  historyOpen.value = (event.target as HTMLDetailsElement).open;
  if (historyOpen.value) await submissionPager.load();
}
const balance = ref<{ balance: string; frozen_balance: string; unit_price: string } | null>(null);
const balanceBusy = ref(false);
const balanceError = ref("");
async function loadBalance() {
  if (disposed || balanceBusy.value || backgroundPaused.value || !settings.value?.scan.has_bearer_token) return;
  balanceBusy.value = true;
  balanceError.value = "";
  try {
    const result = await api("/api/external/scan/balance", { method: "POST", body: jsonBody({}) });
    if (!disposed) balance.value = result;
  } catch (cause) { if (!disposed) balanceError.value = errorText(cause); }
  finally { if (!disposed) balanceBusy.value = false; }
}
async function fillScan(value: string) {
  const link = supportedScanLink(value);
  if (!link || scanLocked.value || quoteBusy.value) { copyNotice.value = "扫码输入正在处理，或此链接不是受支持的 Stripe UPI / nicepay 链接。"; return; }
  scanText.value = link;
  scanQuote.value = null;
  scanNotice.value = "已填入提链结果。获取报价并确认后才会提交扫码。";
  await selectTab("scan");
}

const copyNotice = ref("");
const manualCopy = ref("");
const manualCopyInput = ref<HTMLTextAreaElement | null>(null);
async function copyText(value: string) {
  if (!value) return;
  copyNotice.value = "";
  manualCopy.value = "";
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(value);
    if (!disposed) copyNotice.value = "已复制。";
    return;
  } catch { /* HTTP and denied clipboard access use the selection fallback. */ }
  const previous = document.activeElement as HTMLElement | null;
  const input = document.createElement("textarea");
  input.value = value;
  input.readOnly = true;
  input.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
  document.body.appendChild(input);
  try {
    input.focus();
    input.select();
    if (!document.execCommand("copy")) throw new Error("copy failed");
    copyNotice.value = "已复制。";
  } catch {
    manualCopy.value = value;
    copyNotice.value = "浏览器未授予复制权限，请在下方选中文本复制。";
  } finally { input.remove(); previous?.focus?.(); }
  if (manualCopy.value) { await nextTick(); manualCopyInput.value?.focus(); manualCopyInput.value?.select(); }
}

function stopTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined; }
function stopBackground() {
  stopTimer();
  stopPipelineTimer();
  stopRetryClock();
  jobPager.cancelRead(); orderPager.cancelRead(); submissionPager.cancelRead();
  ++configVersion; configController?.abort(); configLoading.value = false;
}
function schedulePoll() {
  stopTimer();
  if (!mounted || disposed || !visible.value || backgroundPaused.value) return;
  if (activeTab.value === "upi") {
    if (jobs.loading || jobs.error || jobBusy.value || !settings.value?.upi.has_cdk || !jobs.items.some((job) => !terminalJobs.has(job.status))) return;
    timer = setTimeout(() => { timer = undefined; void jobPager.load(true); }, 10000);
  } else if (activeTab.value === "scan") {
    if (!scanReady.value || orders.loading || orders.error || scanBusy.value || retrying.value || !orders.items.some((order) => activeOrders.has(order.status))) return;
    timer = setTimeout(() => { timer = undefined; void orderPager.load(); }, 15000);
  }
}
watch(() => [activeTab.value, visible.value, backgroundPaused.value, scanReady.value, settings.value?.upi.has_cdk, jobs.loading, jobs.error, jobs.items.map((job) => job.status).join(","),
  orders.loading, orders.error, orders.items.map((order) => order.status).join(","), jobBusy.value, scanBusy.value, retrying.value], schedulePoll, { flush: "sync" });
async function loadActive() {
  if (disposed || backgroundPaused.value || !visible.value) return;
  if (activeTab.value === "upi") {
    await jobPager.load(true);
    if (pipelineTask.value) await loadPipelineStatus();
  } else if (activeTab.value === "public") {
    await refreshProviderStatuses();
    if (pipelineTask.value) await loadPipelineStatus();
  } else if (scanReady.value) await Promise.allSettled([loadScanConfig(), orderPager.load(), ...(historyOpen.value ? [submissionPager.load()] : [])]);
  schedulePoll();
  scheduleRetryClock();
}
async function selectTab(tab: Tab) {
  if (tab === activeTab.value || disposed) return;
  stopBackground();
  activeTab.value = tab;
  pendingCancel.value = "";
  await loadActive();
}
async function onVisibilityChange() {
  visible.value = !document.hidden;
  if (!visible.value) stopBackground();
  else await loadActive();
}
onMounted(async () => {
  mounted = true;
  document.addEventListener("visibilitychange", onVisibilityChange);
  await loadSettings();
  await loadActive();
});
onBeforeUnmount(() => {
  disposed = true;
  mounted = false;
  stopBackground();
  ++settingsVersion; settingsController?.abort();
  document.removeEventListener("visibilitychange", onVisibilityChange);
  for (const key of Object.keys(secrets) as Secret[]) secrets[key] = "";
  upiText.value = "";
  upiConfirmation.value = null;
  fuseText.value = "";
  fuseLabel.value = "";
  fuseDraft.value = null;
});
</script>

<template>
  <section class="external-api" :aria-labelledby="`${uid}-title`">
    <header class="ext-heading">
      <div><h2 :id="`${uid}-title`">外部 API</h2><p>旧版 UPI 提链、公益 UPI 批量提链、CDK-Scan 扫码分开管理。</p></div>
      <button class="ext-button" type="button" :disabled="settingsActionBusy" @click="refreshSettings">{{ settingsLoading ? '读取配置中…' : '刷新配置' }}</button>
    </header>
    <div class="ext-tabs" role="tablist" aria-label="外部 API 服务">
      <button v-for="tab in tabs" :id="`${uid}-tab-${tab.key}`" :key="tab.key" class="ext-button" :class="{ 'is-active': activeTab === tab.key }" type="button" role="tab" :aria-selected="activeTab === tab.key" :aria-controls="`${uid}-panel-${tab.key}`" @click="selectTab(tab.key)">{{ tab.label }}</button>
    </div>
    <p v-if="settingsError" class="ext-error" role="alert">配置读取 / 保存失败：{{ settingsError }}</p>
    <p v-if="settingsNotice" class="ext-notice" role="status">{{ settingsNotice }}</p>
    <p v-if="copyNotice" class="ext-notice" role="status">{{ copyNotice }}</p>
    <label v-if="manualCopy" class="ext-field">手动复制<textarea ref="manualCopyInput" :value="manualCopy" rows="2" readonly /></label>

    <details v-for="service in services" v-show="activeTab === service.key" :key="service.key" class="ext-config" :open="service.key === 'scan' ? true : !!settings && !upiReady">
      <summary>{{ service.label }} · 配置 <span>{{ service.key === 'upi' ? (upiReady ? '已配置' : '需要 CDK 与入口 / 出口代理') : (scanReady ? '已配置' : '需要 CDK-Scan API Key / CDK') }}</span></summary>
      <div class="ext-config-body">
        <label class="ext-field">{{ service.key === 'scan' ? 'CDK-Scan API 地址（固定）' : '服务地址（固定）' }}<input :value="serviceUrl(service)" readonly /></label>
        <p class="ext-muted">只展示已保存凭据的掩码。输入留空沿用原值；勾选清除后保存才会移除。</p>
        <p v-if="service.key === 'scan'" class="ext-muted">这里仅配置 CDK-Scan 自己的 API Key / CDK 和可选 Bearer Token。公益批量任务所用的 Seashore、Foarge、Masi 发布者 CDK 在“公益 UPI 提链”页单独配置。</p>
        <template v-if="service.key === 'upi'">
          <label class="ext-field" :for="`${uid}-payment-platform`">自动支付平台
            <select :id="`${uid}-payment-platform`" v-model="upiOptions.payment_platform" :disabled="backgroundPaused">
              <option v-for="platform in paymentPlatforms" :key="platform.key" :value="platform.key">{{ platform.label }}{{ platform.key === 'none' ? '（默认）' : ' · 自动支付' }}</option>
            </select>
          </label>
          <p class="ext-muted">{{ paymentPlan(upiOptions.payment_platform) }} 切换平台会保留其他平台已保存的凭证。</p>
          <div v-if="upiOptions.payment_platform === 'foarge'" class="ext-secret-grid">
            <label class="ext-field" :for="`${uid}-foarge-auth`">Foarge 鉴权方式
              <select :id="`${uid}-foarge-auth`" v-model="upiOptions.foarge_auth_mode" :disabled="backgroundPaused"><option v-for="mode in foargeAuthModes" :key="mode.key" :value="mode.key">{{ mode.label }}</option></select>
            </label>
            <label class="ext-field">Foarge API 地址<input v-model.trim="upiOptions.foarge_api_base" type="url" :disabled="backgroundPaused" autocomplete="off" /></label>
          </div>
          <template v-else-if="upiOptions.payment_platform === 'masi'">
            <label class="ext-field">Masi API 地址<input v-model.trim="upiOptions.masi_api_base" type="url" :disabled="backgroundPaused" autocomplete="off" /></label>
            <label class="ext-check"><input v-model="upiOptions.masi_require_online_workers" type="checkbox" :disabled="backgroundPaused" />要求有在线支付人员</label>
          </template>
          <label v-else-if="upiOptions.payment_platform === 'seashore'" class="ext-field">Seashore API 地址（固定）<input :value="seashoreApiBase" readonly /></label>
        </template>
        <div class="ext-secret-grid">
          <div v-for="field in visibleSecretFields(service.key)" :key="field.key" class="ext-secret">
            <label class="ext-field" :for="`${uid}-${field.key}`">{{ field.label }} <small>{{ settings?.[service.key]?.[`has_${field.key}`] ? `已保存 ${settings[service.key][`${field.key}_masked`] || '••••'}` : '未保存' }}</small></label>
            <input :id="`${uid}-${field.key}`" v-model="secrets[field.key]" type="password" autocomplete="new-password" autocapitalize="off" :spellcheck="false" :disabled="backgroundPaused || clearSecrets[field.key]" :placeholder="field.hint" />
            <label class="ext-check"><input v-model="clearSecrets[field.key]" type="checkbox" :disabled="backgroundPaused" />清除已保存的{{ field.label }}</label>
          </div>
        </div>
        <p v-if="service.key === 'upi' && upiSettingsDirty" class="ext-warning">UPI 配置有未保存更改，请先保存配置再创建任务。</p>
        <div class="ext-actions"><button class="ext-button ext-primary" type="button" :disabled="settingsActionBusy" @click="saveSettings(service.key)">{{ settingsSaving === service.key ? '保存中…' : '保存配置' }}</button></div>
      </div>
    </details>

    <details v-if="activeTab === 'public'" class="ext-config" :open="!!settings && !(settings?.upi.has_entry_proxy && settings?.upi.has_exit_proxy)">
      <summary>公益 UPI · 配置 <span>{{ settings?.upi.has_entry_proxy && settings?.upi.has_exit_proxy ? '入口 / 出口代理已配置' : '需要入口 / 出口代理' }}</span></summary>
      <div class="ext-config-body">
        <label class="ext-field">公益 UPI 服务地址（固定）<input :value="serviceUrl(services[0])" readonly /></label>
        <p class="ext-rule">公益提链不需要旧版 UPI CDK，只使用入口代理和出口代理。旧版 UPI 的 CDK 与手动提链请到“旧版 UPI 提链”页操作。</p>
        <p class="ext-muted">只展示已保存凭据的掩码。输入留空沿用原值；勾选清除后保存才会移除。</p>
        <div class="ext-secret-grid">
          <div v-for="field in publicProxyFields" :key="`public-${field.key}`" class="ext-secret">
            <label class="ext-field" :for="`${uid}-public-${field.key}`">{{ field.label }} <small>{{ settings?.upi?.[`has_${field.key}`] ? `已保存 ${settings.upi[`${field.key}_masked`] || '••••'}` : '未保存' }}</small></label>
            <input :id="`${uid}-public-${field.key}`" v-model="secrets[field.key]" type="password" autocomplete="new-password" autocapitalize="off" :spellcheck="false" :disabled="backgroundPaused || clearSecrets[field.key]" :placeholder="field.hint" />
            <label class="ext-check"><input v-model="clearSecrets[field.key]" type="checkbox" :disabled="backgroundPaused" />清除已保存的{{ field.label }}</label>
          </div>
        </div>
        <div class="ext-actions"><button class="ext-button ext-primary" type="button" :disabled="settingsActionBusy" @click="saveSettings('upi')">{{ settingsSaving === 'upi' ? '保存中…' : '保存公益 UPI 配置' }}</button></div>
      </div>
    </details>

    <section v-if="activeTab === 'public'" class="ext-provider-workspace" aria-label="公益批量任务扫码目标">
      <div class="ext-provider-target-card">
        <div><strong>选择提交目标</strong><p>自动切换按 Seashore → Foarge → CDK-Scan → Masi 顺序使用可用额度；每个账号最终只提交到一家。</p></div>
        <div class="ext-provider-targets" role="radiogroup" aria-label="公益批量任务提交目标">
          <label v-for="target in providerTargets" :key="target.key">
            <input v-model="pipelineScanProvider" type="radio" :value="target.key" :disabled="pipelineTaskBusy" />
            <span>{{ target.label }}</span>
          </label>
        </div>
      </div>
      <p class="ext-rule">每个平台独立保存凭据并单独验证。这里显示发布者连接状态和远端次数；CDK-Scan 的实际下单界面仍在“CDK-Scan 扫码”标签中，避免和公益批量任务混在一起。</p>
      <div class="ext-provider-services">
        <article v-for="provider in providerMeta" :key="provider.key" class="ext-provider-card" :class="{ 'is-selected': pipelineScanProvider === provider.key }">
          <div class="ext-provider-intro">
            <span class="ext-provider-icon" aria-hidden="true">◎</span>
            <div><strong>{{ provider.label }}</strong><p>{{ provider.description }}</p></div>
            <a v-if="provider.docs" class="ext-button ext-small" :href="provider.docs" target="_blank" rel="noopener noreferrer">{{ provider.docsLabel }}</a>
          </div>
          <div class="ext-provider-config-grid">
            <div class="ext-provider-field">
              <div class="ext-provider-label-row"><label :for="`${uid}-provider-${provider.key}`">{{ provider.key === 'cdk_scan' ? 'API Key' : provider.key === 'foarge' ? 'Foarge PBK' : provider.key === 'masi' ? 'Masi CDK' : '发布者 CDK' }}</label><span>{{ provider.key === 'cdk_scan' ? 'csk_ 凭证' : provider.key === 'foarge' ? 'Bearer PBK' : provider.key === 'masi' ? 'X-CDK' : 'PBK 凭证' }}</span></div>
              <input :id="`${uid}-provider-${provider.key}`" v-model="secrets[provider.secret]" type="text" autocomplete="off" autocapitalize="off" :spellcheck="false" :disabled="providerChecks[provider.key].loading || pipelineTaskBusy" :placeholder="provider.key === 'cdk_scan' ? 'csk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' : provider.key === 'foarge' ? 'PBK-XXXX-XXXX-XXXX' : provider.key === 'seashore' ? 'PBK-XXXX-XXXX-XXXX-XXXX' : '请输入 Masi CDK'" />
              <small :class="{ 'ext-provider-valid': providerChecks[provider.key].account, 'ext-provider-invalid': providerChecks[provider.key].error }">{{ providerChecks[provider.key].error || (providerChecks[provider.key].account ? '验证成功；凭据已保存，任务会提交到该接口。' : providerConfigured(provider.key) ? '凭据已保存，可点击验证刷新次数。' : '凭据只保存在服务端加密配置中。') }}</small>
              <div class="ext-actions"><button class="ext-button ext-primary" type="button" :disabled="settingsActionBusy || pipelineTaskBusy" @click="verifyProvider(provider.key)">{{ providerChecks[provider.key].loading ? '验证中…' : '验证凭据' }}</button><button class="ext-button" type="button" :disabled="settingsActionBusy || pipelineTaskBusy || (!providerConfigured(provider.key) && !secrets[provider.secret].trim())" @click="clearProvider(provider.key)">清除</button></div>
            </div>
            <dl class="ext-provider-metrics" aria-live="polite">
              <div><dt>连接状态</dt><dd>{{ providerConnection(provider.key) }}</dd></div>
              <div><dt>{{ providerMetricLabels(provider.key)[0] }}</dt><dd>{{ provider.key === 'cdk_scan' ? providerMetric(provider.key, 0) : providerMetric(provider.key, 0) }}</dd></div>
              <div><dt>{{ providerMetricLabels(provider.key)[1] }}</dt><dd>{{ providerMetric(provider.key, 1) }}</dd></div>
            </dl>
          </div>
        </article>
      </div>
      <div class="ext-actions"><button class="ext-button" type="button" :disabled="settingsActionBusy || pipelineTaskBusy" @click="refreshProviderStatuses">{{ providerStatusBusy ? '刷新中…' : '刷新所有平台状态' }}</button></div>
    </section>

    <section v-show="activeTab === 'public'" :id="`${uid}-panel-public`" role="tabpanel" :aria-labelledby="`${uid}-tab-public`" class="ext-panel">
      <div class="ext-section-heading"><div><h3>公益 UPI 批量提链</h3><p>选择分组和本次账号后，按批提链并提交到指定扫码目标。</p></div></div>
      <p class="ext-rule">公益 UPI 不需要旧版 CDK。先在上方配置入口 / 出口代理，再选择账号；默认不会提交整组。CDK-Scan 的独立扫码下单请前往“CDK-Scan 扫码”页。</p>
      <details class="ext-config ext-pipeline" @toggle="onPipelineToggle">
        <summary>整组自动提链并提交扫码 <span>按分组分批执行，失败账号继续下一批</span></summary>
        <div class="ext-config-body">
          <p class="ext-rule">先选择账号分组，再从账号列表勾选本次要提交的账号；默认不会选择整组。提链成功后自动发送到所选扫码平台。选择公益 UPI + Seashore 时，账号邮箱和 AT 会随对应付款链提交到 Seashore。</p>
          <div class="ext-pipeline-grid">
            <label class="ext-field">账号分组
              <select v-model="pipelineGroupId" :disabled="pipelineTaskBusy" @focus="loadPipelineGroups" @change="onPipelineGroupChange">
                <option value="">请选择分组</option>
                <option v-for="group in pipelineGroups" :key="group.id" :value="group.id">{{ group.name }}（{{ group.account_count ?? 0 }}）</option>
              </select>
            </label>
            <label class="ext-field">每批数量
              <select v-model.number="pipelineBatchSize" :disabled="pipelineTaskBusy">
                <option :value="10">10</option><option :value="20">20</option><option :value="30">30</option><option :value="50">50</option>
              </select>
            </label>
            <div class="ext-field"><span>提链平台</span><strong>公益 UPI（无需 CDK）</strong></div>
            <label class="ext-field">扫码平台
              <select v-model="pipelineScanProvider" :disabled="pipelineTaskBusy">
                <option value="cdk_scan">CDK-Scan（links + ats）</option><option value="seashore">Seashore 发布者 API</option><option value="foarge">Foarge 发布者 API</option><option value="masi">Masi API</option><option value="auto">自动切换（Seashore → Foarge → CDK-Scan → Masi）</option>
              </select>
            </label>
            <label v-if="pipelineScanProvider === 'cdk_scan'" class="ext-field">扫码单价（留空自动读取）
              <input v-model.trim="pipelineUnitPrice" inputmode="decimal" autocomplete="off" placeholder="自动读取当前单价" :disabled="pipelineTaskBusy" />
            </label>
          </div>
          <div class="ext-actions">
            <button class="ext-button" type="button" :disabled="pipelineGroupsLoading || pipelineTaskBusy" @click="loadPipelineGroups">{{ pipelineGroupsLoading ? '读取分组中…' : '刷新分组' }}</button>
            <button class="ext-button" type="button" :disabled="pipelineBusy || pipelineTaskBusy || !pipelineGroupId" @click="previewPipeline">{{ pipelineBusy && !pipelineTask ? '读取中…' : pipelinePreview ? '重新读取账号' : '加载账号列表' }}</button>
            <button class="ext-button ext-primary ext-push" type="button" :disabled="pipelineBusy || pipelineTaskBusy || !pipelinePreview || !pipelinePreview.eligible_count || !pipelineSelectedKeys.length || !pipelineUpiReady || !pipelineScanReady" @click="startPipeline">{{ pipelineBusy && !pipelineTask ? '启动中…' : '启动整组任务' }}</button>
          </div>
          <p v-if="!pipelineUpiReady || !pipelineScanReady" class="ext-warning">当前选择需要的凭据未保存：公益 UPI 需要入口 / 出口代理；扫码目标需保存对应凭证。</p>
          <p v-if="pipelinePreview && !pipelineSelectedKeys.length" class="ext-warning">请先在账号列表勾选本次要提取的账号，系统不会默认提交整组。</p>
          <dl v-if="pipelinePreview" class="ext-metrics"><div><dt>分组</dt><dd>{{ pipelinePreview.group.name }}</dd></div><div><dt>账号总数</dt><dd>{{ pipelinePreview.total_count }}</dd></div><div><dt>可处理</dt><dd>{{ pipelinePreview.eligible_count }}</dd></div><div><dt>跳过</dt><dd>{{ pipelinePreview.skipped_count }}</dd></div><div><dt>预计批次</dt><dd>{{ pipelinePreview.total_batches }}</dd></div></dl>
          <div v-if="pipelinePreview?.accounts?.length" class="ext-pipeline-selection">
            <div class="ext-section-heading"><div><h3>选择本次账号</h3><p>已选 {{ pipelineSelectedKeys.length }} 个；未勾选的账号不会提取。可跨页保留选择。</p></div><div class="ext-actions"><label class="ext-field ext-inline-field">每页<select v-model.number="pipelineAccountPageSize" :disabled="pipelineTaskBusy" @change="changePipelineAccountPageSize"><option :value="10">10</option><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option></select></label><small>本页已选 {{ pipelinePageSelectedCount }} 个 · 总计 {{ pipelineAccountRange }}</small></div></div>
            <div class="ext-actions ext-pipeline-select-actions">
              <label class="ext-check"><input type="checkbox" :checked="pipelinePageAllSelected" :disabled="pipelineTaskBusy || !pipelinePageEligibleKeys.length" @change="togglePipelinePage(($event.target as HTMLInputElement).checked)" />全选本页（{{ pipelinePageEligibleKeys.length }}）</label>
              <label class="ext-check"><input type="checkbox" :checked="pipelineGroupAllSelected" :disabled="pipelineTaskBusy || !pipelineEligibleKeysValue.length" @change="togglePipelineGroup(($event.target as HTMLInputElement).checked)" />全选此分组（{{ pipelineEligibleKeysValue.length }}）</label>
              <button v-if="pipelineSelectedKeys.length" class="ext-button ext-small" type="button" :disabled="pipelineTaskBusy" @click="togglePipelineGroup(false)">清空选择</button>
            </div>
            <div class="ext-pipeline-account-list"><label v-for="item in pipelineAccountPageItems" :key="item.key" class="ext-check"><input type="checkbox" :checked="pipelineSelectedKeys.includes(item.key)" :disabled="!item.eligible || pipelineTaskBusy" @change="togglePipelineAccount(item.key, ($event.target as HTMLInputElement).checked)" /><span>{{ item.email || `第 ${item.position} 个账号` }}</span><small v-if="!item.eligible">{{ item.issue || '不可处理' }}</small></label></div>
            <div class="ext-pagination"><small>显示 {{ pipelineAccountRange }} · 第 {{ pipelineAccountPage }} / {{ pipelineAccountPages }} 页</small><div class="ext-actions"><button class="ext-button ext-small" type="button" :disabled="pipelineAccountPage <= 1" @click="changePipelineAccountPage(-1)">上一页</button><button class="ext-button ext-small" type="button" :disabled="pipelineAccountPage >= pipelineAccountPages" @click="changePipelineAccountPage(1)">下一页</button></div></div>
          </div>
          <p v-if="pipelinePreview?.skipped_count" class="ext-muted">已有 {{ pipelinePreview.skipped_count }} 个账号因缺少 access token 或已停用而跳过；任务会继续处理其余账号。</p>
          <p v-if="pipelineError" class="ext-error" role="alert">{{ pipelineError }}</p><p v-if="pipelineNotice" class="ext-notice" role="status">{{ pipelineNotice }}</p>
          <div v-if="pipelineTask" class="ext-pipeline-task">
            <div class="ext-section-heading"><div><h3>当前整组任务</h3><p>{{ pipelineTask.group_name }} · {{ pipelineTask.upi_provider === 'public' ? '公益 UPI' : '旧版 UPI' }} → {{ pipelineTask.scan_provider === 'seashore' ? 'Seashore' : pipelineTask.scan_provider === 'foarge' ? 'Foarge' : pipelineTask.scan_provider === 'masi' ? 'Masi' : pipelineTask.scan_provider === 'auto' ? '自动切换' : 'CDK-Scan' }} · 每批 {{ pipelineTask.batch_size }} · 第 {{ pipelineTask.current_batch }} / {{ pipelineTask.total_batches }} 批</p></div><span class="ext-status" :class="statusTone(pipelineTask.status)">{{ statusLabel(pipelineTask.status) }}</span></div>
            <p class="ext-rule">任务明细只显示本次勾选的账号；未选择的账号仍计入任务总数和审计统计，不会出现在下面的账号表中。</p>
            <dl class="ext-metrics"><div v-if="pipelineTask.selected_count != null"><dt>本次选中</dt><dd>{{ pipelineTask.selected_count }}</dd></div><div><dt>已提交扫码</dt><dd>{{ pipelineTask.counts.submitted }}</dd></div><div><dt>失败</dt><dd>{{ pipelineTask.counts.failed }}</dd></div><div><dt>未选择</dt><dd>{{ pipelineTask.counts.not_selected }}</dd></div><div><dt>其他跳过</dt><dd>{{ Math.max(0, pipelineTask.counts.skipped - pipelineTask.counts.not_selected) }}</dd></div><div><dt>结果未知</dt><dd>{{ pipelineTask.counts.unknown }}</dd></div><div><dt>已处理</dt><dd>{{ pipelineTask.counts.finished }} / {{ pipelineTask.counts.total }}</dd></div><div v-if="pipelineTask.last_credits && Object.keys(pipelineTask.last_credits).length"><dt>最新 CDK 次数</dt><dd>{{ showValue(pipelineTask.last_credits) }}</dd></div></dl>
            <div class="ext-actions"><button v-if="['queued', 'running', 'stopping'].includes(pipelineTask.status)" class="ext-button" type="button" :disabled="pipelineBusy" @click="pipelineAction('stop')">{{ pipelineTask.status === 'stopping' ? '暂停中…' : '暂停任务' }}</button><button v-if="['paused', 'failed'].includes(pipelineTask.status)" class="ext-button ext-primary" type="button" :disabled="pipelineBusy" @click="pipelineAction('resume')">恢复任务</button><button class="ext-button" type="button" :disabled="pipelineBusy" @click="loadPipelineStatus">刷新进度</button></div>
            <div class="ext-table-wrap"><table class="ext-table ext-pipeline-table"><thead><tr><th>本次选中账号</th><th>状态</th><th>链接 / 结果</th></tr></thead><tbody><tr v-for="item in pipelineItems" :key="item.position"><td><b>{{ item.email || `第 ${item.position} 个账号` }}</b><small v-if="item.upi_status">提链：{{ statusLabel(item.upi_status) }}</small><small v-if="item.scan_status">扫码：{{ statusLabel(item.scan_status) }}</small></td><td><span class="ext-status" :class="statusTone(item.status)">{{ statusLabel(item.status) }}</span><p v-if="item.issue" class="ext-muted">{{ item.issue }}</p><p v-if="item.error" class="ext-error">{{ item.error }}</p></td><td><span v-if="item.link" class="ext-link-text">{{ item.link }}</span><span v-else class="ext-muted">—</span></td></tr><tr v-if="!pipelineItems.length"><td colspan="3" class="ext-empty">正在读取本次选中账号进度…</td></tr></tbody></table></div>
          </div>
        </div>
      </details>
    </section>

    <section v-show="activeTab === 'upi'" :id="`${uid}-panel-upi`" role="tabpanel" :aria-labelledby="`${uid}-tab-upi`" class="ext-panel">
      <div class="ext-section-heading"><div><h3>创建提链任务</h3><p>每成功提链 1 条消耗 1 点；入口与出口代理均为必填。</p></div><button class="ext-button" type="button" :disabled="creditsBusy || backgroundPaused || !settings?.upi.has_cdk" @click="loadCredits">{{ creditsBusy ? '查询中…' : '查询 CDK 余额' }}</button></div>
      <p class="ext-rule">提交后，Session / token 会发送到 UPI 提链服务（{{ services[0].url }}）。已保存设置：{{ paymentPlan(savedUpiOptions.payment_platform) }}<span v-if="savedUpiOptions.payment_platform === 'none'">填入扫码后仍需单独报价与确认。</span></p>
      <p v-if="!upiReady && !settingsLoading" class="ext-muted">展开上方配置并保存 CDK、入口代理和出口代理后即可提链。</p>
      <p v-if="!upiPaymentReady && !settingsLoading" class="ext-warning">请在上方保存 {{ paymentPlatformLabel(savedUpiOptions.payment_platform) }} 的支付凭证。</p>
      <p v-if="upiSettingsDirty" class="ext-warning">UPI 配置有未保存更改，请先保存配置再核对本批支付平台。</p>
      <p v-if="creditsError" class="ext-error" role="alert">余额查询失败：{{ creditsError }}</p>
      <dl v-if="credits" class="ext-metrics"><div v-for="(value, key) in credits" :key="key"><dt>{{ key }}</dt><dd>{{ showValue(value) }}</dd></div></dl>
      <label :for="`${uid}-sessions`" class="ext-field">Session / access token</label>
      <textarea :id="`${uid}-sessions`" v-model="upiText" rows="5" autocomplete="off" autocapitalize="off" :spellcheck="false" :disabled="upiLocked" placeholder="每行一个 token 或 Session JSON；也支持完整的单个 JSON / JSON 数组。每批 1–100 条，自动去重。" />
      <div class="ext-actions"><label class="ext-check"><input v-model="usePromo" type="checkbox" :disabled="upiLocked" />使用优惠（use_promo）</label><small>有效 {{ upiInput.count }} 条<span v-if="upiInput.duplicates">，已去重 {{ upiInput.duplicates }} 条</span></small><button class="ext-button ext-primary ext-push" type="button" :disabled="upiLocked || settingsActionBusy || upiSettingsDirty || !upiReady || !upiPaymentReady || !upiInput.count || !!upiInput.error" @click="prepareUpi">核对计费并提链</button></div>
      <p v-if="upiInput.error" class="ext-error" role="alert">{{ upiInput.error }}</p>
      <div v-if="upiConfirmation" class="ext-confirm" role="group" aria-label="提链计费确认">
        <div><strong>确认提交 {{ upiConfirmation.count }} 条 Session？</strong><p>每成功 1 条扣 1 点，本批最多消耗 {{ upiConfirmation.count }} 点。{{ upiConfirmation.count > 1 ? '批量提交到提链平台' : '提交到提链平台' }}；提交后不会自动重试。</p><p>本批平台：{{ paymentPlan(upiConfirmation.payment_platform) }}</p><small>提交编号：{{ upiConfirmation.request_id }}</small></div>
        <div class="ext-actions"><button class="ext-button" type="button" :disabled="upiBusy" @click="cancelUpiConfirmation">返回编辑</button><button class="ext-button ext-primary" type="button" :disabled="settingsActionBusy || upiSettingsDirty || upiConfirmation.payment_platform !== savedUpiOptions.payment_platform || !upiReady || !upiPaymentReady" @click="confirmUpi">{{ upiBusy ? '提交中…' : `确认计费并提交 ${upiConfirmation.count} 条` }}</button></div>
      </div>
      <p v-if="upiError" class="ext-error" role="alert">{{ upiError }}</p><p v-if="upiNotice" class="ext-notice" role="status">{{ upiNotice }}</p>
      <p v-if="upiReceipt" class="ext-muted ext-wrap">本次提交：{{ upiReceipt.request_id }} <span v-if="upiReceipt.batch_id">· 批次 {{ upiReceipt.batch_id }}</span> · 本次平台：{{ paymentPlatformLabel(upiReceipt.payment_platform) }}</p>
      <div v-if="upiUnknown" class="ext-confirm"><p>保留上方提交编号，在远端核对任务与扣点。原提交不提供重试入口。</p><label class="ext-check"><input v-model="upiReconciled" type="checkbox" />我已核对远端记录，新批次将使用其他 Session</label><button class="ext-button" type="button" :disabled="!upiReconciled" @click="finishUpiReconciliation">结束核对并清空输入</button></div>

      <details class="ext-config">
        <summary>CDK 融合 · 首张保留，其余额度转入后停用</summary>
        <div class="ext-config-body">
          <p class="ext-rule">每行一张，共 2–100 张；第一张为保留卡，其余源卡的额度转入后会停用。确认前请核对顺序。融合不会替换上方已保存的 CDK。</p>
          <template v-if="!fuseLocked">
            <label class="ext-field" :for="`${uid}-fuse-codes`">CDK（第一行为保留卡）</label>
            <textarea :id="`${uid}-fuse-codes`" v-model="fuseText" rows="4" autocomplete="off" autocapitalize="off" :spellcheck="false" :disabled="backgroundPaused" placeholder="第一行：保留卡；后续每行：一张源卡" />
            <label class="ext-field" :for="`${uid}-fuse-label`">标签（可选，最多 120 字符）<input :id="`${uid}-fuse-label`" v-model="fuseLabel" maxlength="120" :disabled="backgroundPaused" autocomplete="off" /></label>
            <p v-if="fuseInput.error" class="ext-error" role="alert">{{ fuseInput.error }}</p>
            <div class="ext-actions"><small>共 {{ fuseInput.codes.length }} 张</small><button class="ext-button ext-push" type="button" :disabled="settingsActionBusy || fuseInput.codes.length < 2 || !!fuseInput.error" @click="prepareFuse">核对融合</button></div>
          </template>
          <div v-if="fuseDraft" class="ext-confirm" role="group" aria-label="CDK 融合确认">
            <strong>确认融合 {{ fuseDraft.codes.length }} 张 CDK？</strong>
            <p>保留卡：{{ maskFuseTarget(fuseDraft.codes[0]) }}。其余 {{ fuseDraft.codes.length - 1 }} 张源卡的额度将转入保留卡，源卡随后停用。</p>
            <p v-if="fuseUnknown" class="ext-warning">结果未知，已固定原组合、顺序和标签。相同组合可手动重试，页面不会自动重试。若要编辑组合，请先核对上次结果。</p>
            <div class="ext-actions"><button class="ext-button" type="button" :disabled="fuseBusy" @click="editFuse">{{ fuseUnknown ? '已核对结果，返回编辑组合' : '返回编辑' }}</button><button class="ext-button ext-primary" type="button" :disabled="settingsActionBusy" @click="confirmFuse">{{ fuseBusy ? '融合中…' : fuseUnknown ? '用原组合手动重试' : `确认融合并停用 ${fuseDraft.codes.length - 1} 张源卡` }}</button></div>
          </div>
          <p v-if="fuseError" class="ext-error" role="alert">{{ fuseError }}</p>
          <div v-if="fuseResult" class="ext-receipt" role="status">
            <p>{{ fuseResult.duplicate ? '已复用同组合的融合结果' : 'CDK 融合成功' }} · 转入 {{ fuseResult.transferred_points }} 点</p>
            <p>保留卡：剩余 {{ fuseResult.target.remaining_uses }} 点<span v-if="fuseResult.target.total_uses !== undefined"> · 总额度 {{ fuseResult.target.total_uses }} 点</span><span v-if="fuseResult.target.enabled !== undefined"> · {{ fuseResult.target.enabled ? '已启用' : '已停用' }}</span></p>
            <p class="ext-muted">输入已清空。需要使用保留卡提链时，请在上方自行填写并保存 CDK。</p>
          </div>
        </div>
      </details>

      <div class="ext-section-heading"><div><h3>提链任务</h3><p>当前页有进行中任务时每 10 秒刷新；结果未知时停止轮询，请手动核对远端，不要重交。</p></div><button class="ext-button" type="button" :disabled="backgroundPaused || jobs.loading || !!jobBusy" @click="jobPager.load(true)">{{ jobs.loading ? '刷新中…' : '刷新任务' }}</button></div>
      <p v-if="jobs.error" class="ext-error" role="alert">{{ jobs.error }} 自动刷新已暂停，请手动刷新。</p><p v-if="jobActionError" class="ext-error" role="alert">{{ jobActionError }}</p>
      <div class="ext-table-wrap" :aria-busy="jobs.loading">
        <table class="ext-table ext-jobs"><thead><tr><th>任务 / 账号</th><th>状态</th><th>提链结果</th><th>操作</th></tr></thead><tbody>
          <tr v-for="job in jobs.items" :key="job.id"><td><b>{{ job.account_email || `第 ${job.position} 条` }}</b><small>本地 {{ job.id }}</small><small>批次 {{ job.batch_id }}</small><small v-if="job.job_id">远端 {{ job.job_id }}</small><small v-if="job.created_at">创建 {{ localTime(job.created_at) }}</small><small v-if="job.updated_at">更新 {{ localTime(job.updated_at) }}</small></td>
            <td><span class="ext-status" :class="statusTone(job.status)">{{ statusLabel(job.status) }}</span><small>本次平台：{{ paymentPlatformLabel(job.payment_platform) }}</small><small v-if="job.queue_position != null">队列位置 {{ job.queue_position }}</small><p v-if="job.status === 'unknown'" class="ext-warning">先核对远端；不要重交。</p><p v-if="job.error" class="ext-error">{{ job.error }}</p></td>
            <td>
              <template v-if="job.result?.payment_link">
                <span class="ext-link-text">{{ job.result.payment_link }}</span>
                <div class="ext-actions">
                  <button class="ext-button ext-small" type="button" @click="copyText(job.result.payment_link)">复制链接</button>
                  <a v-if="safeHttpUrl(job.result.payment_link)" class="ext-button ext-small" :href="safeHttpUrl(job.result.payment_link)" target="_blank" rel="noopener noreferrer">打开</a>
                </div>
              </template>
              <span v-else class="ext-muted">等待付款链接</span>
              <button v-if="resultScanLink(job.result)" class="ext-button ext-small ext-fill" type="button" :disabled="scanLocked || quoteBusy" @click="fillScan(resultScanLink(job.result))">填入扫码</button>
              <div v-if="(job.payment_platform && job.payment_platform !== 'none') || job.result?.payment_submit_status" class="ext-payment">
                <small>自动支付提交：{{ paymentSubmitLabel(job.result?.payment_submit_status) }}<span v-if="job.result?.payment_submit_status">（{{ job.result.payment_submit_status }}）</span></small>
                <small v-if="job.result?.payment_submit_id">支付提交编号：{{ job.result.payment_submit_id }}</small>
                <p v-if="job.result?.payment_submit_error" class="ext-error">支付提交错误：{{ job.result.payment_submit_error }}</p>
              </div>
              <small>最终到账状态：{{ paymentStatusLabel(job.result?.payment_status) }}<span v-if="job.result?.payment_status">（{{ job.result.payment_status }}）</span></small>
              <div v-if="qrImageUrl(job.result?.qr_code)" class="ext-qr">
                <img :key="job.result.qr_code" :src="qrImageUrl(job.result.qr_code)" :alt="`${job.account_email || '任务'}付款二维码`" loading="lazy" referrerpolicy="no-referrer" @error="qrErrors[job.id] = true" @load="qrErrors[job.id] = false" />
                <small v-if="qrErrors[job.id]" class="ext-error">二维码加载失败，请刷新任务或打开付款链接。</small>
              </div>
              <small v-else-if="job.result?.qr_code" class="ext-warning">二维码格式暂不支持，请打开付款链接。</small>
            </td>
            <td><div class="ext-actions"><button class="ext-button ext-small" type="button" :disabled="!!jobBusy || jobs.loading || backgroundPaused" @click="jobAction(job, 'refresh')">{{ jobBusy === job.id ? '处理中…' : '刷新状态' }}</button><button v-if="canCancel(job) && pendingCancel !== job.id" class="ext-button ext-small" type="button" :disabled="backgroundPaused || !!jobBusy || jobs.loading" @click="pendingCancel = job.id">取消任务</button></div><div v-if="pendingCancel === job.id" class="ext-cancel"><p>确认请求取消此任务？</p><div class="ext-actions"><button class="ext-button ext-small" type="button" :disabled="backgroundPaused || !!jobBusy || jobs.loading" @click="jobAction(job, 'cancel')">确认取消</button><button class="ext-button ext-small" type="button" :disabled="!!jobBusy" @click="pendingCancel = ''">保留</button></div></div></td>
          </tr><tr v-if="!jobs.items.length"><td colspan="4" class="ext-empty">{{ jobs.loading ? '正在读取任务…' : jobs.error ? '任务列表加载失败' : '暂无提链任务' }}</td></tr>
        </tbody></table>
      </div>
      <div class="ext-pagination"><span>共 {{ jobs.total }} 条 · 第 {{ jobs.page }} / {{ jobPager.pageCount.value }} 页 · 每页 {{ jobs.pageSize }} 条</span><div class="ext-actions"><button class="ext-button" type="button" :disabled="backgroundPaused || jobs.loading || !!jobBusy || jobs.page <= 1" @click="jobPager.go(jobs.page - 1)">上一页</button><button class="ext-button" type="button" :disabled="backgroundPaused || jobs.loading || !!jobBusy || jobs.page >= jobPager.pageCount.value" @click="jobPager.go(jobs.page + 1)">下一页</button></div></div>
    </section>

    <section v-show="activeTab === 'scan'" :id="`${uid}-panel-scan`" role="tabpanel" :aria-labelledby="`${uid}-tab-scan`" class="ext-panel">
      <div class="ext-section-heading"><div><h3>扫码下单</h3><p>输入付款链接，获取报价后再确认订单与冻结金额。</p></div><div class="ext-actions"><button class="ext-button" type="button" :disabled="configLoading || !scanReady || settingsBusy || quoteBusy || scanBusy" @click="loadScanConfig">{{ configLoading ? '查询中…' : '刷新平台状态' }}</button><button class="ext-button" type="button" :disabled="balanceBusy || settingsBusy || !settings?.scan.has_bearer_token" @click="loadBalance">{{ balanceBusy ? '查询中…' : '查询余额' }}</button></div></div>
      <p v-if="!scanReady" class="ext-rule">请先在配置中保存 API Key，再获取报价或查询订单。</p>
      <p class="ext-rule">当前目标：{{ settings?.scan.cdk_scan_api_base || 'https://scan.fishmail.vip' }}/api/v1/client/orders；这里的 API Key / CDK 只属于 CDK-Scan，不会复用公益发布者凭据。</p>
      <p class="ext-muted">余额查询需要可选 Bearer Token；仅配置 API Key 也可报价、下单和查询订单。</p>
      <p v-if="configError" class="ext-error" role="alert">平台查询失败：{{ configError }}</p><p v-if="balanceError" class="ext-error" role="alert">余额查询失败：{{ balanceError }}</p>
      <dl v-if="scanConfig" class="ext-metrics"><div><dt>平台状态</dt><dd :class="scanConfig.available ? 'ext-notice' : 'ext-warning'">{{ scanConfig.available ? '可接单' : '暂不可用' }}</dd></div><div><dt>当前单价</dt><dd>{{ scanConfig.unit_price }}</dd></div><div><dt>每批上限</dt><dd>{{ scanConfig.max_batch }} 条</dd></div><div><dt>接单供应商</dt><dd>{{ scanConfig.accepting_supplier_count }}</dd></div></dl>
      <dl v-if="balance" class="ext-metrics"><div><dt>余额</dt><dd>{{ balance.balance }}</dd></div><div><dt>冻结余额</dt><dd>{{ balance.frozen_balance }}</dd></div><div><dt>单价</dt><dd>{{ balance.unit_price }}</dd></div></dl>
      <label :for="`${uid}-links`" class="ext-field">付款链接（每行一条）</label><textarea :id="`${uid}-links`" v-model="scanText" rows="4" :disabled="scanLocked || quoteBusy" autocomplete="off" autocapitalize="off" :spellcheck="false" placeholder="payments.stripe.com/upi/instructions/… 或 pay.nicepay.co.kr 付款链接，也可从提链结果点击「填入扫码」。" />
      <div class="ext-actions"><small>有效 {{ scanInput.links.length }} 条<span v-if="scanInput.duplicates">，已去重 {{ scanInput.duplicates }} 条</span></small><button class="ext-button ext-primary ext-push" type="button" :disabled="quoteBusy || scanLocked || settingsBusy || !scanReady || !scanConfig?.available || configLoading || !scanInput.links.length || !!scanInput.error" @click="quoteScan">{{ quoteBusy ? '报价中…' : '获取报价' }}</button></div>
      <p v-if="scanInput.error" class="ext-error" role="alert">{{ scanInput.error }}</p>
      <div v-if="scanQuote" class="ext-confirm" role="group" aria-label="扫码冻结金额确认"><div><strong>{{ scanQuote.quote.count }} 条订单 · 将冻结 {{ scanQuote.quote.total_amount }}</strong><p>最终报价单价 {{ scanQuote.quote.unit_price }}。确认后向 CDK-Scan 发送这些链接并创建扫码订单。</p></div><div class="ext-actions"><button class="ext-button" type="button" :disabled="scanBusy" @click="scanQuote = null">暂不提交</button><button class="ext-button ext-primary" type="button" :disabled="scanLocked || settingsBusy || !scanReady || !scanConfig?.available" @click="confirmScan">确认 {{ scanQuote.quote.count }} 条并冻结 {{ scanQuote.quote.total_amount }}</button></div></div>
      <p v-if="scanError" class="ext-error" role="alert">{{ scanError }}</p><p v-if="scanNotice" class="ext-notice" role="status">{{ scanNotice }}</p>
      <div v-if="scanAttempt" class="ext-receipt">
        <div class="ext-actions"><span class="ext-status" :class="statusTone(scanAttempt.status)">{{ statusLabel(scanAttempt.status) }}</span><span class="ext-wrap">提交编号：{{ scanAttempt.id }}</span></div>
        <p v-if="scanAttempt.error" class="ext-error">{{ scanAttempt.error }}</p>
        <p v-if="scanAttempt.status === 'unknown'" class="ext-warning">响应结果未知。保留原编号和原报价内容，仅使用原提交编号重试；刷新页面后可在下方提交记录中继续核对。</p>
        <p v-if="scanAttempt.status === 'submitting'" class="ext-muted">提交仍在处理中，请在下方提交记录中刷新核对。</p>
        <p v-if="scanAttempt.status === 'failed'" class="ext-muted">修正输入或配置后重新获取报价，新提交会生成新编号。</p>
        <p v-if="retrySeconds(scanAttempt.id) > 0" class="ext-warning">平台限流，请 {{ retrySeconds(scanAttempt.id) }} 秒后使用原提交编号手动重试。</p>
        <p v-if="scanAttempt.data" class="ext-muted">批次 {{ scanAttempt.data.batch_no || '—' }} · {{ scanAttempt.data.order_count ?? '—' }} 条 · 总金额 {{ scanAttempt.data.total_amount ?? '—' }}<span v-if="scanAttempt.data.idempotent"> · 已复用原提交结果</span></p>
        <template v-if="scanAttempt.status === 'unknown' && scanAttempt.retryable && scanPayload">
          <p v-if="scanMissing" class="ext-warning">重试接口明确返回记录不存在（404）。可以使用原提交编号、原链接和原单价补交本次请求。</p>
          <button v-if="scanMissing" class="ext-button" type="button" :disabled="scanBusy || !!retrying || settingsBusy || !scanReady || retrySeconds(scanAttempt.id) > 0" @click="resendMissingScan">使用原编号与原报价补交</button>
          <button v-else class="ext-button" type="button" :disabled="scanBusy || !!retrying || settingsBusy || !scanReady || retrySeconds(scanAttempt.id) > 0" @click="retryCurrentScan">使用原提交编号重试</button>
        </template>
      </div>

      <div class="ext-section-heading"><div><h3>扫码订单</h3><p>当前页有待接单、已接单或处理中订单时，每 15 秒刷新。</p></div><button class="ext-button" type="button" :disabled="orders.loading || !scanReady || settingsBusy" @click="orderPager.load()">{{ orders.loading ? '刷新中…' : '刷新订单' }}</button></div>
      <p v-if="orders.error" class="ext-error" role="alert">{{ orders.error }} 自动刷新已暂停，请手动刷新。</p>
      <div class="ext-table-wrap" :aria-busy="orders.loading"><table class="ext-table"><thead><tr><th>订单号</th><th>状态 / 金额</th><th>付款链接</th><th>结果 / 失败原因</th></tr></thead><tbody><tr v-for="(order, index) in orders.items" :key="order.order_no || order.id || order.order_id || index"><td>{{ order.order_no || order.order_id || order.id || '—' }}</td><td><span class="ext-status" :class="statusTone(order.status)">{{ statusLabel(order.status) }}</span><small>实际收费 {{ order.charge_amount ?? '—' }}</small><small>单价 {{ order.unit_price ?? '—' }}</small></td><td><span class="ext-link-text">{{ order.link || '—' }}</span><div v-if="order.link" class="ext-actions"><button class="ext-button ext-small" type="button" @click="copyText(order.link)">复制</button><a v-if="safeHttpUrl(order.link)" class="ext-button ext-small" :href="safeHttpUrl(order.link)" target="_blank" rel="noopener noreferrer">打开</a></div></td><td><pre>{{ showValue(order.result) }}</pre><p v-if="order.failure_reason" class="ext-error">{{ order.failure_reason }}</p></td></tr><tr v-if="!orders.items.length"><td colspan="4" class="ext-empty">{{ !scanReady ? '保存 API Key 后查询订单' : orders.loading ? '正在读取订单…' : orders.error ? '订单列表加载失败' : '暂无扫码订单' }}</td></tr></tbody></table></div>
      <div class="ext-pagination"><span>共 {{ orders.total }} 条 · 第 {{ orders.page }} / {{ orderPager.pageCount.value }} 页 · 每页 {{ orders.pageSize }} 条</span><div class="ext-actions"><button class="ext-button" type="button" :disabled="orders.loading || !scanReady || orders.page <= 1" @click="orderPager.go(orders.page - 1)">上一页</button><button class="ext-button" type="button" :disabled="orders.loading || !scanReady || orders.page >= orderPager.pageCount.value" @click="orderPager.go(orders.page + 1)">下一页</button></div></div>
      <details class="ext-history" :open="historyOpen" @toggle="toggleHistory">
        <summary>提交记录 · 结果未知时从这里找回原提交编号</summary>
        <div class="ext-history-body">
          <div class="ext-actions"><p class="ext-muted">历史重试使用服务端保存的原始提交内容，与上方输入无关。</p><button class="ext-button ext-push" type="button" :disabled="submissions.loading || !scanReady || scanBusy || !!retrying" @click="submissionPager.load()">{{ submissions.loading ? '读取中…' : '刷新提交记录' }}</button></div>
          <p v-if="submissions.error" class="ext-error" role="alert">{{ submissions.error }}</p>
          <div v-for="submission in submissions.items" :key="submission.id" class="ext-history-row">
            <div>
              <b class="ext-wrap">{{ submission.id }}</b>
              <p><span class="ext-status" :class="statusTone(submission.status)">{{ statusLabel(submission.status) }}</span> <span v-if="submission.data">{{ submission.data.order_count ?? '—' }} 条 · 金额 {{ submission.data.total_amount ?? '—' }} · 批次 {{ submission.data.batch_no || '—' }}</span></p>
              <p v-if="submission.error" class="ext-error">{{ submission.error }}</p>
              <small v-if="submission.status === 'unknown'">先核对原提交；重试沿用同一编号与内容。</small>
              <p v-if="retrySeconds(submission.id) > 0" class="ext-warning">限流等待 {{ retrySeconds(submission.id) }} 秒</p>
            </div>
            <button v-if="submission.status === 'unknown' && submission.retryable" class="ext-button" type="button" :disabled="scanBusy || !!retrying || settingsBusy || !scanReady || retrySeconds(submission.id) > 0 || (scanLocked && scanAttempt?.id !== submission.id)" @click="retrySubmission(submission)">{{ retrying === submission.id ? '重试中…' : '使用原提交编号重试' }}</button>
          </div>
          <p v-if="!submissions.items.length" class="ext-empty">{{ submissions.loading ? '正在读取提交记录…' : submissions.error ? '提交记录加载失败' : '暂无提交记录' }}</p>
          <div class="ext-pagination"><span>共 {{ submissions.total }} 条 · 第 {{ submissions.page }} / {{ submissionPager.pageCount.value }} 页</span><div class="ext-actions"><button class="ext-button" type="button" :disabled="submissions.loading || submissions.page <= 1 || scanBusy || !!retrying" @click="submissionPager.go(submissions.page - 1)">上一页</button><button class="ext-button" type="button" :disabled="submissions.loading || submissions.page >= submissionPager.pageCount.value || scanBusy || !!retrying" @click="submissionPager.go(submissions.page + 1)">下一页</button></div></div>
        </div>
      </details>
    </section>
  </section>
</template>

<style scoped>
.external-api {
  --ext-text: var(--text-primary, var(--text, #1e293b));
  --ext-muted: var(--text-secondary, var(--muted, #64748b));
  --ext-line: var(--field-border, var(--line, #d8dfeb));
  --ext-field: var(--field-bg, var(--soft, #f5f7fb));
  --ext-accent: var(--accent, var(--brand, #2563eb));
  --ext-danger: var(--error, var(--red, #dc2626));
  --ext-warning: var(--warning, var(--warn, #a16207));
  display: grid; gap: 14px; min-width: 0; padding: 20px; color: var(--ext-text);
  background: var(--glass-bg, var(--card, #fff)); border: 1px solid var(--ext-line);
  border-radius: var(--radius-md, 14px); font-size: .82rem; line-height: 1.55;
}
.external-api :is(h2, h3, p, dl, dd) { margin: 0; }
.external-api h2 { font-size: 1.05rem; }.external-api h3 { font-size: .92rem; }
.ext-heading, .ext-section-heading, .ext-actions, .ext-pagination, .ext-history-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.ext-heading, .ext-section-heading, .ext-pagination, .ext-history-row { justify-content: space-between; }
.ext-heading p, .ext-section-heading p, .ext-muted, .ext-actions small, .ext-pagination, .ext-confirm small { color: var(--ext-muted); font-size: .76rem; }
.ext-tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--ext-line); padding-bottom: 12px; }
.external-api .ext-button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 36px; padding: 6px 11px; width: auto; border: 1px solid var(--ext-line); border-radius: 7px; background: var(--ext-field); color: var(--ext-text); box-shadow: none; font: inherit; line-height: 1.4; cursor: pointer; text-decoration: none; }
.external-api .ext-button:hover:not(:disabled) { border-color: var(--ext-accent); }
.external-api .ext-primary, .external-api .ext-tabs .is-active { color: #fff; background: var(--ext-accent); border-color: var(--ext-accent); }
.external-api .ext-small { min-height: 29px; font-size: .73rem; padding: 4px 8px; }
.ext-push { margin-left: auto; }.external-api :disabled { opacity: .55; cursor: default; }
.external-api :is(button, input, select, textarea, summary, a):focus-visible { outline: 2px solid var(--ext-accent); outline-offset: 2px; }
.ext-panel, .ext-config-body, .ext-history-body { display: grid; gap: 12px; min-width: 0; }
.ext-panel > .ext-section-heading:not(:first-child) { margin-top: 10px; padding-top: 18px; border-top: 1px solid var(--ext-line); }
.ext-config, .ext-history { border: 1px solid var(--ext-line); border-radius: 9px; min-width: 0; }
.ext-config > summary, .ext-history > summary { padding: 11px 13px; cursor: pointer; font-weight: 600; }
.ext-config summary span { font-weight: 400; font-size: .75rem; color: var(--ext-muted); margin-left: 8px; }
.ext-config-body, .ext-history-body { padding: 0 13px 13px; }
.ext-secret-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.ext-secret, .ext-field { display: grid; gap: 5px; min-width: 0; }.ext-secret small { color: var(--ext-muted); font-weight: 400; overflow-wrap: anywhere; }
.external-api input:not([type="checkbox"]), .external-api select, .external-api textarea { box-sizing: border-box; width: 100%; min-width: 0; padding: 8px 10px; border: 1px solid var(--ext-line); border-radius: 7px; color: var(--ext-text); background: var(--ext-field); font: inherit; }
.external-api input[readonly] { color: var(--ext-muted); }.external-api textarea { resize: vertical; font-family: var(--mono, monospace); font-size: .77rem; line-height: 1.65; }
.ext-check { display: inline-flex; align-items: center; gap: 7px; font-size: .76rem; cursor: pointer; }
.external-api input[type="checkbox"] { width: 16px; height: 16px; margin: 0; accent-color: var(--ext-accent); flex: 0 0 auto; }
.ext-rule, .ext-confirm, .ext-receipt { background: var(--ext-field); padding: 12px 14px; border-radius: 8px; }
.ext-rule { border-left: 3px solid var(--ext-accent); color: var(--ext-muted); font-size: .77rem; overflow-wrap: anywhere; }
.ext-confirm { display: grid; gap: 10px; border: 1px solid var(--ext-accent); }.ext-confirm p { margin-top: 4px; }.ext-receipt { display: grid; gap: 8px; border: 1px solid var(--ext-line); }
.ext-error { color: var(--ext-danger); overflow-wrap: anywhere; }.ext-notice { color: var(--ext-accent); }.ext-warning { color: var(--ext-warning); }
.ext-metrics { display: flex; flex-wrap: wrap; gap: 10px 28px; padding: 10px 14px; border: 1px solid var(--ext-line); border-radius: 8px; }
.ext-metrics > div { min-width: 65px; max-width: 100%; }.ext-metrics dt { color: var(--ext-muted); font-size: .72rem; }.ext-metrics dd { font-weight: 600; white-space: pre-wrap; overflow-wrap: anywhere; }
.ext-table-wrap { min-width: 0; overflow-x: auto; border: 1px solid var(--ext-line); border-radius: 8px; }
.ext-table { width: 100%; min-width: 750px; border-collapse: collapse; text-align: left; font-size: .77rem; table-layout: fixed; }
.ext-table th, .ext-table td { padding: 11px 12px; border-bottom: 1px solid var(--ext-line); vertical-align: top; overflow-wrap: anywhere; }
.ext-table th { background: var(--ext-field); color: var(--ext-muted); font-weight: 500; }.ext-table tbody tr:last-child td { border-bottom: 0; }
.ext-table small { display: block; color: var(--ext-muted); font-size: .7rem; margin-top: 4px; }.ext-jobs th:nth-child(3) { width: 34%; }.ext-jobs th:nth-child(2) { width: 19%; }
.ext-link-text { display: block; overflow-wrap: anywhere; margin-bottom: 7px; }.external-api pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 160px; overflow: auto; margin: 0; font: .73rem/1.5 var(--mono, monospace); }
.ext-status { display: inline-flex; padding: 2px 7px; border-radius: 5px; background: var(--ext-field); font-size: .74rem; }
.ext-status.success { color: var(--success, var(--ok, #15803d)); }.ext-status.warning { color: var(--ext-warning); }
.ext-empty { text-align: center; padding: 24px 12px !important; color: var(--ext-muted); }.ext-wrap { overflow-wrap: anywhere; min-width: 0; }
.ext-fill { margin-top: 7px; }.ext-qr { margin-top: 10px; }.ext-qr img { display: block; width: 180px; height: 180px; max-width: 100%; object-fit: contain; background: #fff; border: 8px solid #fff; border-radius: 6px; box-sizing: border-box; }.ext-cancel { display: grid; gap: 7px; margin-top: 8px; }
.ext-history-row { padding: 12px 0; border-top: 1px solid var(--ext-line); align-items: flex-start; }.ext-history-row > div { flex: 1; min-width: 200px; }.ext-history-row small { color: var(--ext-muted); }
.ext-provider-workspace { display: grid; gap: 14px; }
.ext-provider-target-card { display: flex; align-items: center; gap: 18px; padding: 15px 16px; border: 1px solid var(--ext-line); border-radius: 10px; background: var(--ext-field); }
.ext-provider-target-card > div:first-child { margin-right: auto; }.ext-provider-target-card strong { display: block; font-size: .86rem; }.ext-provider-target-card p { margin-top: 4px; color: var(--ext-muted); font-size: .75rem; }
.ext-provider-targets { display: flex; flex-wrap: wrap; gap: 7px; justify-content: flex-end; }.ext-provider-targets label { cursor: pointer; }.ext-provider-targets input { position: absolute; opacity: 0; pointer-events: none; }.ext-provider-targets span { display: block; padding: 7px 11px; border: 1px solid var(--ext-line); border-radius: 7px; background: var(--ext-field); color: var(--ext-muted); font-size: .75rem; font-weight: 600; }.ext-provider-targets input:checked + span { border-color: var(--ext-accent); background: color-mix(in srgb, var(--ext-accent) 10%, var(--ext-field)); color: var(--ext-accent); }
.ext-provider-services { display: grid; gap: 14px; }.ext-provider-card { display: grid; gap: 16px; padding: 16px; border: 1px solid var(--ext-line); border-radius: 10px; background: var(--glass-bg, var(--card, #fff)); }.ext-provider-card.is-selected { border-color: color-mix(in srgb, var(--ext-accent) 60%, var(--ext-line)); box-shadow: 0 0 0 2px color-mix(in srgb, var(--ext-accent) 12%, transparent); }.ext-provider-intro { display: flex; align-items: center; gap: 10px; padding-bottom: 14px; border-bottom: 1px solid var(--ext-line); }.ext-provider-intro > div { margin-right: auto; min-width: 0; }.ext-provider-intro strong { display: block; font-size: .86rem; }.ext-provider-intro p { margin-top: 3px; color: var(--ext-muted); font-size: .75rem; }.ext-provider-icon { display: inline-grid; place-items: center; width: 27px; height: 27px; border: 1px solid var(--ext-line); border-radius: 7px; color: var(--ext-accent); font-size: 1.25rem; line-height: 1; }
.ext-provider-config-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr); gap: 18px; align-items: start; }.ext-provider-field { min-width: 0; }.ext-provider-label-row { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 5px; font-size: .76rem; }.ext-provider-label-row span { color: var(--ext-muted); font-size: .7rem; }.ext-provider-field > small { display: block; min-height: 1.4em; margin-top: 5px; color: var(--ext-muted); overflow-wrap: anywhere; }.ext-provider-field > small.ext-provider-valid { color: var(--success, var(--ok, #15803d)); }.ext-provider-field > small.ext-provider-invalid { color: var(--ext-danger); }
.ext-provider-metrics { margin: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }.ext-provider-metrics > div { min-width: 0; padding: 13px; border: 1px solid var(--ext-line); border-radius: 8px; background: var(--ext-field); }.ext-provider-metrics dt { color: var(--ext-muted); font-size: .7rem; }.ext-provider-metrics dd { margin-top: 4px; font-weight: 600; overflow-wrap: anywhere; }
.ext-pipeline { border-color: color-mix(in srgb, var(--ext-accent) 28%, var(--ext-line)); }
.ext-pipeline-grid { display: grid; grid-template-columns: minmax(180px, 1.4fr) minmax(120px, .7fr) minmax(150px, 1fr); gap: 10px; }
.ext-pipeline-task { display: grid; gap: 10px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--ext-line); }
.ext-pipeline-table td { vertical-align: top; }
.ext-pipeline-table small { display: block; color: var(--ext-muted); }
.ext-pipeline-selection { display: grid; gap: 8px; margin-top: 12px; padding: 10px; border: 1px solid var(--ext-line); border-radius: 8px; }
.ext-inline-field { display: inline-flex; align-items: center; grid-template-columns: auto auto; gap: 6px; min-width: 86px; }
.ext-inline-field select { width: auto; min-width: 58px; padding: 5px 7px; }
.ext-pipeline-account-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 6px 12px; max-height: 280px; overflow: auto; padding: 4px; }
.ext-pipeline-account-list .ext-check { min-width: 0; }
.ext-pipeline-account-list .ext-check span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ext-pipeline-account-list .ext-check small { color: var(--ext-muted); margin-left: auto; }
@media (max-width: 640px) {
  .external-api { padding: 14px; }.ext-tabs .ext-button { flex: 1; }.ext-secret-grid { grid-template-columns: minmax(0, 1fr); }
  .ext-config summary span { display: block; margin-left: 0; }.ext-push { margin-left: 0; }.ext-pagination { align-items: flex-start; }.ext-pipeline-grid { grid-template-columns: minmax(0, 1fr); }
  .ext-provider-target-card { align-items: flex-start; flex-direction: column; }.ext-provider-target-card > div:first-child { margin-right: 0; }.ext-provider-targets { width: 100%; justify-content: stretch; }.ext-provider-targets label { flex: 1; min-width: 42%; text-align: center; }.ext-provider-config-grid { grid-template-columns: minmax(0, 1fr); }.ext-provider-metrics { grid-template-columns: minmax(0, 1fr); }.ext-provider-intro { align-items: flex-start; flex-wrap: wrap; }.ext-provider-intro .ext-button { margin-left: auto; }
  .external-api textarea, .external-api select, .external-api input:not([type="checkbox"]) { font-size: 16px; }.ext-confirm .ext-actions > button { flex: 1; }
}
</style>
