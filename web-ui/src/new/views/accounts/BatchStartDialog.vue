<script setup lang="ts">
/* --- 批量任务启动弹窗：并发数 + 改密参数 + 试用强制重检 --- */
import { computed, ref, watch } from "vue";
import { Play } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { fetchUiSettings } from "../../api/settings";

export type BatchKind = "reauth" | "security_repair" | "password_change" | "totp_change" | "refreshAt" | "trial";

const props = defineProps<{
  open: boolean;
  kind: BatchKind;
  keys: string[];
  /** true = 服务端全量范围（仅 refreshAt/trial 支持，keys 传空数组） */
  all?: boolean;
}>();

const emit = defineEmits<{ close: []; started: [kind: BatchKind] }>();

const batch = useBatchOpsStore();
const ui = useUiStore();

const concurrency = ref(3);
const passwordMode = ref<"random" | "custom">("random");
const newPassword = ref("");
const confirmPassword = ref("");
const force = ref(false);
const batchSize = ref(2000);
const submitting = ref(false);
const settingsLoading = ref(false);
let settingsGeneration = 0;
const maxConcurrency = computed(() => props.kind === "security_repair" ? 100 : undefined);
const usesBatchSize = computed(() => props.kind === "refreshAt" || props.kind === "trial");

const config = computed(() => {
  const n = props.all ? "全部" : props.keys.length;
  switch (props.kind) {
    case "reauth":
      return {
        title: "重新登录",
        message: `将 ${n} 个账号使用已保存的密码和 TOTP 2FA 重新登录（协议登录，不读取邮箱验证码）。`,
        defaultConcurrency: 3,
      };
    case "security_repair":
      return {
        title: "补全安全字段",
        message: `将 ${n} 个账号补全缺失的密码和 TOTP 2FA，已存在的字段会跳过。`,
        defaultConcurrency: 3,
      };
    case "password_change":
      return {
        title: "修改密码",
        message: `将 ${n} 个账号修改 ChatGPT 登录密码。随机模式为每个账号生成独立 16 位密码。`,
        defaultConcurrency: 3,
      };
    case "totp_change":
      return {
        title: "修改 2FA",
        message: `将 ${n} 个账号更换 TOTP 2FA 因子，旧因子将失效。`,
        defaultConcurrency: 3,
      };
    case "refreshAt":
      return {
        title: "刷新 AT",
        message: props.all
          ? "将全部待处理账号（测活失败或 AT 过期/缺失，未封号）通过 Session 刷新 Access Token，无 Session 的自动跳过。"
          : `将 ${n} 个选中账号通过 Session 刷新 Access Token（无 Session 的自动跳过）。`,
        defaultConcurrency: 10,
      };
    case "trial":
      return {
        title: "Plus 试用检测",
        message: props.all
          ? "检测全部账号的 Plus 免费试用资格。"
          : `检测 ${n} 个账号的 Plus 免费试用资格。`,
        defaultConcurrency: 5,
      };
  }
});

watch(() => props.open, (value) => {
  const generation = ++settingsGeneration;
  settingsLoading.value = false;
  if (value) {
    concurrency.value = config.value?.defaultConcurrency || 3;
    passwordMode.value = "random";
    newPassword.value = "";
    confirmPassword.value = "";
    force.value = false;
    batchSize.value = 2000;
    if (usesBatchSize.value) {
      settingsLoading.value = true;
      void fetchUiSettings().then((settings) => {
        if (generation !== settingsGeneration) return;
        const saved = Number(settings.account_task_batch_size);
        if (Number.isSafeInteger(saved) && saved >= 1 && saved <= 100000) batchSize.value = saved;
      }).catch(() => {
        // Keep the local default when settings are temporarily unavailable.
      }).finally(() => {
        if (generation === settingsGeneration) settingsLoading.value = false;
      });
    }
  }
});

const passwordInvalid = computed(() => {
  if (props.kind !== "password_change" || passwordMode.value !== "custom") return "";
  if (newPassword.value.length < 12) return "自定义密码至少 12 位";
  if (newPassword.value !== confirmPassword.value) return "两次输入的密码不一致";
  return "";
});

const batchSizeInvalid = computed(() =>
  usesBatchSize.value && (!Number.isSafeInteger(batchSize.value) || batchSize.value < 1 || batchSize.value > 100000),
);

async function submit() {
  if (submitting.value || settingsLoading.value || passwordInvalid.value || batchSizeInvalid.value) return;
  submitting.value = true;
  const requested = Math.max(1, Math.floor(Number(concurrency.value) || 1));
  const workers = maxConcurrency.value ? Math.min(maxConcurrency.value, requested) : requested;
  try {
    if (props.kind === "refreshAt") {
      await batch.startRefreshAt(props.keys, Boolean(props.all), workers, batchSize.value);
    } else if (props.kind === "trial") {
      await batch.startTrial(props.keys, Boolean(props.all), force.value, workers, batchSize.value);
    } else {
      await batch.startReauthBatch(props.kind, props.keys, {
        concurrency: workers,
        passwordMode: passwordMode.value,
        newPassword: newPassword.value,
        confirmPassword: confirmPassword.value,
      });
    }
    emit("started", props.kind);
    emit("close");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "批次启动失败", "error");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <AppModal :open="open" :title="config?.title || ''" :subtitle="all ? '全部账号（服务端筛选范围）' : `${keys.length} 个选中账号`" width="480px" @close="emit('close')">
    <template #icon><Play :size="18" /></template>

    <p class="start-message">{{ config?.message }}</p>

    <div class="field-row">
      <span class="field-label">并发数<small v-if="maxConcurrency">上限 {{ maxConcurrency }}</small></span>
      <input v-model.number="concurrency" class="field-input" type="number" min="1" :max="maxConcurrency" step="1" />
    </div>

    <div v-if="usesBatchSize" class="field-row batch-size-row">
      <span class="field-label">账号任务单批数量</span>
      <input v-model.number="batchSize" class="field-input" type="number" min="1" max="100000" step="1" :disabled="settingsLoading || submitting" />
      <small>超过后由服务端自动分批，任务会继续处理全部账号。</small>
    </div>
    <p v-if="batchSizeInvalid" class="warn-text">账号任务单批数量必须是 1 到 100000 的整数</p>

    <template v-if="kind === 'password_change'">
      <div class="field-row">
        <span class="field-label">密码模式</span>
        <div class="segmented">
          <button type="button" :class="{ active: passwordMode === 'random' }" @click="passwordMode = 'random'">随机生成</button>
          <button type="button" :class="{ active: passwordMode === 'custom' }" @click="passwordMode = 'custom'">自定义</button>
        </div>
      </div>
      <template v-if="passwordMode === 'custom'">
        <div class="field-row">
          <span class="field-label">新密码</span>
          <input v-model="newPassword" class="field-input grow" type="password" placeholder="至少 12 位" autocomplete="new-password" />
        </div>
        <div class="field-row">
          <span class="field-label">确认密码</span>
          <input v-model="confirmPassword" class="field-input grow" type="password" placeholder="再输一次" autocomplete="new-password" />
        </div>
        <p v-if="passwordInvalid" class="warn-text">{{ passwordInvalid }}</p>
      </template>
    </template>

    <label v-if="kind === 'trial'" class="check-row">
      <input v-model="force" type="checkbox" />
      <span>强制重检（忽略已有检测结果全部重跑）</span>
    </label>

    <template #footer>
      <AppButton variant="ghost" @click="emit('close')">取消</AppButton>
      <AppButton variant="primary" :disabled="Boolean(passwordInvalid) || batchSizeInvalid || settingsLoading" :loading="submitting || settingsLoading" @click="submit">
        启动任务
      </AppButton>
    </template>
  </AppModal>
</template>

<style scoped>
.start-message {
  font-size: 0.83rem;
  color: var(--text-secondary);
  line-height: 1.7;
  margin-bottom: 14px;
}

.field-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.field-label {
  font-size: 0.78rem;
  color: var(--text-muted);
  min-width: 60px;
}

.batch-size-row {
  align-items: flex-start;
  flex-wrap: wrap;
}

.batch-size-row .field-label {
  min-width: 112px;
}

.batch-size-row small {
  flex-basis: 100%;
  color: var(--text-muted);
  font-size: 0.72rem;
}

.field-input {
  width: 88px;
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--field-bg);
  color: var(--text-primary);
  font-size: 0.83rem;
  text-align: center;
  outline: none;
  transition: var(--transition-fast);
}

.field-input.grow {
  flex: 1;
  text-align: left;
}

.field-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-bg);
}

.segmented {
  display: inline-flex;
  padding: 3px;
  border-radius: 10px;
  background: var(--inset-bg);
  border: 1px solid var(--line-subtle);
  gap: 2px;
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

.segmented button.active {
  background: var(--accent);
  color: #fff;
}

.check-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.check-row input {
  accent-color: var(--accent);
}

.warn-text {
  color: var(--warning);
  font-size: 0.75rem;
  margin: -4px 0 10px;
}
</style>
