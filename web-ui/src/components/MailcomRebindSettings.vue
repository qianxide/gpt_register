<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api, jsonBody } from "../api.js";
import { MAILCOM_ALIAS_DOMAINS } from "../constants/mailcomAliasDomains.js";

interface MailcomSettings {
  ok: boolean;
  configured: boolean;
  api_base: string;
  admin_key_configured: boolean;
  accounts: MailcomAccount[];
  account_email: string;
  alias_domain: string;
  domains: string[];
  alias_domains?: string[];
  alias_name_mode?: string;
  alias_name_length?: number;
  import_concurrency?: number;
  settings_concurrency?: number;
  proxy_pool_enabled?: boolean;
  proxy_pool_count?: number;
  proxy_pool_assigned?: number;
  proxy_pool_generation?: number;
  proxy_pool_items?: string[];
  alias_limit?: number;
  login_ok?: boolean;
  account_saved?: boolean;
  account_error?: string;
  verification?: "not_checked" | "passed" | "failed" | string;
  verification_message?: string;
  usage_checked?: boolean;
  error?: string;
  alias_deletion?: {
    requested: number;
    deleted: string[];
    failed: Array<{ address: string; error: string }>;
    remaining: number;
    removed_local: number;
  };
  alias_batch_deletion?: {
    requested_accounts: number;
    processed_accounts: number;
    deleted_accounts: number;
    failed_accounts: string[];
    requested_aliases: number;
    deleted_aliases: number;
    concurrency?: number;
    results: Array<{
      email: string;
      requested: number;
      deleted: string[];
      failed: Array<{ address: string; error: string }>;
      remaining: number | null;
      removed_local: number;
    }>;
  };
  account_batch_removal?: {
    requested_accounts: number;
    processed_accounts: number;
    removed_accounts: string[];
    failed_accounts: string[];
    removed_local_aliases: number;
    remote_aliases_retained: true;
    results: Array<{ email: string; ok: boolean; error?: string; removed_local_aliases: number }>;
    concurrency: number;
  };
  bulk?: MailcomBulkReport;
}

interface MailcomAccount {
  email: string;
  alias_count?: number | null;
  alias_limit?: number | null;
  bound_alias_count?: number | null;
  unbound_alias_count?: number | null;
  available_alias_count?: number | null;
  quarantined_alias_count?: number | null;
  proxy?: string;
  proxy_assigned?: boolean;
  proxy_active?: boolean;
}

interface MailcomAlias {
  address: string;
  status?: string;
  bound?: boolean;
  created_at?: string;
  deletable?: boolean;
}

interface MailcomBulkResult {
  email: string;
  ok: boolean;
  checked?: boolean;
  domains?: string[];
  error?: string;
}

interface MailcomBulkReport {
  requested: number;
  saved: number;
  verified: number;
  failed: number;
  unchecked?: number;
  invalid: number;
  results: MailcomBulkResult[];
  invalid_lines: Array<{ line: number; error: string }>;
  concurrency?: number;
}

const props = defineProps<{ disabled?: boolean }>();
const emit = defineEmits<{
  selection: [value: { account_email: string; domain: string }];
  invalidate: [];
  "ready-change": [value: boolean];
  "busy-change": [value: boolean];
}>();

const loaded = ref(false);
const configured = ref(false);
const adminKeyConfigured = ref(false);
const accounts = ref<MailcomAccount[]>([]);
const email = ref("");
const password = ref("");
const apiBase = ref("");
const adminKey = ref("");
const bulkText = ref("");
const bulkReport = ref<MailcomBulkReport | null>(null);
const selectedDomains = ref<string[]>([]);
const useAllAccounts = ref(true);
const domain = ref("random");
const aliasNameMode = ref<"random" | "name_digits">("random");
const aliasNameLength = ref(17);
const importConcurrency = ref(3);
const settingsConcurrency = ref(3);
const proxyPoolEnabled = ref(false);
const proxyPoolCount = ref(0);
const proxyPoolAssigned = ref(0);
const proxyPoolItems = ref<string[]>([]);
const proxyPoolText = ref("");
const proxyPoolReassign = ref(false);
const proxyPoolBusy = ref(false);
const proxyPoolError = ref("");
const domains = ref<string[]>([]);
const domainsCheckedFor = ref("");
const domainSearch = ref("");
const settingsBusy = ref(false);
const domainsBusy = ref(false);
const aliasesBusy = ref(false);
const deletingAccount = ref("");
const deletingAliases = ref("");
const aliases = ref<MailcomAlias[]>([]);
const aliasDeleteReport = ref<NonNullable<MailcomSettings["alias_deletion"]> | null>(null);
const aliasBatchDeleteReport = ref<NonNullable<MailcomSettings["alias_batch_deletion"]> | null>(null);
const accountBatchRemovalReport = ref<NonNullable<MailcomSettings["account_batch_removal"]> | null>(null);
const selectedAccounts = ref<string[]>([]);
const accountSearch = ref("");
const batchConcurrency = ref(3);
const batchError = ref("");
const dirty = ref(false);
const settingsError = ref("");
const domainsError = ref("");
const aliasesError = ref("");
const message = ref("");
const controller = new AbortController();
let disposed = false;
let domainsGeneration = 0;
let aliasesGeneration = 0;

const accountEmail = computed(() => email.value.trim().toLowerCase());
const knownAccount = computed(() => accounts.value.some((item) => item.email === accountEmail.value));
const busy = computed(() => settingsBusy.value || domainsBusy.value || aliasesBusy.value || proxyPoolBusy.value
  || Boolean(deletingAccount.value) || Boolean(deletingAliases.value));
const locked = computed(() => props.disabled || busy.value);
const aliasNameMinLength = computed(() => aliasNameMode.value === "name_digits" ? 13 : 8);
const aliasNameLengthValid = computed(() => Number.isInteger(Number(aliasNameLength.value))
  && Number(aliasNameLength.value) >= aliasNameMinLength.value && Number(aliasNameLength.value) <= 32);
const settingsConcurrencyValid = computed(() => Number.isInteger(Number(settingsConcurrency.value))
  && Number(settingsConcurrency.value) >= 1 && Number(settingsConcurrency.value) <= 50);
const aliasNamePreview = computed(() => {
  if (!aliasNameLengthValid.value) return "";
  const length = Number(aliasNameLength.value);
  // Keep both names complete even in the shortest supported examples.
  const fullName = length >= 16 ? "elenaparker" : "lilyward";
  const localPart = aliasNameMode.value === "name_digits"
    ? fullName + "4827169305".repeat(3).slice(0, length - fullName.length)
    : "k7m4p9x2r6q8v3n5".repeat(2).slice(0, length);
  const exampleDomain = selectedDomains.value[0] || (domain.value === "random" ? "dr.com" : domain.value);
  return `${localPart}@${exampleDomain}`;
});
const ready = computed(() => loaded.value && configured.value && accounts.value.length > 0
  && !dirty.value && !busy.value && !settingsError.value
  // Domain discovery is deliberately optional.  The allocator validates the
  // selected domain at alias-creation time and skips only that failed account.
  );
const canSave = computed(() => loaded.value && !locked.value
  && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(accountEmail.value)
  && aliasNameLengthValid.value
  && settingsConcurrencyValid.value
  && (knownAccount.value || password.value.length > 0)
  && (configured.value || (/^https?:\/\/\S+$/i.test(apiBase.value.trim())
    && (adminKeyConfigured.value || adminKey.value.trim().length > 0))));
const canLoadDomains = computed(() => loaded.value && configured.value && knownAccount.value && !dirty.value && !settingsError.value);
const canSelectDomains = computed(() => loaded.value && !busy.value && !settingsError.value);
const canBulkImport = computed(() => loaded.value && !locked.value && bulkText.value.trim().length > 0
  && aliasNameLengthValid.value
  && (configured.value || (/^https?:\/\/\S+$/i.test(apiBase.value.trim())
    && (adminKeyConfigured.value || adminKey.value.trim().length > 0))));
const visibleAccounts = computed(() => {
  const query = accountSearch.value.trim().toLowerCase();
  return accounts.value.filter((account) => account.email.includes(query));
});
const visibleSelectedCount = computed(() => visibleAccounts.value
  .filter((account) => selectedAccounts.value.includes(account.email)).length);
const allVisibleAccountsSelected = computed(() => visibleAccounts.value.length > 0
  && visibleSelectedCount.value === visibleAccounts.value.length);
const hiddenSelectedCount = computed(() => selectedAccounts.value.length - visibleSelectedCount.value);
const batchConcurrencyValid = computed(() => Number.isInteger(Number(batchConcurrency.value))
  && Number(batchConcurrency.value) >= 1 && Number(batchConcurrency.value) <= 100);
const proxyPoolInputCount = computed(() => new Set(proxyPoolText.value.split(/\r?\n/)
  .map((item) => item.trim()).filter((item) => item && !item.startsWith("#"))).size);
const canSaveProxyPool = computed(() => loaded.value && !locked.value
  && (proxyPoolCount.value > 0 || proxyPoolInputCount.value > 0 || !proxyPoolEnabled.value));

function domainList(value: unknown): string[] {
  return Array.isArray(value)
    ? [...new Set(value.filter((item): item is string => typeof item === "string").map((item) => item.trim().toLowerCase()).filter((item) => item && item !== "random"))]
    : [];
}

function settingsDomainList(value: MailcomSettings): string[] {
  const configuredDomains = domainList(value.alias_domains);
  if (configuredDomains.length) return configuredDomains;
  const legacy = String(value.alias_domain || "").trim().toLowerCase();
  return legacy && legacy !== "random" ? [legacy] : [];
}

const domainOptions = computed(() => {
  const values = [...MAILCOM_ALIAS_DOMAINS, ...selectedDomains.value];
  return [...new Set(values.map((item) => String(item || "").trim().toLowerCase()).filter(Boolean))];
});
const visibleDomainOptions = computed(() => {
  const query = domainSearch.value.trim().toLowerCase();
  return query ? domainOptions.value.filter((item) => item.includes(query)) : domainOptions.value;
});
const liveDomainSet = computed(() => new Set(domains.value));
const domainDiagnosticComplete = computed(() => domainsCheckedFor.value === accountEmail.value && !domainsError.value);

function syncSelectedDomains() {
  selectedDomains.value = [...new Set(selectedDomains.value.map((item) => item.trim().toLowerCase()).filter(Boolean))];
  domain.value = selectedDomains.value[0] || "random";
  invalidateSettings();
}

function selectRandomDomain() {
  selectedDomains.value = [];
  domain.value = "random";
  invalidateSettings();
}

function toggleAccountMode() {
  message.value = "";
  emit("invalidate");
}

function applyMetadata(value: MailcomSettings, preserveForm = false) {
  configured.value = value.configured === true;
  adminKeyConfigured.value = value.admin_key_configured === true;
  proxyPoolEnabled.value = value.proxy_pool_enabled === true;
  proxyPoolCount.value = Math.max(0, Number(value.proxy_pool_count || 0));
  proxyPoolAssigned.value = Math.max(0, Number(value.proxy_pool_assigned || 0));
  proxyPoolItems.value = Array.isArray(value.proxy_pool_items)
    ? value.proxy_pool_items.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  // Deletion responses refresh server state without replacing the editable draft.
  if (!preserveForm) {
    aliasNameMode.value = value.alias_name_mode === "name_digits" ? "name_digits" : "random";
    const savedAliasLength = Number(value.alias_name_length ?? 17);
    aliasNameLength.value = Number.isInteger(savedAliasLength) && savedAliasLength >= 8 && savedAliasLength <= 32
      ? Math.max(aliasNameMinLength.value, savedAliasLength)
      : 17;
    const savedImportConcurrency = Number(value.import_concurrency ?? 3);
    importConcurrency.value = Number.isInteger(savedImportConcurrency) && savedImportConcurrency >= 1 && savedImportConcurrency <= 10
      ? savedImportConcurrency
      : 3;
    const savedSettingsConcurrency = Number(value.settings_concurrency ?? 3);
    settingsConcurrency.value = Number.isInteger(savedSettingsConcurrency) && savedSettingsConcurrency >= 1 && savedSettingsConcurrency <= 50
      ? savedSettingsConcurrency
      : 3;
  }
  const seen = new Set<string>();
  accounts.value = (Array.isArray(value.accounts) ? value.accounts : [])
    .map((item) => {
      const raw = item as MailcomAccount | string;
      const accountEmail = typeof raw === "string" ? raw : raw?.email;
      return {
        ...(typeof raw === "object" && raw ? raw : {}),
        email: String(accountEmail || "").trim().toLowerCase(),
      };
    })
    .filter((item) => item.email && !seen.has(item.email) && seen.add(item.email))
    .map((item) => ({
      email: item.email,
      alias_count: typeof item.alias_count === "number" ? item.alias_count : null,
      alias_limit: typeof item.alias_limit === "number" ? item.alias_limit : (value.alias_limit || 9),
      bound_alias_count: typeof item.bound_alias_count === "number" ? item.bound_alias_count : null,
      unbound_alias_count: typeof item.unbound_alias_count === "number" ? item.unbound_alias_count : null,
      available_alias_count: typeof item.available_alias_count === "number" ? item.available_alias_count : null,
      quarantined_alias_count: typeof item.quarantined_alias_count === "number" ? item.quarantined_alias_count : null,
      proxy: String(item.proxy || ""),
      proxy_assigned: item.proxy_assigned === true,
      proxy_active: item.proxy_active === true,
    }));
  const availableAccounts = new Set(accounts.value.map((item) => item.email));
  selectedAccounts.value = selectedAccounts.value.filter((item) => availableAccounts.has(item));
}

function resultError(value: MailcomSettings): string {
  return value.error || (value.ok === false ? "读取或保存邮箱设置失败，请重试。" : "");
}

function invalidateSettings() {
  dirty.value = true;
  message.value = "";
  emit("invalidate");
}

function changeAliasNameMode() {
  if (aliasNameMode.value === "name_digits" && Number(aliasNameLength.value) < 13) {
    aliasNameLength.value = 13;
  }
  invalidateSettings();
}

function editEmail() {
  domainsGeneration += 1;
  domains.value = [];
  domainsCheckedFor.value = "";
  aliasesGeneration += 1;
  aliases.value = [];
  aliasesError.value = "";
  aliasDeleteReport.value = null;
  resetBatchFeedback();
  domain.value = "random";
  invalidateSettings();
}

async function loadDomains() {
  const selectedEmail = accountEmail.value;
  const generation = ++domainsGeneration;
  domainsBusy.value = true;
  domainsError.value = "";
  domainsCheckedFor.value = "";
  try {
    const value = await api(`/api/mailcom/domains?account_email=${encodeURIComponent(selectedEmail)}`, { signal: controller.signal });
    if (disposed || generation !== domainsGeneration || selectedEmail !== accountEmail.value) return;
    if (value?.ok === false) throw new Error(value.error || "获取可用域名失败，请重试。");
    domains.value = domainList(value?.domains);
    if (!domains.value.length) throw new Error("暂未获取到可用域名，请刷新后重试。");
    domainsCheckedFor.value = selectedEmail;
  } catch (error) {
    if (disposed || generation !== domainsGeneration || selectedEmail !== accountEmail.value) return;
    domainsError.value = error instanceof Error ? error.message : "获取可用域名失败，请重试。";
  } finally {
    if (!disposed && generation === domainsGeneration) domainsBusy.value = false;
  }
}

async function loadAliases() {
  const selectedEmail = accountEmail.value;
  const generation = ++aliasesGeneration;
  if (!selectedEmail || !configured.value || !knownAccount.value || dirty.value || settingsError.value) {
    aliases.value = [];
    aliasesError.value = "";
    aliasesBusy.value = false;
    return;
  }
  aliasesBusy.value = true;
  aliasesError.value = "";
  try {
    const value = await api(`/api/mailcom/aliases?email=${encodeURIComponent(selectedEmail)}`, { signal: controller.signal });
    if (disposed || generation !== aliasesGeneration || selectedEmail !== accountEmail.value) return;
    if (value?.ok === false) throw new Error(value.error || "读取别名列表失败，请重试。");
    aliases.value = (Array.isArray(value?.items) ? value.items : [])
      .map((item: MailcomAlias) => ({
        address: String(item?.address || "").trim().toLowerCase(),
        status: String(item?.status || "active").trim().toLowerCase(),
        bound: item?.bound === true,
        created_at: String(item?.created_at || ""),
        deletable: item?.deletable !== false,
      }))
      .filter((item: MailcomAlias) => item.address);
  } catch (error) {
    if (disposed || generation !== aliasesGeneration || selectedEmail !== accountEmail.value) return;
    aliasesError.value = error instanceof Error ? error.message : "读取别名列表失败，请重试。";
    aliases.value = [];
  } finally {
    if (!disposed && generation === aliasesGeneration) aliasesBusy.value = false;
  }
}

async function loadSettings() {
  if (busy.value) return;
  settingsBusy.value = true;
  settingsError.value = "";
  try {
    // Loading the panel must stay local: detailed alias usage is an explicit
    // operation because it would otherwise authenticate every saved mailbox.
    const value: MailcomSettings = await api("/api/mailcom/settings?include_usage=0", { signal: controller.signal });
    if (disposed) return;
    applyMetadata(value);
    apiBase.value = String(value.api_base || "");
    email.value = String(value.account_email || accounts.value[0]?.email || "");
    selectedDomains.value = settingsDomainList(value);
    domain.value = selectedDomains.value[0] || String(value.alias_domain || "random").trim().toLowerCase();
    domains.value = domainList(value.domains);
    domainsCheckedFor.value = "";
    loaded.value = true;
    settingsError.value = resultError(value);
    // Do not probe domains or aliases while opening the page.  Mail.com
    // allocation performs the authoritative check only when an alias is
    // actually needed.
  } catch (error) {
    if (!disposed) settingsError.value = error instanceof Error ? error.message : "读取邮箱设置失败，请重试。";
  } finally {
    if (!disposed) settingsBusy.value = false;
  }
}

async function selectAccount(event: Event) {
  if (locked.value) return;
  useAllAccounts.value = false;
  email.value = (event.target as HTMLSelectElement).value;
  password.value = "";
  domain.value = selectedDomains.value[0] || "random";
  domains.value = [];
  domainsCheckedFor.value = "";
  dirty.value = false;
  settingsError.value = "";
  message.value = "";
  bulkReport.value = null;
  aliases.value = [];
  aliasesError.value = "";
  aliasDeleteReport.value = null;
  resetBatchFeedback();
  emit("invalidate");
  message.value = "已切换主邮箱；需要诊断时再点击“检查当前邮箱域名”。";
}

function beginAddAccount() {
  if (locked.value) return;
  domainsGeneration += 1;
  email.value = "";
  password.value = "";
  domain.value = selectedDomains.value[0] || "random";
  domains.value = [];
  domainsCheckedFor.value = "";
  aliases.value = [];
  aliasesError.value = "";
  aliasDeleteReport.value = null;
  resetBatchFeedback();
  selectedAccounts.value = [];
  settingsError.value = "";
  domainsError.value = "";
  message.value = "";
  bulkReport.value = null;
  invalidateSettings();
}

function resetBatchFeedback() {
  aliasBatchDeleteReport.value = null;
  accountBatchRemovalReport.value = null;
  batchError.value = "";
}

function targetSummary(targets: string[]): string {
  const visible = new Set(visibleAccounts.value.map((account) => account.email));
  const hidden = targets.filter((item) => !visible.has(item)).length;
  return `${hidden ? `其中 ${hidden} 个主邮箱不在当前搜索结果中。\n` : ""}${targets.slice(0, 5).join("\n")}${targets.length > 5 ? `\n…共 ${targets.length} 个主邮箱` : ""}`;
}

function retainFailedSelection(targets: string[], failures: string[]) {
  const failed = new Set(failures.map((item) => item.trim().toLowerCase()));
  const available = new Set(accounts.value.map((item) => item.email));
  selectedAccounts.value = targets.filter((item) => failed.has(item) && available.has(item));
}

async function removeAccounts(targetEmails: string[], isBatch = true) {
  const targets = [...targetEmails];
  if (locked.value || !targets.length || !batchConcurrencyValid.value) return;
  const confirmed = window.confirm(
    `确认从当前服务器移除 ${targets.length} 个 mail.com 主邮箱？\n仅清理本机保存的登录凭证和本地别名记录，mail.com 远端主邮箱及子邮箱均保留。移除后本机将停止使用这些账号，重新使用需再次导入。\n\n${targetSummary(targets)}\n\n并发：${batchConcurrency.value}。未保存的表单内容会保留。`,
  );
  if (!confirmed) return;
  emit("invalidate");
  deletingAccount.value = isBatch ? "batch" : targets[0];
  resetBatchFeedback();
  message.value = "";
  try {
    const value: MailcomSettings = await api("/api/mailcom/accounts/batch", {
      method: "DELETE",
      body: jsonBody({ emails: targets, concurrency: Number(batchConcurrency.value) }),
      signal: controller.signal,
    });
    if (disposed) return;
    const report = value.account_batch_removal;
    if (!report) throw new Error(value.error || "未收到主邮箱移除结果，已保留选择，请刷新确认账号状态后重试。");
    const currentRemoved = report.removed_accounts.some((item) => item.trim().toLowerCase() === accountEmail.value);
    applyMetadata(value, true);
    accountBatchRemovalReport.value = report;
    aliasDeleteReport.value = null;
    if (isBatch) retainFailedSelection(targets, report.failed_accounts);
    if (currentRemoved) {
      domainsGeneration += 1;
      aliasesGeneration += 1;
      domains.value = [];
      domainsCheckedFor.value = "";
      aliases.value = [];
      domainsError.value = "";
      aliasesError.value = "";
      if (dirty.value) {
        message.value = "当前编辑的主邮箱已从本机移除，未保存内容仍保留；再次保存该邮箱会重新添加账号。";
      } else {
        const preferred = String(value.account_email || "").trim().toLowerCase();
        email.value = accounts.value.find((item) => item.email === preferred)?.email || accounts.value[0]?.email || "";
        domain.value = selectedDomains.value[0] || "random";
        message.value = "已切换到下一个已保存主邮箱；域名目录可直接选择，需要时再手动检查当前邮箱。";
      }
    }
    emit("invalidate");
  } catch (error) {
    if (!disposed) batchError.value = error instanceof Error ? error.message : "移除主邮箱失败，已保留选择，请重试。";
  } finally {
    if (!disposed) deletingAccount.value = "";
  }
}

function toggleAccountSelection(accountEmail: string) {
  if (locked.value) return;
  const selected = new Set(selectedAccounts.value);
  if (selected.has(accountEmail)) selected.delete(accountEmail);
  else selected.add(accountEmail);
  selectedAccounts.value = accounts.value.map((item) => item.email).filter((item) => selected.has(item));
  resetBatchFeedback();
}

function toggleVisibleAccountSelection() {
  if (locked.value) return;
  const selected = new Set(selectedAccounts.value);
  const deselect = allVisibleAccountsSelected.value;
  for (const account of visibleAccounts.value) {
    if (deselect) selected.delete(account.email);
    else selected.add(account.email);
  }
  selectedAccounts.value = accounts.value.map((item) => item.email).filter((item) => selected.has(item));
  resetBatchFeedback();
}

function clearAccountSelection() {
  if (locked.value) return;
  selectedAccounts.value = [];
  resetBatchFeedback();
}

async function deleteSelectedAliases() {
  const targets = [...selectedAccounts.value];
  if (locked.value || !targets.length || !batchConcurrencyValid.value) return;
  const selected = accounts.value.filter((account) => targets.includes(account.email));
  const aliasCount = selected.reduce((total, account) => total + (account.alias_count ?? 0), 0);
  const countSummary = selected.some((account) => account.alias_count == null)
    ? `已知涉及 ${aliasCount} 个子邮箱，部分账号数量待查询。`
    : `当前计数共 ${aliasCount} 个子邮箱，以远端实际结果为准。`;
  const confirmed = window.confirm(
    `确认删除选中 ${targets.length} 个主邮箱下由系统管理的全部远端子邮箱（别名）？\n${countSummary}\n已绑定及未绑定的子邮箱都会删除，成功后同步清理本地别名记录；对应地址将停止接收验证码。主邮箱及其已保存的登录凭证保留。\n\n${targetSummary(targets)}\n\n并发：${batchConcurrency.value}。未保存的表单内容会保留。`,
  );
  if (!confirmed) return;
  emit("invalidate");
  deletingAliases.value = "batch";
  resetBatchFeedback();
  aliasesError.value = "";
  message.value = "";
  try {
    const value: MailcomSettings = await api("/api/mailcom/aliases/batch", {
      method: "DELETE",
      body: jsonBody({ emails: targets, concurrency: Number(batchConcurrency.value) }),
      signal: controller.signal,
    });
    if (disposed) return;
    const report = value.alias_batch_deletion;
    if (!report) throw new Error(value.error || "未收到子邮箱删除结果，已保留选择，请刷新确认远端状态后重试。");
    applyMetadata(value, true);
    aliasBatchDeleteReport.value = report;
    aliasDeleteReport.value = null;
    retainFailedSelection(targets, report.failed_accounts);
    await loadAliases();
    emit("invalidate");
  } catch (error) {
    if (!disposed) batchError.value = error instanceof Error ? error.message : "批量删除子邮箱失败，已保留选择，请重试。";
  } finally {
    if (!disposed) deletingAliases.value = "";
  }
}

async function deleteAllAliases() {
  const selectedEmail = accountEmail.value;
  const count = aliases.value.length;
  if (locked.value || !selectedEmail || !count || deletingAliases.value) return;
  const boundCount = aliases.value.filter((item) => item.bound).length;
  const confirmed = window.confirm(
    `确认从 mail.com 远端删除 ${selectedEmail} 下的 ${count} 个别名？${boundCount ? `其中 ${boundCount} 个已绑定账号，删除后将无法继续收取这些地址的验证码。` : ""}`,
  );
  if (!confirmed) return;
  emit("invalidate");
  deletingAliases.value = selectedEmail;
  resetBatchFeedback();
  aliasesError.value = "";
  message.value = "";
  try {
    const value: MailcomSettings = await api(`/api/mailcom/aliases?email=${encodeURIComponent(selectedEmail)}`, {
      method: "DELETE",
      signal: controller.signal,
    });
    if (disposed) return;
    applyMetadata(value, true);
    aliasDeleteReport.value = value.alias_deletion || null;
    aliases.value = [];
    const report = aliasDeleteReport.value;
    const failed = report?.failed?.length || 0;
    const failureMessage = failed ? report!.failed.map((item) => `${item.address}：${item.error}`).join("；") : "";
    message.value = report
      ? `批量删除完成：远端删除 ${report.deleted.length} 个${failed ? `，失败 ${failed} 个` : ""}。`
      : "批量删除完成。";
    await loadAliases();
    if (failureMessage) aliasesError.value = failureMessage;
    emit("invalidate");
  } catch (error) {
    if (!disposed) aliasesError.value = error instanceof Error ? error.message : "批量删除别名失败，请重试。";
  } finally {
    if (!disposed) deletingAliases.value = "";
  }
}

async function refreshDomains() {
  if (locked.value || !canLoadDomains.value) return;
  message.value = "正在检查当前主邮箱的可用域名…";
  await loadDomains();
  if (!domainsError.value) {
    message.value = domains.value.length
      ? `检查完成：当前账号返回 ${domains.value.length} 个可用域名，仅作诊断标记，不会改动已选域名。`
      : "检查完成，但当前账号没有返回可用域名。";
  }
}

async function save() {
  if (!canSave.value) return;
  emit("invalidate");
  settingsBusy.value = true;
  settingsError.value = "";
  message.value = "";
  bulkReport.value = null;
  aliasDeleteReport.value = null;
  resetBatchFeedback();
  const payload: { email: string; alias_domain: string; alias_domains: string[]; alias_name_mode: string; alias_name_length: number; import_concurrency: number; settings_concurrency: number; verify: boolean; password?: string; api_base?: string; admin_key?: string } = {
    email: accountEmail.value,
    alias_domain: selectedDomains.value.length === 1 ? selectedDomains.value[0] : "random",
    alias_domains: selectedDomains.value,
    alias_name_mode: aliasNameMode.value,
    alias_name_length: Number(aliasNameLength.value),
    import_concurrency: Math.min(10, Math.max(1, Math.floor(Number(importConcurrency.value) || 3))),
    settings_concurrency: Math.min(50, Math.max(1, Math.floor(Number(settingsConcurrency.value) || 3))),
    verify: false,
  };
  if (password.value) payload.password = password.value;
  if (!configured.value) {
    payload.api_base = apiBase.value.trim();
    if (adminKey.value.trim()) payload.admin_key = adminKey.value.trim();
  }
  try {
    const value: MailcomSettings = await api("/api/mailcom/settings", { method: "PUT", body: jsonBody(payload), signal: controller.signal });
    if (disposed) return;
    applyMetadata(value);
    selectedDomains.value = settingsDomainList(value);
    domain.value = selectedDomains.value[0] || String(value.alias_domain || "random").trim().toLowerCase();
    settingsError.value = resultError(value);
    if (settingsError.value) {
      dirty.value = true;
      return;
    }
    dirty.value = false;
    password.value = "";
    adminKey.value = "";
    message.value = value.account_error
      ? `邮箱已保存，但当前账号暂不可用：${value.account_error}；换绑时会自动跳过它。`
      : (value.verification_message || "邮箱已保存，可直接用于换绑；创建别名时才会调用 mail.com。");
  } catch (error) {
    if (!disposed) {
      settingsError.value = error instanceof Error ? error.message : "保存邮箱设置失败，请重试。";
      dirty.value = true;
    }
  } finally {
    if (!disposed) settingsBusy.value = false;
  }
}

async function saveProxyPool(clear = false) {
  if (locked.value || (!clear && !canSaveProxyPool.value)) return;
  if (clear && !window.confirm("确认清空 mail.com 登录代理池？所有邮箱将恢复服务器直连，已保存账号和别名不会删除。")) return;
  if (!clear && proxyPoolReassign.value && !window.confirm("确认重新分配全部邮箱的代理？账号出口 IP 会变化，下一次登录可能触发 mail.com 验证。")) return;
  emit("invalidate");
  proxyPoolBusy.value = true;
  proxyPoolError.value = "";
  message.value = "";
  try {
    const text = proxyPoolText.value.trim();
    const value: MailcomSettings = await api("/api/mailcom/proxy-pool", {
      method: "PUT",
      body: jsonBody({
        enabled: clear ? false : proxyPoolEnabled.value,
        text: clear ? null : (text || null),
        reassign: clear ? false : proxyPoolReassign.value,
        clear,
      }),
      signal: controller.signal,
    });
    if (disposed) return;
    applyMetadata(value, true);
    proxyPoolText.value = "";
    proxyPoolReassign.value = false;
    message.value = clear
      ? "mail.com 登录代理池已清空，账号恢复服务器直连。"
      : `代理池已保存：${proxyPoolCount.value} 条代理，已绑定 ${proxyPoolAssigned.value} 个邮箱。`;
    emit("invalidate");
  } catch (error) {
    if (!disposed) proxyPoolError.value = error instanceof Error ? error.message : "保存 mail.com 代理池失败，请重试。";
  } finally {
    if (!disposed) proxyPoolBusy.value = false;
  }
}

async function bulkImport() {
  if (!canBulkImport.value) return;
  emit("invalidate");
  settingsBusy.value = true;
  settingsError.value = "";
  domainsError.value = "";
  message.value = "";
  const payload: { text: string; alias_domain: string; alias_domains: string[]; alias_name_mode: string; alias_name_length: number; concurrency: number; verify: boolean; api_base?: string; admin_key?: string } = {
    text: bulkText.value,
    alias_domain: selectedDomains.value.length === 1 ? selectedDomains.value[0] : "random",
    alias_domains: selectedDomains.value,
    alias_name_mode: aliasNameMode.value,
    alias_name_length: Number(aliasNameLength.value),
    concurrency: Math.min(10, Math.max(1, Math.floor(Number(importConcurrency.value) || 3))),
    verify: false,
  };
  if (!configured.value) {
    payload.api_base = apiBase.value.trim();
    if (adminKey.value.trim()) payload.admin_key = adminKey.value.trim();
  }
  try {
    const value: MailcomSettings = await api("/api/mailcom/accounts/import-batch", {
      method: "POST",
      body: jsonBody(payload),
      signal: controller.signal,
    });
    if (disposed) return;
    applyMetadata(value);
    apiBase.value = String(value.api_base || apiBase.value || "");
    email.value = String(value.account_email || accounts.value[0]?.email || email.value);
    useAllAccounts.value = true;
    selectedDomains.value = settingsDomainList(value);
    domain.value = selectedDomains.value[0] || String(value.alias_domain || domain.value || "random").trim().toLowerCase();
    domains.value = domainList(value.domains);
    domainsCheckedFor.value = "";
    bulkReport.value = value.bulk || null;
    aliasDeleteReport.value = null;
    resetBatchFeedback();
    settingsError.value = resultError(value);
    if (settingsError.value) {
      dirty.value = true;
      return;
    }
    dirty.value = false;
    bulkText.value = "";
    password.value = "";
    adminKey.value = "";
    const report = bulkReport.value;
    message.value = report
      ? `批量保存完成：保存 ${report.saved} 个${report.unchecked ? `，未检查 ${report.unchecked} 个` : `，检查成功 ${report.verified} 个`}，失败 ${report.failed} 个${report.invalid ? `，格式错误 ${report.invalid} 行` : ""}。失败账号不会阻塞其他账号。`
      : "批量导入完成。";
  } catch (error) {
    if (!disposed) {
      settingsError.value = error instanceof Error ? error.message : "批量导入失败，请重试。";
      dirty.value = true;
    }
  } finally {
    if (!disposed) settingsBusy.value = false;
  }
}

watch([accountEmail, domain, selectedDomains, useAllAccounts], () => emit("selection", {
  account_email: useAllAccounts.value ? "" : accountEmail.value,
  domain: selectedDomains.value[0] || domain.value,
}), { immediate: true, flush: "sync", deep: true });
watch(ready, (value) => emit("ready-change", value), { immediate: true, flush: "sync" });
watch(busy, (value) => emit("busy-change", value), { immediate: true, flush: "sync" });
onMounted(loadSettings);
onBeforeUnmount(() => {
  disposed = true;
  controller.abort();
  emit("ready-change", false);
  emit("busy-change", false);
});
</script>

<template>
  <section class="mailcom-settings" aria-label="mail.com 邮箱设置" :aria-busy="busy">
    <p class="mailcom-note">计数来自本机别名记录和 GPT 绑定关系，打开页面不会登录 mail.com；“隔离”是未绑定但因失败或流程中状态暂不可复用的地址。</p>
    <p v-if="!loaded && settingsBusy" class="mailcom-note" role="status">正在读取邮箱设置…</p>
    <template v-if="loaded">
      <div v-if="accounts.length" class="mailcom-account-toolbar">
        <label class="mailcom-field">
          <span>已保存 mail.com 账号（{{ accounts.length }}，自动模式按顺序轮换）</span>
          <select :value="knownAccount ? accountEmail : ''" :disabled="locked" @change="selectAccount">
            <option value="" disabled>选择已保存账号</option>
            <option v-for="account in accounts" :key="account.email" :value="account.email">{{ account.email }}</option>
          </select>
        </label>
        <button type="button" :disabled="locked" @click="beginAddAccount">新增账号</button>
      </div>
      <label v-if="accounts.length" class="mailcom-auto-rotate">
        <input v-model="useAllAccounts" type="checkbox" :disabled="locked" @change="toggleAccountMode" />
        <span>自动轮换全部账号（满额或不可用时继续使用下一个）</span>
      </label>
      <label v-if="accounts.length" class="mailcom-concurrency-field">
        <span>邮箱查询并发</span>
        <input v-model.number="settingsConcurrency" type="number" min="1" max="50" step="1" inputmode="numeric" :disabled="locked" aria-label="mail.com 邮箱查询并发" :aria-invalid="!settingsConcurrencyValid" @input="invalidateSettings" />
        <small>读取主邮箱别名统计时同时查询的账号数，范围 1–50，默认 3。保存后生效。</small>
      </label>
      <p v-if="!settingsConcurrencyValid" class="mailcom-error" role="alert">邮箱查询并发请输入 1 到 50 之间的整数。</p>
      <div v-if="accounts.length" class="mailcom-account-manager" aria-label="主邮箱批量管理">
        <div class="mailcom-account-selection-bar">
          <label class="mailcom-account-select-all">
            <input type="checkbox" :checked="allVisibleAccountsSelected" :indeterminate="visibleSelectedCount > 0 && !allVisibleAccountsSelected" :disabled="locked || !visibleAccounts.length" @change="toggleVisibleAccountSelection" />
            <span>{{ accountSearch.trim() ? '全选搜索结果' : '全选主邮箱' }}（{{ visibleAccounts.length }}）</span>
          </label>
          <button type="button" :disabled="locked || !selectedAccounts.length" @click="clearAccountSelection">清空选择</button>
          <span class="mailcom-selection-count" role="status">已选 <strong>{{ selectedAccounts.length }}</strong> / {{ accounts.length }} 个<span v-if="hiddenSelectedCount">（搜索结果外 {{ hiddenSelectedCount }} 个）</span></span>
          <label class="mailcom-account-search">
            <span>搜索</span>
            <input v-model="accountSearch" type="search" placeholder="按主邮箱地址筛选" autocomplete="off" spellcheck="false" :disabled="locked" />
          </label>
          <button v-if="accountSearch" type="button" :disabled="locked" @click="accountSearch = ''">清除搜索</button>
        </div>
        <div class="mailcom-account-batch-actions">
          <label class="mailcom-batch-concurrency">
            <span>批量并发</span>
            <input v-model.number="batchConcurrency" type="number" min="1" max="100" step="1" inputmode="numeric" :disabled="locked" :aria-invalid="!batchConcurrencyValid" />
            <small>1–100，默认 3</small>
          </label>
          <button type="button" class="mailcom-alias-delete-all" :disabled="locked || !selectedAccounts.length || !batchConcurrencyValid" @click="deleteSelectedAliases">
            {{ deletingAliases === 'batch' ? '删除子邮箱中…' : `删除选中子邮箱（${selectedAccounts.length} 个主邮箱）` }}
          </button>
          <button type="button" class="mailcom-account-remove" :disabled="locked || !selectedAccounts.length || !batchConcurrencyValid" @click="removeAccounts(selectedAccounts)">
            {{ deletingAccount === 'batch' ? '移除主邮箱中…' : `移除选中主邮箱（${selectedAccounts.length}）` }}
          </button>
        </div>
        <p v-if="!batchConcurrencyValid" class="mailcom-error" role="alert">批量并发请输入 1 到 100 之间的整数。</p>
        <div class="mailcom-operation-notes">
          <p class="mailcom-note"><strong>删除子邮箱：</strong>删除所选主邮箱下由系统管理的全部远端别名（含已绑定地址），并清理对应本地记录；这些地址将停止接收验证码。</p>
          <p class="mailcom-note"><strong>移除主邮箱：</strong>仅清理当前服务器保存的登录凭证和本地别名记录，保留 mail.com 远端主邮箱及子邮箱。</p>
          <p class="mailcom-note">支持单选或全选主邮箱，批量操作后保留失败项供重试。</p>
        </div>
        <ul v-if="visibleAccounts.length" class="mailcom-account-list" aria-label="主邮箱及子邮箱数量">
          <li v-for="account in visibleAccounts" :key="`summary-${account.email}`" class="mailcom-account-card" :class="{ 'is-selected': selectedAccounts.includes(account.email) }">
            <div class="mailcom-account-card-head">
              <label class="mailcom-account-choice">
                <input type="checkbox" :checked="selectedAccounts.includes(account.email)" :disabled="locked" :aria-label="`选择 ${account.email}`" @change="toggleAccountSelection(account.email)" />
                <strong>{{ account.email }}</strong>
              </label>
              <button type="button" class="mailcom-account-remove" :disabled="locked || !batchConcurrencyValid" :aria-label="`从当前服务器移除 ${account.email}`" title="仅移除本机凭证和记录，保留远端邮箱" @click="removeAccounts([account.email], false)">{{ deletingAccount === account.email ? '移除中…' : '移除' }}</button>
            </div>
            <dl class="mailcom-account-counts">
              <div><dt>已创建</dt><dd>{{ account.alias_count ?? '—' }}<small> / {{ account.alias_limit ?? 9 }}</small></dd></div>
              <div><dt>已绑定</dt><dd>{{ account.bound_alias_count ?? '—' }}</dd></div>
              <div><dt>未绑定</dt><dd>{{ account.unbound_alias_count ?? '—' }}</dd></div>
              <div><dt>可用</dt><dd>{{ account.available_alias_count ?? '—' }}</dd></div>
              <div title="未绑定但当前不可复用"><dt>隔离</dt><dd>{{ account.quarantined_alias_count ?? '—' }}</dd></div>
            </dl>
            <small class="mailcom-account-proxy" :title="account.proxy || '服务器直连'">登录出口：{{ account.proxy_active ? account.proxy : (account.proxy_assigned ? '代理池已停用' : '服务器直连') }}</small>
          </li>
        </ul>
        <p v-else class="mailcom-note mailcom-empty">没有匹配的主邮箱。清除搜索可查看全部账号，已有勾选保持不变。</p>
      </div>
      <p v-else class="mailcom-note mailcom-empty">当前服务器还没有保存 mail.com 主邮箱，请先批量导入或单个保存。</p>
      <div v-if="aliasBatchDeleteReport" class="mailcom-bulk-report" role="status">
        <strong>子邮箱删除结果：处理 {{ aliasBatchDeleteReport.processed_accounts }} / {{ aliasBatchDeleteReport.requested_accounts }} 个主邮箱，删除 {{ aliasBatchDeleteReport.deleted_aliases }} 个子邮箱，失败 {{ aliasBatchDeleteReport.failed_accounts.length }} 个主邮箱<span v-if="aliasBatchDeleteReport.concurrency">，实际并发 {{ aliasBatchDeleteReport.concurrency }}</span>。</strong>
        <small v-if="aliasBatchDeleteReport.failed_accounts.length">失败主邮箱已保留勾选；再次点击“删除选中子邮箱”重试。</small>
        <details :open="aliasBatchDeleteReport.failed_accounts.length > 0" class="mailcom-batch-details">
          <summary>逐账号结果（{{ aliasBatchDeleteReport.results.length }}）</summary>
          <ul>
            <li v-for="item in aliasBatchDeleteReport.results" :key="`alias-delete-${item.email}`" :class="{ 'mailcom-result-error': item.failed.length > 0 }">
              {{ item.email }}：删除 {{ item.deleted.length }} 个<span v-if="item.failed.length">，失败 {{ item.failed.length }} 个</span><span v-if="item.remaining !== null">，剩余 {{ item.remaining }} 个</span>
              <span v-for="(failure, index) in item.failed" :key="index" class="mailcom-failure-reason">{{ failure.address ? `${failure.address}：` : '' }}{{ failure.error }}</span>
            </li>
          </ul>
        </details>
      </div>
      <div v-if="accountBatchRemovalReport" class="mailcom-bulk-report" role="status">
        <strong>主邮箱移除结果：处理 {{ accountBatchRemovalReport.processed_accounts }} / {{ accountBatchRemovalReport.requested_accounts }} 个，移除 {{ accountBatchRemovalReport.removed_accounts.length }} 个，失败 {{ accountBatchRemovalReport.failed_accounts.length }} 个，清理本地别名记录 {{ accountBatchRemovalReport.removed_local_aliases }} 条，实际并发 {{ accountBatchRemovalReport.concurrency }}。</strong>
        <small>mail.com 远端主邮箱及子邮箱均保留。</small>
        <small v-if="accountBatchRemovalReport.failed_accounts.length">失败原因如下；批量操作的失败主邮箱保留勾选，可再次点击“移除选中主邮箱”重试。</small>
        <details :open="accountBatchRemovalReport.failed_accounts.length > 0" class="mailcom-batch-details">
          <summary>逐账号结果（{{ accountBatchRemovalReport.results.length }}）</summary>
          <ul>
            <li v-for="item in accountBatchRemovalReport.results" :key="`account-remove-${item.email}`" :class="{ 'mailcom-result-error': !item.ok }">
              {{ item.email }}：{{ item.ok ? `已移除，清理 ${item.removed_local_aliases} 条本地别名记录` : (item.error || '移除失败，请重试') }}
            </li>
          </ul>
        </details>
      </div>
      <p v-if="batchError" class="mailcom-error" role="alert">{{ batchError }}</p>
      <details class="mailcom-bulk" open>
        <summary>mail.com 登录代理池</summary>
        <label class="mailcom-auto-rotate">
          <input v-model="proxyPoolEnabled" type="checkbox" :disabled="locked" />
          <span>启用代理池（每个主邮箱固定绑定一个代理，登录、创建别名和收验证码始终使用同一出口）</span>
        </label>
        <label class="mailcom-field">
          <span>代理列表（每行一个；留空保存时保留现有代理）</span>
          <textarea v-model="proxyPoolText" rows="5" spellcheck="false" autocomplete="off" placeholder="http://用户名:密码@主机:端口&#10;主机:端口:用户名:密码" :disabled="locked" />
          <small>支持 HTTP/HTTPS 代理。新账号自动稳定分配；现有账号仅在原代理被移除时自动换绑。</small>
        </label>
        <div class="mailcom-bulk-actions">
          <button type="button" :disabled="!canSaveProxyPool" @click="saveProxyPool(false)">{{ proxyPoolBusy ? '保存中…' : '保存代理池' }}</button>
          <button v-if="proxyPoolCount" type="button" :disabled="locked" @click="saveProxyPool(true)">清空代理池</button>
          <label v-if="proxyPoolCount || proxyPoolInputCount" class="mailcom-auto-rotate">
            <input v-model="proxyPoolReassign" type="checkbox" :disabled="locked" />
            <span>重新分配全部邮箱（会更换已有账号出口）</span>
          </label>
        </div>
        <p class="mailcom-note">当前保存 {{ proxyPoolCount }} 条，已绑定 {{ proxyPoolAssigned }} 个邮箱，状态：{{ proxyPoolEnabled ? '已启用' : '已停用' }}<span v-if="proxyPoolInputCount">；待提交 {{ proxyPoolInputCount }} 条</span>。</p>
        <div v-if="proxyPoolItems.length" class="mailcom-proxy-list" aria-label="已保存代理池">
          <code v-for="item in proxyPoolItems" :key="item">{{ item }}</code>
        </div>
        <p v-if="proxyPoolError" class="mailcom-error" role="alert">{{ proxyPoolError }}</p>
      </details>
      <div v-if="knownAccount && !dirty" class="mailcom-alias-cleanup">
        <div class="mailcom-alias-cleanup-head">
          <div>
            <strong>当前主邮箱的已创建别名（{{ aliases.length }}）</strong>
            <small v-if="aliasesBusy">正在读取远端别名…</small>
            <small v-else-if="!aliases.length">没有可由当前服务管理的别名。</small>
            <small v-else>一次删除全部远端别名，成功后本地取件凭证同步清理。</small>
          </div>
          <button v-if="aliases.length" type="button" class="mailcom-alias-delete-all" :disabled="locked" @click="deleteAllAliases">
            {{ deletingAliases === accountEmail ? '删除子邮箱中…' : `删除当前主邮箱的全部子邮箱（${aliases.length}）` }}
          </button>
        </div>
        <div v-if="aliases.length" class="mailcom-alias-list" aria-label="当前主邮箱别名列表">
          <span v-for="item in aliases" :key="item.address" class="mailcom-alias-chip">
            <strong>{{ item.address }}</strong>
            <small>{{ item.bound ? '已绑定' : '未绑定' }} · {{ item.status === 'active' ? '远端正常' : item.status }}</small>
          </span>
        </div>
        <small class="mailcom-note">删除已绑定别名后，对应 GPT 账号将无法继续通过该地址收取验证码；删除失败的地址会保留并列出原因。</small>
      </div>
      <details class="mailcom-bulk" open>
        <summary>批量保存 mail.com 主邮箱</summary>
        <label class="mailcom-field">
          <span>账号---密码（每行一个，支持 `----` 分隔）</span>
          <textarea v-model="bulkText" rows="6" spellcheck="false" autocomplete="off" placeholder="user1@mail.com----密码1&#10;user2@mail.com---密码2" :disabled="locked" @input="invalidateSettings" />
        </label>
        <div class="mailcom-bulk-actions">
          <label class="mailcom-concurrency-field">
            <span>登录并发</span>
            <input v-model.number="importConcurrency" type="number" min="1" max="10" step="1" inputmode="numeric" :disabled="locked" aria-label="mail.com 登录并发" @input="invalidateSettings" />
          </label>
          <button type="button" :disabled="!canBulkImport" @click="bulkImport">{{ settingsBusy ? '保存中…' : '批量保存账号' }}</button>
          <small class="mailcom-note">并发只控制凭证写入，默认 3，范围 1–10。保存不会逐个登录；换绑创建别名时按需登录，坏账号会跳过并继续。</small>
        </div>
        <div v-if="bulkReport" class="mailcom-bulk-report" role="status">
          <strong>最近批量结果：保存 {{ bulkReport.saved }} 个，登录成功 {{ bulkReport.verified }} 个，失败 {{ bulkReport.failed }} 个，格式错误 {{ bulkReport.invalid }} 行<span v-if="bulkReport.concurrency">，实际并发 {{ bulkReport.concurrency }}</span></strong>
          <ul v-if="bulkReport.results.some((item) => !item.ok)">
            <li v-for="item in bulkReport.results.filter((row) => !row.ok)" :key="`bulk-error-${item.email}`">{{ item.email }}：{{ item.error || '登录失败' }}</li>
          </ul>
          <small v-if="bulkReport.invalid_lines.length">格式错误行：{{ bulkReport.invalid_lines.map((item) => item.line).join('、') }}</small>
        </div>
      </details>
      <div class="mailcom-fields">
        <label class="mailcom-field">
          <span>mail.com 账号邮箱</span>
          <input v-model="email" type="email" autocomplete="username" spellcheck="false" placeholder="输入已有账号邮箱" :disabled="locked" @input="editEmail" />
        </label>
        <label class="mailcom-field">
          <span>邮箱密码</span>
          <input v-model="password" type="password" autocomplete="new-password" :placeholder="knownAccount ? '留空继续使用已保存密码' : '输入邮箱密码'" :disabled="locked" @input="invalidateSettings" />
        </label>
      </div>
      <details v-if="!configured" class="mailcom-connection" open>
        <summary>连接设置（首次使用）</summary>
        <div class="mailcom-fields">
          <label class="mailcom-field">
            <span>API URL</span>
            <input v-model="apiBase" type="url" autocomplete="off" placeholder="https://…" :disabled="locked" @input="invalidateSettings" />
          </label>
          <label class="mailcom-field">
            <span>管理密钥</span>
            <input v-model="adminKey" type="password" autocomplete="new-password" :placeholder="adminKeyConfigured ? '留空继续使用已保存密钥' : '输入管理密钥'" :disabled="locked" @input="invalidateSettings" />
          </label>
        </div>
      </details>
      <div class="mailcom-domain-row">
        <label class="mailcom-field">
          <span>别名域名（可多选，批量统一使用）</span>
          <span class="mailcom-domain-filter">
            <input v-model="domainSearch" type="search" autocomplete="off" spellcheck="false" placeholder="筛选域名，如 dr.com" :disabled="locked" />
            <small>maildotcom-sdk 1.0.9 固定目录，共 {{ domainOptions.length }} 个；选择时不会登录主邮箱。</small>
          </span>
          <span class="mailcom-domain-picker">
            <label class="mailcom-domain-option">
              <input type="checkbox" :checked="selectedDomains.length === 0" :disabled="locked || !canSelectDomains || !domainOptions.length" @change="selectRandomDomain" />
              <span>随机（可用域名）</span>
            </label>
            <label v-for="item in visibleDomainOptions" :key="item" class="mailcom-domain-option" :class="{ 'is-live': domainDiagnosticComplete && liveDomainSet.has(item), 'is-not-returned': domainDiagnosticComplete && !liveDomainSet.has(item) }">
              <input v-model="selectedDomains" type="checkbox" :value="item" :disabled="locked || !canSelectDomains" @change="syncSelectedDomains" />
              <span>{{ item }}</span>
              <small v-if="domainDiagnosticComplete">{{ liveDomainSet.has(item) ? '当前邮箱可用' : '本次未返回' }}</small>
            </label>
          </span>
          <small class="mailcom-note">勾选多个域名后，批量导入和后续换绑会从这些域名中自动选择。“检查当前邮箱域名”只标记该邮箱本次返回的域名，不会隐藏目录或改动勾选。</small>
        </label>
          <button type="button" :disabled="locked || !canLoadDomains" @click="refreshDomains">{{ domainsBusy ? '检查中…' : '检查当前邮箱域名（可选）' }}</button>
      </div>
      <div class="mailcom-alias-options">
        <label class="mailcom-field">
          <span>新建别名格式</span>
          <select v-model="aliasNameMode" :disabled="locked" @change="changeAliasNameMode">
            <option value="random">随机英数字（当前规则）</option>
            <option value="name_digits">完整英文名 + 姓氏 + 随机数字</option>
          </select>
        </label>
        <label class="mailcom-field">
          <span>长度（@ 前字符数）</span>
          <input v-model.number="aliasNameLength" type="number" :min="aliasNameMinLength" max="32" step="1" inputmode="numeric" :disabled="locked" :aria-invalid="!aliasNameLengthValid" @input="invalidateSettings" />
        </label>
        <small class="mailcom-note">先复用已有且未绑定的别名；没有可用别名时才按此格式新建。英文名模式保留完整名字和姓氏，后接至少 5 位随机数字，长度为 13–32 位。</small>
        <small v-if="aliasNamePreview" class="mailcom-note">示例：{{ aliasNamePreview }}（@ 前 {{ aliasNameLength }} 位，仅示意）。</small>
        <small v-if="!aliasNameLengthValid" class="mailcom-error" role="alert">{{ aliasNameMode === 'name_digits' ? '完整英文名模式' : '随机英数字模式' }}的长度请输入 {{ aliasNameMinLength }} 到 32 之间的整数（@ 前字符数）。</small>
      </div>
      <div class="mailcom-actions">
          <button type="button" :disabled="!canSave" @click="save">{{ settingsBusy ? '保存中…' : '保存邮箱并直接分配' }}</button>
          <small v-if="dirty && !settingsError" class="mailcom-note">邮箱设置已修改，请先保存；保存不会触发登录检查。</small>
          <small v-else-if="ready" class="mailcom-note">已就绪；换绑创建别名时才调用 mail.com，失败账号会自动跳过。</small>
      </div>
    </template>
    <p v-if="settingsError" class="mailcom-error" role="alert">{{ settingsError }}</p>
    <button v-if="!loaded && !settingsBusy" type="button" :disabled="locked" @click="loadSettings">重试读取设置</button>
    <p v-if="domainsError" class="mailcom-error" role="alert">{{ domainsError }}</p>
    <p v-if="aliasesError" class="mailcom-error" role="alert">{{ aliasesError }}</p>
    <p v-if="message" class="mailcom-note" role="status">{{ message }}</p>
  </section>
</template>

<style scoped>
.mailcom-settings { display: grid; gap: 12px; min-width: 0; padding: 14px; border: 1px solid var(--line-subtle, var(--line, #cbd5e1)); border-radius: 12px; color: var(--text-primary, var(--text, #1e293b)); }
.mailcom-note, .mailcom-error { margin: 0; font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.mailcom-note { color: var(--text-secondary, var(--muted, #64748b)); }
.mailcom-error { color: var(--error, var(--err, #dc2626)); }
.mailcom-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.mailcom-account-toolbar { display: flex; align-items: end; gap: 10px; flex-wrap: wrap; }
.mailcom-account-toolbar .mailcom-field { flex: 1 1 260px; }
.mailcom-settings .mailcom-auto-rotate { display: flex; align-items: center; gap: 7px; width: fit-content; max-width: 100%; font-size: 12px; color: var(--text-secondary, var(--muted, #64748b)); }
.mailcom-auto-rotate span { min-width: 0; }
.mailcom-account-manager { display: grid; gap: 12px; min-width: 0; }
.mailcom-account-selection-bar, .mailcom-account-batch-actions { display: flex; align-items: center; gap: 10px 12px; flex-wrap: wrap; min-width: 0; }
.mailcom-account-selection-bar { padding-top: 4px; }
.mailcom-account-select-all { display: inline-flex; align-items: center; flex: 0 0 auto; gap: 8px; width: auto; font-size: 13px; white-space: nowrap; cursor: pointer; }
.mailcom-selection-count { font-size: 13px; line-height: 1.5; color: var(--text-secondary, var(--muted, #64748b)); }
.mailcom-selection-count strong { color: var(--text-primary, var(--text, #1e293b)); font-variant-numeric: tabular-nums; }
.mailcom-account-search { display: flex; align-items: center; flex: 1 1 220px; gap: 8px; min-width: 0; max-width: 360px; margin-left: auto; font-size: 13px; }
.mailcom-account-search > span { flex: 0 0 auto; white-space: nowrap; }
.mailcom-batch-concurrency { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 8px; font-size: 13px; white-space: nowrap; }
.mailcom-settings .mailcom-batch-concurrency input[type="number"] { width: 68px; padding: 7px 8px; }
.mailcom-batch-concurrency small { color: var(--text-secondary, var(--muted, #64748b)); font-size: 12px; }
.mailcom-operation-notes { display: grid; gap: 3px; }
.mailcom-account-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 10px; min-width: 0; max-height: min(520px, 60vh); overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; margin: 0; padding: 2px; list-style: none; }
.mailcom-account-card { display: grid; align-content: start; gap: 12px; min-width: 0; padding: 12px; border: 1px solid var(--line-subtle, var(--line, #cbd5e1)); border-radius: 9px; background: var(--field-bg, var(--bg-inset, #fff)); }
.mailcom-account-card.is-selected { border-color: var(--accent, var(--accent-cyan, #2563eb)); background: color-mix(in srgb, var(--accent, #2563eb) 7%, var(--field-bg, #fff)); }
.mailcom-account-card-head { display: flex; align-items: start; gap: 8px; min-width: 0; }
.mailcom-account-choice { display: flex; align-items: start; flex: 1 1 auto; gap: 8px; min-width: 0; padding: 5px 0; cursor: pointer; }
.mailcom-account-choice strong { min-width: 0; font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.mailcom-settings :is(.mailcom-account-select-all, .mailcom-account-choice, .mailcom-batch-concurrency, .mailcom-account-search, .mailcom-auto-rotate) { flex-direction: row; }
.mailcom-account-counts { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin: 0; padding-top: 10px; border-top: 1px solid var(--line-subtle, var(--line, #cbd5e1)); }
.mailcom-account-counts dt { font-size: 12px; line-height: 1.5; color: var(--text-secondary, var(--muted, #64748b)); }
.mailcom-account-counts dd { margin: 3px 0 0; font-size: 15px; font-weight: 600; line-height: 1.5; font-variant-numeric: tabular-nums; }
.mailcom-account-counts dd small { font-size: 12px; font-weight: 400; color: var(--text-secondary, var(--muted, #64748b)); }
.mailcom-account-proxy { display: block; color: var(--text-secondary, var(--muted, #64748b)); overflow-wrap: anywhere; }
.mailcom-proxy-list { display: flex; flex-wrap: wrap; gap: 6px; max-height: 140px; overflow: auto; }
.mailcom-proxy-list code { max-width: 100%; padding: 5px 7px; border: 1px solid var(--line-subtle, var(--line, #cbd5e1)); border-radius: 6px; background: var(--field-bg, var(--bg-inset, #fff)); font-size: 11px; overflow-wrap: anywhere; }
.mailcom-settings .mailcom-account-remove { flex: 0 0 auto; color: var(--error, var(--err, #dc2626)); white-space: nowrap; }
.mailcom-settings .mailcom-account-card-head button { padding: 5px 8px; }
.mailcom-batch-details summary { cursor: pointer; padding: 4px 0; }
.mailcom-bulk-report .mailcom-batch-details ul { display: grid; gap: 7px; margin-top: 5px; max-height: 260px; overflow: auto; color: inherit; }
.mailcom-batch-details .mailcom-result-error { color: var(--error, var(--err, #dc2626)); }
.mailcom-failure-reason { display: block; }
.mailcom-empty { padding: 12px 0; }
.mailcom-alias-cleanup { display: grid; gap: 9px; padding: 10px; border: 1px solid var(--line-subtle, var(--line, #cbd5e1)); border-radius: 8px; background: var(--bg-inset, #f8fafc); }
.mailcom-alias-cleanup-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.mailcom-alias-cleanup-head > div { display: grid; gap: 3px; min-width: 0; }
.mailcom-alias-cleanup-head small { color: var(--text-secondary, var(--muted, #64748b)); font-size: 12px; }
.mailcom-alias-delete-all { border-color: var(--error, var(--err, #dc2626)) !important; color: var(--error, var(--err, #dc2626)) !important; }
.mailcom-alias-list { display: flex; flex-wrap: wrap; gap: 6px; max-height: 150px; overflow: auto; }
.mailcom-alias-chip { display: inline-flex; align-items: baseline; gap: 7px; max-width: 100%; padding: 6px 8px; border: 1px solid var(--line-subtle, var(--line, #cbd5e1)); border-radius: 6px; background: var(--field-bg, var(--bg, #fff)); font-size: 11px; }
.mailcom-alias-chip strong { overflow-wrap: anywhere; }
.mailcom-alias-chip small { color: var(--text-muted, var(--muted, #64748b)); }
.mailcom-field { display: flex; flex-direction: column; gap: 6px; min-width: 0; font-size: 12px; }
.mailcom-settings input:not([type="checkbox"]), .mailcom-settings select, .mailcom-settings textarea { box-sizing: border-box; width: 100%; min-width: 0; padding: 9px 10px; border: 1px solid var(--field-border, var(--line, #cbd5e1)); border-radius: 8px; background: var(--field-bg, var(--bg-inset, #fff)); color: inherit; font: inherit; }
.mailcom-settings input[type="checkbox"] { box-sizing: border-box; display: inline-block; flex: 0 0 18px; width: 18px; min-width: 18px; max-width: 18px; height: 18px; min-height: 18px; max-height: 18px; margin: 0; padding: 0; accent-color: var(--accent, var(--primary-600, #2563eb)); cursor: pointer; }
.mailcom-settings input[type="checkbox"]:indeterminate { position: relative; border-color: var(--accent, var(--primary-600, #2563eb)); background: var(--accent, var(--primary-600, #2563eb)); }
.mailcom-settings input[type="checkbox"]:indeterminate::after { content: ""; position: absolute; inset: 7px 3px auto; height: 2px; border: 0; background: #fff; transform: none; }
.mailcom-settings textarea { min-height: 118px; resize: vertical; line-height: 1.5; }
.mailcom-settings button { padding: 8px 12px; border: 1px solid var(--field-border, var(--line, #cbd5e1)); border-radius: 8px; background: var(--field-bg, var(--bg-inset, #fff)); color: inherit; font: inherit; font-size: 12px; cursor: pointer; }
.mailcom-settings :is(input, select, textarea, button):disabled { opacity: 0.55; cursor: not-allowed; }
.mailcom-settings :is(input, select, textarea, button, summary):focus-visible { outline: 2px solid var(--accent, var(--accent-cyan, #2563eb)); outline-offset: 2px; }
.mailcom-connection summary { margin-bottom: 10px; font-size: 12px; cursor: pointer; }
.mailcom-bulk { display: grid; gap: 10px; padding: 10px; border: 1px dashed var(--line-subtle, var(--line, #cbd5e1)); border-radius: 8px; }
.mailcom-bulk summary { cursor: pointer; font-size: 12px; font-weight: 600; }
.mailcom-bulk-actions { display: flex; align-items: end; gap: 10px; flex-wrap: wrap; }
.mailcom-concurrency-field { display: flex; flex-direction: column; gap: 5px; min-width: 108px; font-size: 12px; }
.mailcom-settings .mailcom-concurrency-field input { width: 108px; }
.mailcom-bulk-report { display: grid; gap: 5px; padding: 8px 10px; border-radius: 7px; background: var(--bg-inset, #f8fafc); font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
.mailcom-bulk-report ul { margin: 0; padding-left: 18px; color: var(--error, var(--err, #dc2626)); }
.mailcom-bulk-report small { color: var(--text-secondary, var(--muted, #64748b)); }
.mailcom-domain-row, .mailcom-actions { display: flex; align-items: end; gap: 10px; flex-wrap: wrap; }
.mailcom-domain-row .mailcom-field { flex: 1; }
.mailcom-alias-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px; align-items: end; max-width: 560px; }
.mailcom-alias-options .mailcom-note, .mailcom-alias-options .mailcom-error { grid-column: 1 / -1; }
.mailcom-domain-filter { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.mailcom-domain-filter input { flex: 0 1 250px; min-width: 180px; }
.mailcom-domain-filter small { color: var(--text-secondary, var(--muted, #64748b)); font-size: 12px; }
.mailcom-domain-picker { display: grid; grid-template-columns: repeat(auto-fill, minmax(185px, 1fr)); gap: 7px 12px; max-height: 280px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 8px 10px; border: 1px solid var(--field-border, var(--line, #cbd5e1)); border-radius: 8px; background: var(--field-bg, var(--bg-inset, #fff)); }
.mailcom-domain-option { display: inline-flex; align-items: center; gap: 5px; min-width: 0; font-size: 12px; }
.mailcom-domain-option small { margin-left: auto; color: var(--text-secondary, var(--muted, #64748b)); font-size: 10px; white-space: nowrap; }
.mailcom-domain-option.is-live small { color: var(--success, #15803d); }
.mailcom-domain-option.is-not-returned { opacity: .72; }
.mailcom-actions { align-items: center; }
@media (max-width: 640px) {
  .mailcom-fields, .mailcom-alias-options { grid-template-columns: 1fr; }
  .mailcom-account-search { flex-basis: 100%; max-width: none; margin-left: 0; }
  .mailcom-account-batch-actions > button { flex: 1 1 220px; }
}
</style>
