<script setup lang="ts">
/* --- 批量操作条：选中账号后底部吸附（含接码/刷新AT/试用/重登/安全修复/改密/改2FA） --- */
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import {
  Activity, ArrowLeftRight, Copy, Download, FileDown, FolderInput, KeyRound, LogIn, ShieldCheck,
  ShieldEllipsis, Smartphone, Sparkles, Trash2, X, Zap,
} from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useAccountsStore } from "../../stores/accounts";
import { useGroupsStore } from "../../stores/groups";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import {
  downloadAccountsJson, exportAccessTokens, exportSecurityCredentials, fetchAccessTokens,
} from "../../api/accounts";
import { checkAccountsPromo } from "../../api/accounts";
import { fetchUiSettings } from "../../api/settings";
import { useCopy } from "../../composables/useCopy";
import type { BatchKind } from "./BatchStartDialog.vue";

const emit = defineEmits<{
  startBatch: [kind: BatchKind];
  codex: [];
}>();

const accounts = useAccountsStore();
const groups = useGroupsStore();
const batch = useBatchOpsStore();
const ui = useUiStore();
const confirm = useConfirm();
const router = useRouter();
const { copyText } = useCopy();

const count = computed(() => accounts.selectedKeys.length);
const busy = ref(false);
const promoConcurrency = ref(5);

async function run(action: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true;
  try {
    await action();
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "操作失败", "error");
  } finally {
    busy.value = false;
  }
}

function healthSelected() {
  run(async () => {
    const settings = await fetchUiSettings().catch(() => null);
    const workers = Math.max(1, Math.min(20, Math.floor(Number(settings?.health_workers) || 20)));
    const savedBatchSize = Number(settings?.account_task_batch_size);
    const batchSize = Number.isSafeInteger(savedBatchSize) && savedBatchSize >= 1 && savedBatchSize <= 100000 ? savedBatchSize : 2000;
    await accounts.startHealth(accounts.selectedKeys, false, workers, batchSize);
    ui.notify(`已开始测活 ${count.value} 个账号`, "success");
    accounts.clearSelection();
  });
}

function copyTokens() {
  run(async () => {
    const result = await fetchAccessTokens(accounts.selectedKeys);
    const ok = await copyText(result.tokens.join("\n"), "batch-at");
    if (ok) {
      ui.notify(
        `已复制 ${result.exported} 条 AT${result.missing ? `，${result.missing} 条缺失` : ""}`,
        result.missing ? "warning" : "success",
      );
    }
  });
}

function downloadJson() {
  run(async () => {
    await downloadAccountsJson(accounts.selectedKeys);
    ui.notify("已开始下载 JSON", "success");
  });
}

function openRebind() {
  if (busy.value || accounts.loading) return;
  const selected = accounts.rows.filter((account) => accounts.selectedSet.has(account.delete_key));
  if (selected.length !== count.value) {
    ui.notify("所选账号尚未完整加载，请刷新账号列表后重试", "warning");
    return;
  }
  const emails = [...new Set(selected.map((account) => String(account.email || "").trim().toLowerCase()).filter(Boolean))];
  if (!emails.length || emails.length !== selected.length) {
    ui.notify("所选账号缺少邮箱，请检查后重新选择", "warning");
    return;
  }
  try {
    sessionStorage.setItem("gpt-register-fp.rebind-prefill.v1", JSON.stringify(emails));
  } catch {
    ui.notify("浏览器暂时无法保存换绑选择", "error");
    return;
  }
  void router.push("/accounts/rebind");
}

function exportSecurity() {
  run(async () => {
    const result = await exportSecurityCredentials(accounts.selectedKeys);
    ui.notify(`已导出 ${result.exported} 条凭据${result.missing ? `，${result.missing} 条缺失` : ""}`, "success");
  });
}

function exportTokens() {
  run(async () => {
    const result = await exportAccessTokens(accounts.selectedKeys);
    ui.notify(`已导出 ${result.exported} 条 AT`, "success");
  });
}

function moveTo(groupId: string) {
  if (!groupId) return;
  run(async () => {
    const name = groups.nameOf(groupId);
    await groups.move(groupId, accounts.selectedKeys);
    ui.notify(`已移动 ${count.value} 个账号到「${name}」`, "success");
    accounts.clearSelection();
    await accounts.load();
  });
}

function removeSelected() {
  run(async () => {
    if (!(await confirm(`删除选中的 ${count.value} 个账号？将移入回收站。`, { title: "批量删除", confirmText: "删除", danger: true }))) return;
    const result = await accounts.remove(accounts.selectedKeys);
    ui.notify(`已删除 ${result.removed} 个账号`, "success");
  });
}

/* --- 接码：完整跨页选择进入统一设置与预检，不直接取号 --- */
function codexSelected() {
  return run(async () => {
    if (accounts.loading) {
      ui.notify("账号列表正在加载，请加载完成后再接码", "warning");
      return;
    }
    // 选择会跨页保留；必须使用完整列表，并在任何选中记录缺失时停止。
    const selected = accounts.selectedSet;
    const selectedRows = accounts.rows.filter((a) => selected.has(a.delete_key));
    if (selectedRows.length !== selected.size) {
      throw new Error("所选账号已变化或尚未加载完整，请刷新账号列表并重新选择");
    }
    const emails = selectedRows.map((a) => String(a.email || "").trim()).filter(Boolean);
    if (!emails.length || emails.length !== selectedRows.length) {
      throw new Error("所选账号缺少邮箱，请检查后重新选择");
    }
    batch.openCodexStart(emails);
    emit("codex");
  });
}

/* --- 打开启动参数弹窗的批次 --- */
function openStart(kind: BatchKind) {
  emit("startBatch", kind);
}

function checkPromoSelected() {
  run(async () => {
    const concurrency = Math.max(1, Math.floor(Number(promoConcurrency.value) || 1));
    promoConcurrency.value = concurrency;
    const result = await checkAccountsPromo([...accounts.selectedKeys], [], 2, concurrency);
    await accounts.load();
    ui.notify(`优惠检测完成：${result.promo || 0} 个发现优惠，${result.failed || 0} 个失败`, result.failed ? "warning" : "success");
  });
}
</script>

<template>
  <Transition name="batch">
    <div v-if="count" class="batch-bar glass-panel">
      <span class="batch-count">已选 <b>{{ count }}</b> 个账号</span>

      <div class="batch-actions">
        <AppButton variant="primary" size="sm" :disabled="busy" @click="healthSelected">
          <Activity :size="14" /><span>测活</span>
        </AppButton>
        <AppButton variant="primary" size="sm" :disabled="busy || accounts.loading" title="把选中账号带入邮箱换绑页" @click="openRebind">
          <ArrowLeftRight :size="14" /><span>换绑</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.codex.running || batch.codexStartBusy" title="设置选中账号的接码参数并预检" aria-label="设置选中账号的接码任务" @click="codexSelected">
          <Smartphone :size="14" /><span>接码</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" title="复制选中账号的 Access Token" @click="copyTokens">
          <Copy :size="14" /><span>复制 AT</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" title="导出邮箱----密码----2FA" @click="exportSecurity">
          <ShieldCheck :size="14" /><span>导出凭据</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" @click="exportTokens">
          <FileDown :size="14" /><span>导出 AT</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" @click="downloadJson">
          <Download :size="14" /><span>下载 JSON</span>
        </AppButton>

        <span class="batch-divider" aria-hidden="true"></span>

        <AppButton variant="ghost" size="sm" :disabled="busy || batch.refreshAt.running" title="通过 Session 刷新选中账号的 Access Token" @click="openStart('refreshAt')">
          <Zap :size="14" /><span>刷新AT</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.trial.running" title="检测选中账号的 Plus 免费试用资格" @click="openStart('trial')">
          <Sparkles :size="14" /><span>试用检测</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" title="读取选中账号的优惠金额、支付方式和结账通道，不提交付款" @click="checkPromoSelected">
          <Sparkles :size="14" /><span>查优惠</span>
        </AppButton>
        <label class="promo-concurrency" title="优惠检测同时处理的账号数">
          <span>优惠并发</span>
          <input v-model.number="promoConcurrency" class="input" type="number" min="1" step="1" aria-label="优惠检测并发数" />
        </label>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="使用已保存的密码和 2FA 重新登录" @click="openStart('reauth')">
          <LogIn :size="14" /><span>重新登录</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="补全缺失的密码和 TOTP 2FA" @click="openStart('security_repair')">
          <ShieldEllipsis :size="14" /><span>安全修复</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="修改选中账号的登录密码" @click="openStart('password_change')">
          <KeyRound :size="14" /><span>改密</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="更换选中账号的 TOTP 2FA 因子" @click="openStart('totp_change')">
          <FolderInput :size="14" /><span>改2FA</span>
        </AppButton>

        <span class="batch-divider" aria-hidden="true"></span>

        <select class="input move-select" :value="''" aria-label="移动到分组" @change="moveTo(($event.target as HTMLSelectElement).value); ($event.target as HTMLSelectElement).value = ''">
          <option value="" disabled selected>移动到分组…</option>
          <option v-for="group in groups.items" :key="group.id" :value="group.id">{{ group.name }}</option>
        </select>

        <AppButton variant="danger" size="sm" :disabled="busy" @click="removeSelected">
          <Trash2 :size="14" /><span>删除</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" title="清除选择" aria-label="清除选择" @click="accounts.clearSelection()">
          <X :size="14" />
        </AppButton>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.batch-bar {
  position: sticky;
  bottom: 12px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  flex-wrap: wrap;
}

.batch-count {
  font-size: 0.85rem;
  color: var(--text-secondary);
  white-space: nowrap;
}

.batch-count b {
  color: var(--accent);
}

.batch-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.move-select {
  width: auto;
  min-width: 128px;
  padding: 6px 10px;
  font-size: 0.78rem;
  cursor: pointer;
}

.promo-concurrency {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text-secondary);
  font-size: 0.74rem;
  white-space: nowrap;
}

.promo-concurrency .input {
  width: 58px;
  min-height: 32px;
  padding: 4px 7px;
  font-size: 0.78rem;
}

.batch-divider {
  width: 1px;
  height: 20px;
  background: var(--line-subtle);
  margin: 0 2px;
}

.batch-enter-active,
.batch-leave-active {
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.batch-enter-from,
.batch-leave-to {
  opacity: 0;
  transform: translateY(12px);
}

@media (max-width: 768px) {
  .batch-bar {
    bottom: 8px;
  }

  .batch-actions :deep(.btn span) {
    display: none;
  }

  .move-select {
    min-width: 110px;
  }
}
</style>
