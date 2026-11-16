<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
  AlertTriangle,
  ArrowLeftRight,
  ArrowRight,
  CheckCircle2,
  ClipboardPaste,
  Eraser,
  MailCheck,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
} from "@lucide/vue";
import MailcomRebindSettings from "./MailcomRebindSettings.vue";
import RebindAccountPicker from "./RebindAccountPicker.vue";
import RebindMailboxPicker from "./RebindMailboxPicker.vue";

const props = defineProps({
  progress: { type: Object, default: () => ({}) },
  preflight: { type: Object, default: () => ({}) },
  initialEmails: { type: Array, default: () => [] },
  busy: Boolean,
  checking: Boolean,
});

const emit = defineEmits(["preflight", "start", "stop", "reset", "input-change", "initial-applied"]);

const INPUT_STATE_KEY = "gpt-register-fp.rebind-input.v1";
const sourceEmail = ref("");
const targetMode = ref("auto");
const targetEmail = ref("");
const providerType = ref("maddy_webui");
const concurrency = ref(1);
const pasteError = ref("");
const restoringInput = ref(true);
const mailcomSelection = ref({ account_email: "", domain: "random" });
const mailcomReady = ref(false);
const mailcomBusy = ref(false);
const poolEmails = ref([]);
const poolProviderRef = ref("");
const poolGeneration = ref(0);

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function splitEntries(value) {
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter(Boolean);
}
const sourceEntries = computed(() => {
  const values = [];
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
const progressRunning = computed(() => !!props.progress?.running || ["queued", "running", "stopping"].includes(String(props.progress?.status || "")));
const reservationId = computed(() => String(props.preflight?.reservationId || props.preflight?.reservation_id || "").trim());
const eligible = computed(() => props.preflight?.eligible === true && !!reservationId.value);
const sourceBusy = computed(() => props.busy || props.checking || progressRunning.value || (providerType.value === "mailcom_api" && mailcomBusy.value));
const sourceReady = computed(() => providerType.value !== "mailcom_api" || mailcomReady.value);
const mailcomDeferredTarget = computed(() => providerType.value === "mailcom_api" && targetMode.value === "auto");
const canCheck = computed(() => sourceEntries.value.some((value) => emailPattern.test(value)) && targetValid.value && sourceReady.value && !sourceBusy.value);
const canStart = computed(() => eligible.value && targetValid.value && sourceReady.value && !sourceBusy.value);
const sourceOptions = computed(() => ({
  ...(targetMode.value === "pool" && poolProviderRef.value ? { provider_ref: poolProviderRef.value } : {}),
  // Keep automatic allocation on the complete configured pool; only an
  // explicit target may pin one primary mailbox.
  ...(providerType.value === "mailcom_api" && targetMode.value !== "auto"
    ? { mailcom_account: mailcomSelection.value.account_email, mailcom_domain: mailcomSelection.value.domain }
    : providerType.value === "mailcom_api" ? { mailcom_domain: mailcomSelection.value.domain } : {}),
}));
const poolSelectionHandler = computed(() => {
  const provider = providerType.value;
  const generation = poolGeneration.value;
  return (selection) => {
    if (sourceBusy.value || targetMode.value !== "pool" || provider !== providerType.value || generation !== poolGeneration.value) return;
    const emails = [...new Set((selection.emails || []).map((value) => String(value).trim().toLowerCase()).filter(Boolean))];
    const providerRef = String(selection.provider_ref || "");
    if (poolEmails.value.join("\n") === emails.join("\n") && poolProviderRef.value === providerRef) return;
    poolEmails.value = emails;
    poolProviderRef.value = providerRef;
    emit("input-change");
  };
});
const percent = computed(() => {
  const total = Number(props.progress?.total || 0);
  const done = Number(props.progress?.done || 0);
  return total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
});
const hasPreflight = computed(() => {
  const value = props.preflight || {};
  return Boolean(
    value.message
      || value.source
      || value.target
      || value.notFound?.length
      || value.invalidFormat?.length
      || value.invalid_format?.length
      || value.missingCredentials?.length
      || value.missing_credentials?.length
      || value.conflicts?.length
      || value.items?.length,
  );
});
const source = computed(() => props.preflight?.source || props.preflight?.source_account || null);
const target = computed(() => props.preflight?.target || null);
const preflightItems = computed(() => Array.isArray(props.preflight?.items) ? props.preflight.items : []);
const eligibleCount = computed(() => Number(props.preflight?.eligibleCount || props.preflight?.eligible_count || (props.preflight?.eligible ? 1 : 0)));
const invalidFormat = computed(() => props.preflight?.invalidFormat || props.preflight?.invalid_format || []);
const notFound = computed(() => props.preflight?.notFound || props.preflight?.not_found || []);
const missingCredentials = computed(() => props.preflight?.missingCredentials || props.preflight?.missing_credentials || []);
const skippedCount = computed(() => Number(props.preflight?.skippedCount ?? props.preflight?.skipped_count ?? missingCredentials.value.length));
const conflicts = computed(() => props.preflight?.conflicts || []);
const progressItems = computed(() => Array.isArray(props.progress?.items) ? props.progress.items : []);

function providerLabel(value) {
  const provider = String(value || "maddy_webui");
  if (provider === "fastmail_api") return "Fastmail 别名池";
  if (provider === "mailcom_api") return "mail.com 域名邮箱";
  if (provider === "icloud_api") return "iCloud / 已导入邮箱";
  if (provider === "outlook_token") return "Outlook 邮箱";
  return "Maddy 域名邮箱";
}

function credentialLabel(value) {
  return ({ password: "密码", password_unconfirmed: "密码未确认", totp: "TOTP 2FA", totp_secret: "TOTP 2FA" }[String(value || "").toLowerCase()] || String(value || "安全字段"));
}

function healthLabel(value) {
  return ({ alive: "可用", limited: "限流", reauth_required: "待续期", invalid: "失效", error: "错误", unknown: "未检测" }[String(value || "unknown").toLowerCase()] || String(value || "未检测"));
}

function planLabel(value) {
  const plan = String(value || "free").trim().toLowerCase();
  return plan === "plus" ? "Plus" : plan === "free" ? "Free" : plan.toUpperCase();
}

function itemStatusLabel(item) {
  const status = String(item?.status || "queued").toLowerCase();
  return ({ queued: "排队", running: "进行中", success: "成功", completed: "成功", failed: "失败", cancelled: "已停止", stopped: "已停止" }[status] || status);
}

function itemStatusClass(item) {
  const status = String(item?.status || "queued").toLowerCase();
  if (["success", "completed"].includes(status)) return "success";
  if (["failed"].includes(status)) return "failed";
  if (["cancelled", "stopped"].includes(status)) return "cancelled";
  return "running";
}

function domainOf(value) {
  const text = String(value || "");
  return text.includes("@") ? text.split("@").slice(1).join("@") : "";
}

function check() {
  if (!canCheck.value) return;
  emit("preflight", {
    text: String(sourceEmail.value || "").trim(),
    new_email: targetMode.value === "pool" ? poolEmails.value.join("\n") : targetMode.value === "custom" ? String(targetEmail.value || "").trim() : "",
    provider_type: providerType.value,
    ...sourceOptions.value,
  });
}

function start() {
  if (!canStart.value) return;
  emit("start", {
    reservation_id: reservationId.value,
    provider_type: providerType.value,
    ...sourceOptions.value,
    concurrency: Math.min(50, Math.max(1, Math.floor(Number(concurrency.value) || 1))),
  });
}

async function pasteEmail() {
  if (sourceBusy.value) return;
  pasteError.value = "";
  try {
    const value = String(await navigator.clipboard.readText() || "").trim();
    if (!sourceBusy.value) sourceEmail.value = value;
  } catch (error) {
    pasteError.value = error?.message || "读取剪贴板失败";
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
  emit("reset");
}

function appendSourceAccounts(emails) {
  if (sourceBusy.value) return;
  const additions = emails.map((value) => String(value).trim().toLowerCase()).filter((value) => emailPattern.test(value));
  sourceEmail.value = [...new Set([...sourceEntries.value, ...additions])].join("\n");
}

function selectMailcom(selection) {
  if (providerType.value !== "mailcom_api") return;
  if (mailcomSelection.value.account_email === selection.account_email && mailcomSelection.value.domain === selection.domain) return;
  mailcomSelection.value = selection;
  emit("input-change");
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
  } catch {
    // Local storage can be disabled; the page remains usable in memory.
  }
}

onMounted(async () => {
  const initialEmails = [...new Set(
    (Array.isArray(props.initialEmails) ? props.initialEmails : [])
      .map((value) => String(value || "").trim().toLowerCase())
      .filter((value) => emailPattern.test(value)),
  )];
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_STATE_KEY) || "null");
    if (!initialEmails.length && saved && typeof saved === "object") {
      if (typeof saved.sourceEmail === "string") sourceEmail.value = saved.sourceEmail;
      if (["auto", "custom", "pool"].includes(saved.targetMode)) targetMode.value = saved.targetMode;
      if (typeof saved.targetEmail === "string") targetEmail.value = saved.targetEmail;
      if (["maddy_webui", "fastmail_api", "mailcom_api", "icloud_api", "outlook_token"].includes(saved.providerType)) providerType.value = saved.providerType;
      if (Number.isFinite(Number(saved.concurrency))) concurrency.value = Math.min(50, Math.max(1, Number(saved.concurrency)));
    }
  } catch {
    // Ignore malformed or unavailable local state.
  }
  if (initialEmails.length) sourceEmail.value = initialEmails.join("\n");
  // Let the initial v-model restoration flush before input changes can clear
  // a server-side preflight restored by the parent shell.
  await nextTick();
  restoringInput.value = false;
  // Pool selections are loaded afresh; a restored reservation cannot supply them.
  if (targetMode.value === "pool") emit("input-change");
  if (initialEmails.length) emit("initial-applied");
});

watch([sourceEmail, targetMode, targetEmail, providerType], (value, previous) => {
  if (value[1] !== previous[1] || value[3] !== previous[3]) {
    poolGeneration.value += 1;
    poolEmails.value = [];
    poolProviderRef.value = "";
  }
  saveInputState();
  if (restoringInput.value) return;
  emit("input-change");
}, { flush: "sync" });
watch(concurrency, saveInputState);
</script>

<template>
  <section class="card rebind-panel">
    <div class="card-h rebind-head">
      <div>
        <h2><MailCheck :size="16" aria-hidden="true" />邮箱换绑</h2>
        <p class="subtle">验证当前账号后，从所选邮箱池预留一个未占用邮箱并同步新的登录凭据。</p>
      </div>
      <span class="rebind-mode"><ArrowLeftRight :size="14" aria-hidden="true" />协议流程</span>
    </div>

    <div class="rebind-form-grid">
      <div class="rebind-field rebind-source-field">
        <label for="rebind-source-email">当前 GPT 账号邮箱</label>
        <div class="rebind-input-with-actions">
          <textarea id="rebind-source-email" v-model="sourceEmail" rows="5" autocomplete="off" spellcheck="false" placeholder="每行一个账号邮箱，可粘贴多行" :disabled="sourceBusy" @keydown.ctrl.enter.prevent="check" @keydown.meta.enter.prevent="check"></textarea>
          <button class="btn secondary rebind-icon-btn" type="button" title="从剪贴板粘贴邮箱" aria-label="从剪贴板粘贴邮箱" :disabled="sourceBusy" @click="pasteEmail"><ClipboardPaste :size="15" aria-hidden="true" /></button>
        </div>
        <small v-if="sourceEmail && !sourceValid" class="rebind-error">每行输入一个有效的邮箱地址，可用逗号或空格分隔</small>
        <small v-if="pasteError" class="rebind-error">{{ pasteError }}</small>
        <RebindAccountPicker :disabled="sourceBusy" @append="appendSourceAccounts" />
      </div>

      <div class="rebind-target-box">
        <span class="rebind-field-label">新邮箱</span>
        <div class="rebind-mode-switch" role="tablist" aria-label="新邮箱来源">
          <button type="button" :class="{ active: targetMode === 'auto' }" :disabled="sourceBusy" @click="targetMode = 'auto'">自动从邮箱池选择</button>
          <button type="button" :class="{ active: targetMode === 'custom' }" :disabled="sourceBusy" @click="targetMode = 'custom'">指定邮箱</button>
          <button type="button" :class="{ active: targetMode === 'pool' }" :disabled="sourceBusy" @click="targetMode = 'pool'">从邮箱库选择</button>
        </div>
        <textarea v-if="targetMode === 'custom'" v-model="targetEmail" rows="5" autocomplete="off" spellcheck="false" placeholder="每行一个新邮箱，顺序与左侧账号对应" :disabled="sourceBusy"></textarea>
        <RebindMailboxPicker v-else-if="targetMode === 'pool'" :key="poolGeneration" :provider-type="providerType" :disabled="sourceBusy" @selection-change="poolSelectionHandler" />
        <div v-else class="rebind-auto-target"><RefreshCw :size="14" aria-hidden="true" /><span>{{ mailcomDeferredTarget ? "预检只核验账号；开始时创建并预留 mail.com 别名" : "预检时从所选邮箱池中原子预留" }}</span></div>
        <small v-if="targetMode === 'custom' && targetEmail && !targetValid" class="rebind-error">批量指定时，新邮箱数量要与账号数量一致，且格式有效</small>
        <small v-if="targetMode === 'pool'" :class="{ 'rebind-error': !targetValid }">已选择 {{ poolEmails.length }} 个目标邮箱，需与 {{ validSourceCount }} 个有效账号一一对应，按选择顺序匹配。</small>
        <div class="rebind-inline-options">
          <label>邮箱来源<select v-model="providerType" :disabled="sourceBusy"><option value="maddy_webui">Maddy 域名邮箱</option><option value="fastmail_api">Fastmail 别名池</option><option value="mailcom_api">mail.com 域名邮箱</option><option value="icloud_api">iCloud / 已导入邮箱</option><option value="outlook_token">Outlook 邮箱</option></select></label>
          <label>并发<input v-model.number="concurrency" type="number" min="1" max="50" inputmode="numeric" :disabled="sourceBusy" /></label>
        </div>
      </div>
    </div>

    <MailcomRebindSettings v-if="providerType === 'mailcom_api'" :disabled="sourceBusy" @selection="selectMailcom" @invalidate="emit('input-change')" @ready-change="mailcomReady = $event" @busy-change="mailcomBusy = $event" />

    <div class="rebind-actions">
      <button class="btn secondary" type="button" :disabled="(!sourceEmail && !targetEmail && !poolEmails.length) || sourceBusy" @click="clearForm"><Eraser :size="15" aria-hidden="true" />清空</button>
      <button class="btn primary" type="button" :disabled="!canCheck" @click="check"><RefreshCw :size="15" aria-hidden="true" />{{ props.checking ? "检查中" : "检查并匹配邮箱" }}</button>
    </div>

    <div v-if="hasPreflight" class="rebind-preflight" aria-live="polite">
      <div class="rebind-result-banner" :class="eligible ? 'ok' : 'warn'">
        <CheckCircle2 v-if="eligible" :size="16" aria-hidden="true" />
        <AlertTriangle v-else :size="16" aria-hidden="true" />
        <span>{{ props.preflight.message || (eligible ? `已匹配 ${eligibleCount} 个可用目标邮箱` : "预检未通过") }}</span>
      </div>

      <div v-if="preflightItems.length > 1" class="rebind-batch-summary">
        <div class="rebind-batch-summary-head"><strong>逐项预检结果</strong><span>{{ props.preflight.matched || 0 }} / {{ props.preflight.requested || preflightItems.length }} 已识别 · 可执行 {{ eligibleCount }} · 已跳过 {{ skippedCount }}</span></div>
        <div class="rebind-batch-list">
          <div v-for="(item, index) in preflightItems" :key="item.record_key || item.input || index" class="rebind-batch-row">
            <span class="rebind-batch-index">{{ index + 1 }}</span>
            <strong>{{ item.source?.email || item.input || "-" }}</strong>
            <span v-if="item.target?.email" class="rebind-batch-target">{{ item.target.email }}</span>
            <span v-else class="rebind-batch-message">{{ item.message || item.status || "-" }}</span>
            <span :class="['rebind-item-status', item.status === 'eligible' ? 'success' : (item.status === 'missing_credentials' ? 'warn' : 'failed')]">{{ item.status === 'eligible' ? "可执行" : (item.status === 'missing_credentials' ? "已跳过（安全字段不全）" : item.status === 'not_found' ? "未找到" : "跳过") }}</span>
          </div>
        </div>
      </div>

      <div v-else-if="source || target" class="rebind-route">
        <article v-if="source" class="rebind-endpoint">
          <span class="rebind-endpoint-kicker">当前账号</span>
          <strong>{{ source.email || sourceEmail }}</strong>
          <small v-if="source.account_id">Account ID：{{ source.account_id }}</small>
          <div class="rebind-endpoint-tags"><span class="tag">{{ planLabel(source.plan_type) }}</span><span class="tag">{{ healthLabel(source.health_status) }}</span><span :class="['tag', source.has_password && source.password_confirmed !== false && source.has_totp ? 'ok' : 'warn']">{{ source.has_password && source.password_confirmed !== false && source.has_totp ? "密码 / 2FA 齐全" : "缺少安全字段" }}</span></div>
        </article>
        <ArrowRight v-if="source && target" class="rebind-route-arrow" :size="20" aria-hidden="true" />
        <article v-if="target" class="rebind-endpoint target">
          <span class="rebind-endpoint-kicker">换绑目标</span>
          <strong>{{ target.email || (target.deferred ? "开始时创建并预留" : "-") }}</strong>
          <small>{{ providerLabel(target.provider || providerType) }}<template v-if="target.domain || domainOf(target.email)"> · {{ target.domain || domainOf(target.email) }}</template></small>
          <div class="rebind-endpoint-tags"><span :class="['tag', target.deferred ? 'warn' : 'ok']">{{ target.deferred ? "开始时分配" : "已预留" }}</span><small v-if="target.reserved_at">预检时间 {{ target.reserved_at }}</small></div>
        </article>
      </div>

      <div v-if="invalidFormat.length || notFound.length || missingCredentials.length || conflicts.length || props.preflight.invalidTargets?.length" class="rebind-issues">
        <div v-if="invalidFormat.length"><b>格式无效</b><span>{{ invalidFormat.join("、") }}</span></div>
        <div v-if="notFound.length"><b>未找到账号</b><span>{{ notFound.join("、") }}</span></div>
        <div v-if="missingCredentials.length"><b>安全字段不全，已跳过</b><span v-for="item in missingCredentials" :key="item.email">{{ item.email }}（{{ (item.missing || []).map(credentialLabel).join("、") }}）</span></div>
        <div v-if="conflicts.length"><b>目标冲突</b><span>{{ conflicts.join("；") }}</span></div>
        <div v-if="props.preflight.invalidTargets?.length"><b>目标格式无效</b><span>{{ props.preflight.invalidTargets.join("、") }}</span></div>
      </div>

      <div class="rebind-preflight-footer">
        <small v-if="eligible">{{ mailcomDeferredTarget ? "预检不创建 mail.com 别名；点击开始后才按需创建，远端已创建的别名不会因换绑取消而删除。" : `候选邮箱只在本次预检中保留一段时间，启动前会再次校验账号状态。${['fastmail_api', 'mailcom_api'].includes(providerType) ? ' 远端别名会保留，不会因取消预检而恢复。' : ''}` }}</small>
        <small v-else>{{ skippedCount ? `已跳过 ${skippedCount} 个安全字段不全账号；补齐后重新预检。` : "请修正输入或先补齐当前账号的密码和 TOTP 2FA。" }}</small>
        <button v-if="eligible" class="btn primary" type="button" :disabled="!canStart" @click="start"><Play :size="15" aria-hidden="true" />开始换绑</button>
      </div>
    </div>

    <div v-if="props.progress?.total" class="rebind-progress" :class="{ active: progressRunning }" aria-live="polite">
      <div class="rebind-progress-head"><strong>{{ progressRunning ? "邮箱换绑进行中" : (props.progress?.status === 'completed' ? "邮箱换绑完成" : "最近一次换绑结果") }}</strong><span>{{ props.progress.done || 0 }} / {{ props.progress.total || 0 }} · {{ percent }}%</span><button v-if="progressRunning" class="btn danger rebind-stop" type="button" :disabled="props.progress.stopping" @click="emit('stop')"><Square :size="13" aria-hidden="true" />{{ props.progress.stopping ? "停止中" : "停止" }}</button></div>
      <div class="rebind-track"><i :style="{ width: `${percent}%` }"></i></div>
      <div class="rebind-progress-meta"><span class="rebind-current">{{ props.progress.current || props.progress.lastMessage || "等待结果" }}</span><span class="ok-text">成功 {{ props.progress.success || 0 }}</span><span class="danger-text">失败 {{ props.progress.failed || 0 }}</span><span>已停止 {{ props.progress.cancelled || 0 }}</span></div>
      <div v-if="props.progress.message" class="rebind-progress-message">{{ props.progress.message }}</div>
      <div v-if="props.progress.skipped?.length" class="rebind-progress-message danger-text">启动前跳过：{{ props.progress.skipped.join("；") }}</div>
    </div>

    <div v-if="progressItems.length" class="rebind-items-wrap">
      <table class="rebind-items">
        <thead><tr><th>原邮箱</th><th>新邮箱</th><th>状态</th><th>结果</th><th>AT</th></tr></thead>
        <tbody>
          <tr v-for="(item, index) in progressItems" :key="item.key || `${item.email}-${index}`">
            <td><strong>{{ item.old_email || item.email || "-" }}</strong></td>
            <td><strong>{{ item.new_email || "-" }}</strong></td>
            <td><span :class="['rebind-item-status', itemStatusClass(item)]">{{ itemStatusLabel(item) }}</span></td>
            <td><span class="rebind-item-message">{{ item.message || "-" }}</span></td>
            <td><code v-if="item.access_token_masked">{{ item.access_token_masked }}</code><span v-else class="muted">-</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="!hasPreflight && !props.progress?.total" class="rebind-empty"><ShieldCheck :size="18" aria-hidden="true" /><span>输入一个或多个当前 GPT 账号邮箱后开始预检</span></div>
  </section>
</template>
