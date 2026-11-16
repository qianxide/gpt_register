<script setup lang="ts">
/* --- 邮箱换绑：预检（原子预留目标邮箱）→ 确认 → 批次换绑（进度/逐项结果/停止） --- */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
  AlertTriangle, ArrowLeftRight, ArrowRight, CheckCircle2, ClipboardPaste, Eraser,
  MailCheck, Play, RefreshCw, ShieldCheck, Square,
} from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { ApiError } from "../../api/client";
import { useRebindStore } from "../../stores/rebind";
import { useAccountsStore } from "../../stores/accounts";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import MailcomRebindSettings from "../../../components/MailcomRebindSettings.vue";
import RebindAccountPicker from "../../../components/RebindAccountPicker.vue";
import RebindMailboxPicker from "../../../components/RebindMailboxPicker.vue";

const INPUT_STATE_KEY = "gpt-register-fp.rebind-input.v1";

const rebind = useRebindStore();
const accounts = useAccountsStore();
const ui = useUiStore();
const confirm = useConfirm();

const sourceEmail = ref("");
const targetMode = ref<"auto" | "custom" | "pool">("auto");
const targetEmail = ref("");
const providerType = ref("maddy_webui");
const concurrency = ref(1);
const pasteError = ref("");
const restoringInput = ref(true);
const mailcomSelection = ref({ account_email: "", domain: "random" });
const mailcomReady = ref(false);
const mailcomBusy = ref(false);
const confirmingStart = ref(false);
const poolEmails = ref<string[]>([]);
const poolProviderRef = ref("");
const poolGeneration = ref(0);

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function splitEntries(value: string): string[] {
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter(Boolean);
}

const sourceEntries = computed(() => {
  const values: string[] = [];
  String(sourceEmail.value || "").split(/[\r\n]+/).forEach((line) => {
    let value = line.trim();
    if (!value) return;
    if (value.includes("----")) value = value.split("----", 1)[0].trim();
    else if (value.includes("->")) value = value.split("->", 1)[0].trim();
    values.push(...splitEntries(value));
  });
  return [...new Set(values)];
});

const sourceInvalid = computed(() => sourceEntries.value.filter((value) => !emailPattern.test(value)));
const sourceValid = computed(() => sourceEntries.value.length > 0 && sourceInvalid.value.length === 0);
const validSourceCount = computed(() => sourceEntries.value.filter((value) => emailPattern.test(value)).length);
const targetEntries = computed(() => targetMode.value === "pool" ? poolEmails.value : splitEntries(targetEmail.value));
const targetInvalid = computed(() => targetEntries.value.filter((value) => !emailPattern.test(value)));
const targetCountValid = computed(() => targetMode.value === "auto" || targetEntries.value.length === validSourceCount.value);
const targetValid = computed(() => targetMode.value === "auto" || (targetEntries.value.length > 0 && targetInvalid.value.length === 0 && targetCountValid.value));

const progress = computed(() => rebind.progress);
const preflight = computed(() => rebind.preflight);
const progressRunning = computed(() => Boolean(progress.value.running) || ["queued", "running", "stopping"].includes(String(progress.value.status || "")));
const reservationId = computed(() => String(preflight.value.reservation_id || "").trim());
const eligible = computed(() => preflight.value.eligible === true && Boolean(reservationId.value));
const sourceBusy = computed(() => rebind.requestBusy || rebind.checking || progressRunning.value || confirmingStart.value || (providerType.value === "mailcom_api" && mailcomBusy.value));
const sourceReady = computed(() => providerType.value !== "mailcom_api" || mailcomReady.value);
const mailcomDeferredTarget = computed(() => providerType.value === "mailcom_api" && targetMode.value === "auto");
const canCheck = computed(() => sourceEntries.value.some((value) => emailPattern.test(value)) && targetValid.value && sourceReady.value && !sourceBusy.value);
const canStart = computed(() => eligible.value && targetValid.value && sourceReady.value && !sourceBusy.value);
const sourceOptions = computed(() => ({
  ...(targetMode.value === "pool" && poolProviderRef.value ? { provider_ref: poolProviderRef.value } : {}),
  // Automatic mail.com allocation must retain the full saved account pool.
  // A manually selected primary is only meaningful for explicit targets.
  ...(providerType.value === "mailcom_api" && targetMode.value !== "auto"
    ? { mailcom_account: mailcomSelection.value.account_email, mailcom_domain: mailcomSelection.value.domain }
    : providerType.value === "mailcom_api" ? { mailcom_domain: mailcomSelection.value.domain } : {}),
}));
const poolSelectionHandler = computed(() => {
  const provider = providerType.value;
  const generation = poolGeneration.value;
  return (selection: { emails: string[]; provider_ref: string }) => {
    if (sourceBusy.value || targetMode.value !== "pool" || provider !== providerType.value || generation !== poolGeneration.value) return;
    const emails = [...new Set(selection.emails.map((value) => value.trim().toLowerCase()).filter(Boolean))];
    if (poolEmails.value.join("\n") === emails.join("\n") && poolProviderRef.value === selection.provider_ref) return;
    poolEmails.value = emails;
    poolProviderRef.value = selection.provider_ref;
    rebind.resetForInput();
  };
});
const percent = computed(() => {
  const total = Number(progress.value.total || 0);
  const done = Number(progress.value.done || 0);
  return total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
});

const hasPreflight = computed(() => {
  const value = preflight.value;
  return Boolean(
    value.message || value.source || value.target
    || value.not_found?.length || value.invalid_format?.length
    || value.missing_credentials?.length || value.conflicts?.length || value.items?.length,
  );
});

const source = computed(() => preflight.value.source || null);
const target = computed(() => preflight.value.target || null);
const preflightItems = computed(() => (Array.isArray(preflight.value.items) ? preflight.value.items : []));
const eligibleCount = computed(() => Number(preflight.value.eligible_count || (preflight.value.eligible ? 1 : 0)));
const invalidFormat = computed(() => preflight.value.invalid_format || []);
const invalidTargets = computed(() => preflight.value.invalid_targets || []);
const notFound = computed(() => preflight.value.not_found || []);
const missingCredentials = computed(() => preflight.value.missing_credentials || []);
const skippedCount = computed(() => Number(preflight.value.skipped_count ?? missingCredentials.value.length));
const conflicts = computed(() => preflight.value.conflicts || []);
const progressItems = computed(() => (Array.isArray(progress.value.items) ? progress.value.items : []) as NonNullable<typeof progress.value.items>);

function providerLabel(value?: string) {
  return ({ fastmail_api: "Fastmail 别名池", mailcom_api: "mail.com 域名邮箱", icloud_api: "iCloud / 已导入邮箱", outlook_token: "Outlook 邮箱" } as Record<string, string>)[String(value || "")] || "Maddy 域名邮箱";
}

function credentialLabel(value?: string) {
  return ({ password: "密码", password_unconfirmed: "密码未确认", totp: "TOTP 2FA", totp_secret: "TOTP 2FA" } as Record<string, string>)[String(value || "").toLowerCase()] || String(value || "安全字段");
}

function healthLabel(value?: string) {
  return ({ alive: "可用", ok: "可用", limited: "限流", reauth_required: "待续期", invalid: "失效", error: "错误", unknown: "未检测" } as Record<string, string>)[String(value || "unknown").toLowerCase()] || String(value || "未检测");
}

function planLabel(value?: string) {
  const plan = String(value || "free").trim().toLowerCase();
  return plan === "plus" ? "Plus" : plan === "free" ? "Free" : plan.toUpperCase();
}

function itemStatusLabel(item: { status?: string }) {
  const status = String(item?.status || "queued").toLowerCase();
  return ({ queued: "排队", running: "进行中", success: "成功", completed: "成功", failed: "失败", cancelled: "已停止", stopped: "已停止" } as Record<string, string>)[status] || status;
}

function itemStatusKind(item: { status?: string }): "success" | "error" | "warning" | "info" {
  const status = String(item?.status || "queued").toLowerCase();
  if (["success", "completed"].includes(status)) return "success";
  if (status === "failed") return "error";
  if (["cancelled", "stopped"].includes(status)) return "warning";
  return "info";
}

function domainOf(value?: string) {
  const text = String(value || "");
  return text.includes("@") ? text.split("@").slice(1).join("@") : "";
}

async function check() {
  if (!canCheck.value) return;
  try {
    await rebind.check({
      text: String(sourceEmail.value || "").trim(),
      new_email: targetMode.value === "pool" ? poolEmails.value.join("\n") : targetMode.value === "custom" ? String(targetEmail.value || "").trim() : "",
      provider_type: providerType.value,
      ...sourceOptions.value,
    });
    if (!rebind.preflight.eligible) ui.notify(rebind.preflight.message || "换绑预检未通过", "error");
    else ui.notify(rebind.preflight.message || `已匹配 ${eligibleCount.value || 1} 个未占用邮箱，请确认后开始换绑`, "success");
  } catch (error) {
    const message = error instanceof ApiError && [502, 503, 504].includes(error.status)
      ? "服务器预检连接超时，请重新点击“检查并匹配邮箱”；本次尚未开始换绑"
      : error instanceof Error ? error.message : "换绑预检失败";
    ui.notify(message, "error", 5200);
  }
}

async function start() {
  if (!canStart.value) return;
  const confirmedReservation = reservationId.value;
  confirmingStart.value = true;
  try {
    if (!(await confirm(`将 ${eligibleCount.value} 个账号换绑到预检匹配的新邮箱。换绑完成后账号库会同步新邮箱和访问凭据。`, { title: "开始邮箱换绑", confirmText: "开始换绑" }))) return;
    if (reservationId.value !== confirmedReservation || !eligible.value || !sourceReady.value || rebind.requestBusy || rebind.checking || progressRunning.value) return;
    await rebind.start(providerType.value, concurrency.value, sourceOptions.value);
    ui.notify("换绑任务已启动", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "换绑任务启动失败", "error");
  } finally {
    confirmingStart.value = false;
  }
}

async function stop() {
  if (!(await confirm("停止当前邮箱换绑任务？正在处理的账号会在当前步骤结束后停止。", { title: "停止邮箱换绑", confirmText: "停止", danger: true }))) return;
  try {
    await rebind.stop();
    ui.notify("已发送停止请求", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "停止换绑失败", "error");
  }
}

async function pasteEmail() {
  if (sourceBusy.value) return;
  pasteError.value = "";
  try {
    const value = String(await navigator.clipboard.readText() || "").trim();
    if (!sourceBusy.value) sourceEmail.value = value;
  } catch (error) {
    pasteError.value = error instanceof Error ? error.message : "读取剪贴板失败";
  }
}

function clearForm() {
  if (sourceBusy.value) return;
  sourceEmail.value = "";
  targetEmail.value = "";
  targetMode.value = "auto";
  providerType.value = "maddy_webui";
  concurrency.value = 1;
  pasteError.value = "";
  rebind.resetForInput();
}

function appendSourceAccounts(emails: string[]) {
  if (sourceBusy.value) return;
  const additions = emails.map((value) => value.trim().toLowerCase()).filter((value) => emailPattern.test(value));
  sourceEmail.value = [...new Set([...sourceEntries.value, ...additions])].join("\n");
}

function selectMailcom(selection: { account_email: string; domain: string }) {
  if (providerType.value !== "mailcom_api") return;
  if (mailcomSelection.value.account_email === selection.account_email && mailcomSelection.value.domain === selection.domain) return;
  mailcomSelection.value = selection;
  rebind.resetForInput();
}

function saveInputState() {
  try {
    localStorage.setItem(INPUT_STATE_KEY, JSON.stringify({
      sourceEmail: sourceEmail.value,
      targetMode: targetMode.value,
      targetEmail: targetEmail.value,
      providerType: providerType.value,
      concurrency: Number(concurrency.value) || 1,
    }));
  } catch { /* 本地存储不可用时仅内存可用 */ }
}

watch([sourceEmail, targetMode, targetEmail, providerType], (value, previous) => {
  if (value[1] !== previous[1] || value[3] !== previous[3]) {
    poolGeneration.value += 1;
    poolEmails.value = [];
    poolProviderRef.value = "";
  }
  saveInputState();
  if (restoringInput.value) return;
  rebind.resetForInput();
}, { flush: "sync" });
watch(concurrency, saveInputState);

onMounted(async () => {
  let prefillEmails: string[] = [];
  try {
    const stored = JSON.parse(sessionStorage.getItem("gpt-register-fp.rebind-prefill.v1") || "null");
    sessionStorage.removeItem("gpt-register-fp.rebind-prefill.v1");
    if (Array.isArray(stored)) {
      prefillEmails = [...new Set(stored.map((value) => String(value || "").trim().toLowerCase()).filter((value) => emailPattern.test(value)))];
    }
  } catch { /* 浏览器存储不可用时继续使用本地表单状态 */ }
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_STATE_KEY) || "null");
    if (!prefillEmails.length && saved && typeof saved === "object") {
      if (typeof saved.sourceEmail === "string") sourceEmail.value = saved.sourceEmail;
      if (["auto", "custom", "pool"].includes(saved.targetMode)) targetMode.value = saved.targetMode;
      if (typeof saved.targetEmail === "string") targetEmail.value = saved.targetEmail;
      if (["maddy_webui", "fastmail_api", "mailcom_api", "icloud_api", "outlook_token"].includes(saved.providerType)) providerType.value = saved.providerType;
      if (Number.isFinite(Number(saved.concurrency))) concurrency.value = Math.min(50, Math.max(1, Number(saved.concurrency)));
    }
  } catch { /* 忽略损坏的本地状态 */ }
  if (prefillEmails.length) sourceEmail.value = prefillEmails.join("\n");
  await nextTick();
  restoringInput.value = false;
  if (targetMode.value === "pool") rebind.resetForInput();
  /* 后台有换绑任务时接管进度；任务结束后刷新账号库 */
  rebind.attachIfRunning();
  if (!accounts.rows.length) void accounts.load();
});
</script>

<template>
  <section class="rebind-panel glass-panel">
    <div class="panel-head">
      <div>
        <h2><MailCheck :size="16" /> 邮箱换绑</h2>
        <p class="panel-sub">验证当前账号后，从所选邮箱池预留一个未占用邮箱并同步新的登录凭据。</p>
      </div>
      <AppTag kind="info"><ArrowLeftRight :size="12" />协议流程</AppTag>
    </div>

    <div class="form-grid">
      <div class="field">
        <label for="new-rebind-source-email">当前 GPT 账号邮箱</label>
        <div class="input-with-btn">
          <textarea
            id="new-rebind-source-email" v-model="sourceEmail" class="input" rows="5" autocomplete="off" spellcheck="false"
            placeholder="每行一个账号邮箱，可粘贴多行"
            :disabled="sourceBusy"
            @keydown.ctrl.enter.prevent="check" @keydown.meta.enter.prevent="check"
          ></textarea>
          <AppButton variant="accent" size="sm" title="从剪贴板粘贴邮箱" :disabled="sourceBusy" @click="pasteEmail">
            <ClipboardPaste :size="14" />
          </AppButton>
        </div>
        <small v-if="sourceEmail && !sourceValid" class="error-text">每行输入一个有效的邮箱地址，可用逗号或空格分隔</small>
        <small v-if="pasteError" class="error-text">{{ pasteError }}</small>
        <RebindAccountPicker :disabled="sourceBusy" @append="appendSourceAccounts" />
      </div>

      <div class="target-box">
        <span class="field-label">新邮箱</span>
        <div class="segmented">
          <button type="button" :class="{ active: targetMode === 'auto' }" :disabled="sourceBusy" @click="targetMode = 'auto'">自动从邮箱池选择</button>
          <button type="button" :class="{ active: targetMode === 'custom' }" :disabled="sourceBusy" @click="targetMode = 'custom'">指定邮箱</button>
          <button type="button" :class="{ active: targetMode === 'pool' }" :disabled="sourceBusy" @click="targetMode = 'pool'">从邮箱库选择</button>
        </div>
        <textarea v-if="targetMode === 'custom'" v-model="targetEmail" class="input" rows="5" autocomplete="off" spellcheck="false" placeholder="每行一个新邮箱，顺序与左侧账号对应" :disabled="sourceBusy"></textarea>
        <RebindMailboxPicker v-else-if="targetMode === 'pool'" :key="poolGeneration" :provider-type="providerType" :disabled="sourceBusy" @selection-change="poolSelectionHandler" />
        <div v-else class="auto-target"><RefreshCw :size="14" /><span>{{ mailcomDeferredTarget ? "预检只核验账号；开始时创建并预留 mail.com 别名" : "预检时从所选邮箱池中原子预留" }}</span></div>
        <small v-if="targetMode === 'custom' && targetEmail && !targetValid" class="error-text">批量指定时，新邮箱数量要与账号数量一致，且格式有效</small>
        <small v-if="targetMode === 'pool'" :class="{ 'error-text': !targetValid }">已选择 {{ poolEmails.length }} 个目标邮箱，需与 {{ validSourceCount }} 个有效账号一一对应，按选择顺序匹配。</small>
        <div class="inline-options">
          <label>邮箱来源
            <select v-model="providerType" class="input" :disabled="sourceBusy">
              <option value="maddy_webui">Maddy 域名邮箱</option>
              <option value="fastmail_api">Fastmail 别名池</option>
              <option value="mailcom_api">mail.com 域名邮箱</option>
              <option value="icloud_api">iCloud / 已导入邮箱</option>
              <option value="outlook_token">Outlook 邮箱</option>
            </select>
          </label>
          <label>并发
            <input v-model.number="concurrency" class="input concurrency" type="number" min="1" max="50" inputmode="numeric" :disabled="sourceBusy" />
          </label>
        </div>
      </div>
    </div>

    <MailcomRebindSettings v-if="providerType === 'mailcom_api'" :disabled="sourceBusy" @selection="selectMailcom" @invalidate="rebind.resetForInput" @ready-change="mailcomReady = $event" @busy-change="mailcomBusy = $event" />

    <div class="actions">
      <AppButton variant="ghost" :disabled="(!sourceEmail && !targetEmail && !poolEmails.length) || sourceBusy" @click="clearForm">
        <Eraser :size="14" /><span>清空</span>
      </AppButton>
      <AppButton variant="primary" :disabled="!canCheck" :loading="rebind.checking" @click="check">
        <RefreshCw :size="14" /><span>{{ rebind.checking ? "检查中" : "检查并匹配邮箱" }}</span>
      </AppButton>
    </div>

    <!-- 预检结果 -->
    <div v-if="hasPreflight" class="preflight" aria-live="polite">
      <div class="result-banner" :class="eligible ? 'ok' : 'warn'">
        <CheckCircle2 v-if="eligible" :size="16" />
        <AlertTriangle v-else :size="16" />
        <span>{{ preflight.message || (eligible ? `已匹配 ${eligibleCount} 个可用目标邮箱` : "预检未通过") }}</span>
      </div>

      <div v-if="preflightItems.length > 1" class="batch-summary">
        <div class="batch-summary-head">
          <strong>逐项预检结果</strong>
          <span>{{ preflight.matched || 0 }} / {{ preflight.requested || preflightItems.length }} 已识别 · 可执行 {{ eligibleCount }} · 已跳过 {{ skippedCount }}</span>
        </div>
        <div class="batch-list">
          <div v-for="(item, index) in preflightItems" :key="item.record_key || item.input || index" class="batch-row">
            <span class="batch-index">{{ index + 1 }}</span>
            <strong>{{ item.source?.email || item.input || "-" }}</strong>
            <span v-if="item.target?.email" class="batch-target">{{ item.target.email }}</span>
            <span v-else class="batch-message">{{ item.message || item.status || "-" }}</span>
            <AppTag :kind="item.status === 'eligible' ? 'success' : item.status === 'missing_credentials' ? 'warning' : 'error'">
              {{ item.status === "eligible" ? "可执行" : item.status === "missing_credentials" ? "已跳过（安全字段不全）" : item.status === "not_found" ? "未找到" : "跳过" }}
            </AppTag>
          </div>
        </div>
      </div>

      <div v-else-if="source || target" class="route">
        <article v-if="source" class="endpoint">
          <span class="endpoint-kicker">当前账号</span>
          <strong>{{ source.email || sourceEmail }}</strong>
          <small v-if="source.account_id">Account ID：{{ source.account_id }}</small>
          <div class="endpoint-tags">
            <AppTag>{{ planLabel(source.plan_type) }}</AppTag>
            <AppTag kind="info">{{ healthLabel(source.health_status) }}</AppTag>
            <AppTag :kind="source.has_password && source.password_confirmed !== false && source.has_totp ? 'success' : 'warning'">
              {{ source.has_password && source.password_confirmed !== false && source.has_totp ? "密码 / 2FA 齐全" : "缺少安全字段" }}
            </AppTag>
          </div>
        </article>
        <ArrowRight v-if="source && target" class="route-arrow" :size="20" />
        <article v-if="target" class="endpoint target">
          <span class="endpoint-kicker">换绑目标</span>
          <strong>{{ target.email || (target.deferred ? "开始时创建并预留" : "-") }}</strong>
          <small>{{ providerLabel(target.provider || providerType) }}<template v-if="target.domain || domainOf(target.email)"> · {{ target.domain || domainOf(target.email) }}</template></small>
          <div class="endpoint-tags">
            <AppTag :kind="target.deferred ? 'warning' : 'success'">{{ target.deferred ? "开始时分配" : "已预留" }}</AppTag>
            <small v-if="target.reserved_at">预检时间 {{ target.reserved_at }}</small>
          </div>
        </article>
      </div>

      <div v-if="invalidFormat.length || invalidTargets.length || notFound.length || missingCredentials.length || conflicts.length" class="issues">
        <div v-if="invalidFormat.length"><b>格式无效</b><span>{{ invalidFormat.join("、") }}</span></div>
        <div v-if="invalidTargets.length"><b>目标格式无效</b><span>{{ invalidTargets.join("、") }}</span></div>
        <div v-if="notFound.length"><b>未找到账号</b><span>{{ notFound.join("、") }}</span></div>
        <div v-if="missingCredentials.length">
          <b>安全字段不全，已跳过</b>
          <span v-for="item in missingCredentials" :key="item.email">{{ item.email }}（{{ (item.missing || []).map(credentialLabel).join("、") }}）</span>
        </div>
        <div v-if="conflicts.length"><b>目标冲突</b><span>{{ conflicts.join("；") }}</span></div>
      </div>

      <div class="preflight-footer">
        <small v-if="eligible">{{ mailcomDeferredTarget ? "预检不创建 mail.com 别名；点击开始后才按需创建，远端已创建的别名不会因换绑取消而删除。" : `候选邮箱只在本次预检中保留一段时间，启动前会再次校验账号状态。${["fastmail_api", "mailcom_api"].includes(providerType) ? " 远端别名会保留，不会因取消预检而恢复。" : ""}` }}</small>
        <small v-else>{{ skippedCount ? `已跳过 ${skippedCount} 个安全字段不全账号；补齐后重新预检。` : "请修正输入或先补齐当前账号的密码和 TOTP 2FA。" }}</small>
        <AppButton v-if="eligible" variant="primary" :disabled="!canStart" :loading="rebind.requestBusy" @click="start">
          <Play :size="14" /><span>开始换绑</span>
        </AppButton>
      </div>
    </div>

    <!-- 换绑进度 -->
    <div v-if="progress.total" class="progress-block" :class="{ active: progressRunning }" aria-live="polite">
      <div class="progress-head">
        <strong>{{ progressRunning ? "邮箱换绑进行中" : progress.status === "completed" ? "邮箱换绑完成" : "最近一次换绑结果" }}</strong>
        <span>{{ progress.done || 0 }} / {{ progress.total || 0 }} · {{ percent }}%</span>
        <AppButton v-if="progressRunning" variant="danger" size="sm" :disabled="progress.stopping" @click="stop">
          <Square :size="12" /><span>{{ progress.stopping ? "停止中" : "停止" }}</span>
        </AppButton>
      </div>
      <div class="track"><i :style="{ width: `${percent}%` }"></i></div>
      <div class="progress-meta">
        <span>{{ progress.current || progress.lastMessage || "等待结果" }}</span>
        <span class="ok-text">成功 {{ progress.success || 0 }}</span>
        <span class="error-text">失败 {{ progress.failed || 0 }}</span>
        <span>已停止 {{ progress.cancelled || 0 }}</span>
      </div>
      <div v-if="progress.message" class="progress-message">{{ progress.message }}</div>
      <div v-if="progress.skipped?.length" class="progress-message error-text">启动前跳过：{{ progress.skipped.join("；") }}</div>
    </div>

    <div v-if="progressItems.length" class="table-wrap">
      <table>
        <thead><tr><th>原邮箱</th><th>新邮箱</th><th>状态</th><th>结果</th><th>AT</th></tr></thead>
        <tbody>
          <tr v-for="(item, index) in progressItems" :key="item.key || `${item.email}-${index}`">
            <td><strong class="email">{{ item.old_email || item.email || "-" }}</strong></td>
            <td><strong class="email">{{ item.new_email || "-" }}</strong></td>
            <td><AppTag :kind="itemStatusKind(item)">{{ itemStatusLabel(item) }}</AppTag></td>
            <td><span class="item-message">{{ item.message || "-" }}</span></td>
            <td><code v-if="item.access_token_masked" class="mono">{{ item.access_token_masked }}</code><span v-else class="muted">-</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="!hasPreflight && !progress.total" class="empty-hint">
      <ShieldCheck :size="18" /><span>输入一个或多个当前 GPT 账号邮箱后开始预检</span>
    </div>
  </section>
</template>

<style scoped>
.rebind-panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.panel-head h2 { display: flex; align-items: center; gap: 8px; font-size: 1.05rem; color: var(--text-primary); }
.panel-sub { font-size: 0.8rem; color: var(--text-muted); margin-top: 3px; }

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.field { display: flex; flex-direction: column; gap: 6px; font-size: 0.8rem; color: var(--text-secondary); }
.field textarea { resize: vertical; font-family: var(--font-mono, monospace); font-size: 0.8rem; }
.field-label { font-size: 0.8rem; color: var(--text-secondary); }

.input-with-btn { display: flex; gap: 8px; align-items: flex-start; }
.input-with-btn textarea { flex: 1; }

.target-box { display: flex; flex-direction: column; gap: 8px; }

.segmented {
  display: inline-flex;
  flex-wrap: wrap;
  padding: 3px;
  border-radius: 10px;
  background: var(--inset-bg);
  border: 1px solid var(--line-subtle);
  gap: 2px;
  width: fit-content;
}

.segmented button {
  padding: 6px 14px;
  border-radius: 8px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 0.8rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.segmented button.active { background: var(--accent); color: #fff; }
.segmented button:disabled { opacity: 0.5; cursor: not-allowed; }

.auto-target {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  border: 1px dashed var(--line-subtle);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 0.8rem;
}

.inline-options { display: flex; gap: 16px; flex-wrap: wrap; }
.inline-options label { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--text-secondary); }
.inline-options select { width: auto; }
.concurrency { width: 72px; text-align: center; }

.actions { display: flex; gap: 8px; }

.error-text { color: var(--error); }
.ok-text { color: var(--success); }
.muted { color: var(--text-muted); }
.mono { font-family: var(--font-mono, monospace); font-size: 0.75rem; }

.result-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  font-size: 0.84rem;
}

.result-banner.ok { background: var(--success-bg); color: var(--success); }
.result-banner.warn { background: var(--warning-bg); color: var(--warning); }

.batch-summary { border: 1px solid var(--line-subtle); border-radius: var(--radius-sm); overflow: hidden; }
.batch-summary-head {
  display: flex;
  justify-content: space-between;
  padding: 8px 12px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  background: var(--inset-bg);
}

.batch-list { max-height: 220px; overflow: auto; }
.batch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  border-top: 1px solid var(--line-subtle);
  font-size: 0.8rem;
}
.batch-index { color: var(--text-muted); font-size: 0.72rem; width: 20px; }
.batch-target { color: var(--accent); }
.batch-message { color: var(--text-muted); flex: 1; }
.batch-row strong { color: var(--text-primary); }

.route { display: flex; align-items: stretch; gap: 12px; }
.endpoint {
  flex: 1;
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.endpoint-kicker { font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; }
.endpoint strong { color: var(--text-primary); overflow-wrap: anywhere; }
.endpoint small { color: var(--text-muted); }
.endpoint-tags { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.route-arrow { align-self: center; color: var(--accent); flex-shrink: 0; }

.issues {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.78rem;
  color: var(--warning);
}
.issues b { margin-right: 8px; }
.issues span { margin-right: 12px; }

.preflight-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.preflight-footer small { color: var(--text-muted); }

.preflight { display: flex; flex-direction: column; gap: 12px; }

.progress-block {
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--inset-bg);
}

.progress-head { display: flex; align-items: center; gap: 12px; font-size: 0.82rem; color: var(--text-primary); }
.progress-head strong { flex: 1; }

.track { height: 6px; border-radius: 4px; background: var(--line-subtle); overflow: hidden; }
.track i { display: block; height: 100%; border-radius: 4px; background: var(--accent); transition: width 0.3s; }

.progress-meta { display: flex; gap: 14px; flex-wrap: wrap; font-size: 0.76rem; color: var(--text-secondary); }
.progress-message { font-size: 0.78rem; color: var(--text-secondary); }

.table-wrap { overflow: auto; max-height: 44vh; border: 1px solid var(--line-subtle); border-radius: var(--radius-sm); }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line-subtle); }
th { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; background: var(--inset-bg); position: sticky; top: 0; }
.email { color: var(--text-primary); }
.item-message { font-size: 0.78rem; color: var(--text-secondary); }

.empty-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 36px 0;
  color: var(--text-muted);
  font-size: 0.84rem;
}

@media (max-width: 768px) {
  .form-grid { grid-template-columns: 1fr; }
  .route { flex-direction: column; }
  .route-arrow { transform: rotate(90deg); }
}
</style>
