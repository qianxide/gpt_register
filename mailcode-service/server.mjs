import { AsyncLocalStorage } from "node:async_hooks";
import { createHash, randomBytes, randomInt, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { MailComClient, MemorySessionStore } from "maildotcom-sdk";
import { MailComConfiguredAliasAddon, MAILCOM_ALIAS_DOMAINS } from "./web-alias-client.mjs";
import { ProxyAgent, fetch as undiciFetch } from "undici";
import { MailboxStore } from "./store.mjs";
import { fetchMailboxMessages } from "./mailbox.mjs";

export class ApiProblem extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function publicError(error) {
  if (error instanceof ApiProblem) return { status: error.status, message: error.message };
  const description = String(error?.message || "");
  if (/maximum number|alias.*limit/i.test(description)) return { status: 409, message: "mail.com 别名名额已满，请先检查已有别名" };
  if (/already exists|address is not available/i.test(description)) return { status: 409, message: "该别名地址已被占用，请更换名称" };
  if (/domain is not (supported|available)/i.test(description)) return { status: 422, message: "所选别名域名当前不可用，请刷新域名列表" };
  if (/not allowed for deletion/i.test(description)) return { status: 403, message: "该邮箱地址不允许删除" };
  if (error?.status === 429) return { status: 429, message: "mail.com 请求过于频繁，请稍后重试" };
  if (error?.name === "MailComAuthError" || [401, 403].includes(error?.status)) {
    return { status: 502, message: "mail.com 登录失败，请检查邮箱密码，或在 mail.com 网页完成账号验证后重试" };
  }
  if (/timeout|abort/i.test(error?.name + description)) return { status: 504, message: "连接 mail.com 超时，请稍后重试" };
  return { status: 502, message: "mail.com 接口请求失败，请稍后重试" };
}

function normalizeEmail(value) {
  const email = String(value || "").trim().toLowerCase();
  if (email.length > 254 || !/^[^\s\\@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new ApiProblem(400, "请填写有效的邮箱地址");
  return email;
}

function normalizeDomain(value) {
  const domain = String(value || "").trim().toLowerCase();
  if (!domain || domain.length > 254 || !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$/.test(domain)) {
    throw new ApiProblem(400, "请填写有效的别名域名");
  }
  if (!MAILCOM_ALIAS_DOMAIN_SET.has(domain)) {
    throw new ApiProblem(422, "所选别名域名不受 mail.com 支持");
  }
  return domain;
}

function secretEqual(actual, expected) {
  if (typeof actual !== "string" || typeof expected !== "string") return false;
  const left = Buffer.from(actual), right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

function boundedNumber(value, fallback, minimum, maximum) {
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) throw new ApiProblem(400, "数字参数无效");
  return Math.max(minimum, Math.min(maximum, number));
}

export const MAILCOM_ALIAS_LIMIT = 9;
const MAILCOM_DOMAIN_CACHE_TTL_MS = 5 * 60_000;
const MAILCOM_PROXY_POOL_LIMIT = 2000;
const MAILCOM_PROXY_FAILURE_COOLDOWN_MS = 5 * 60_000;
const MAILCOM_ALIAS_DOMAIN_SET = new Set(MAILCOM_ALIAS_DOMAINS);
const MAILCOM_ALIAS_OPERATION_TIMEOUT_MS = Math.max(
  10_000,
  Math.min(60_000, Number(process.env.MAILCOM_ALIAS_OPERATION_TIMEOUT_MS || 30_000)),
);

const operationSignal = new AsyncLocalStorage();
function normalizeProxy(value) {
  const raw = String(value || "").trim();
  if (!raw) throw new ApiProblem(400, "代理地址不能为空");
  let candidate = raw;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate)) {
    const parts = candidate.split(":");
    if (parts.length === 2) candidate = `http://${parts[0]}:${parts[1]}`;
    else if (parts.length === 4) {
      const [host, port, username, password] = parts;
      candidate = `http://${encodeURIComponent(username)}:${encodeURIComponent(password)}@${host}:${port}`;
    } else {
      throw new ApiProblem(400, "代理格式应为 http(s)://用户:密码@主机:端口、主机:端口或主机:端口:用户:密码");
    }
  }
  let parsed;
  try { parsed = new URL(candidate); }
  catch { throw new ApiProblem(400, "代理地址格式无效"); }
  if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || !parsed.port) {
    throw new ApiProblem(400, "mail.com 代理池目前支持 HTTP/HTTPS 代理，并且必须包含端口");
  }
  if ((parsed.pathname && parsed.pathname !== "/") || parsed.search || parsed.hash) {
    throw new ApiProblem(400, "代理地址不能包含路径、查询参数或片段");
  }
  return parsed.toString().replace(/\/$/, "");
}

function maskProxy(value) {
  if (!value) return "";
  try {
    const parsed = new URL(value);
    if (parsed.password) parsed.password = "***";
    const slot = createHash("sha256").update(value).digest("hex").slice(0, 8);
    return `${parsed.toString().replace(/\/$/, "")} [槽位 ${slot}]`;
  } catch { return "已配置代理"; }
}

function proxyNetworkError(error) {
  const values = [error?.code, error?.cause?.code, error?.name, error?.message, error?.cause?.message]
    .map((value) => String(value || "").toLowerCase()).join(" ");
  return /fetch failed|proxy|connect|socket|econn|timed?out|und_err|tls|network/.test(values);
}

function upstreamFetch(proxy, proxyAgents) {
  return async (url, options = {}) => {
    const signal = AbortSignal.any([options.signal, operationSignal.getStore(), AbortSignal.timeout(25_000)].filter(Boolean));
    signal.throwIfAborted();
    if (!proxy) return fetch(url, { ...options, signal });
    let dispatcher = proxyAgents.get(proxy);
    if (!dispatcher) {
      dispatcher = new ProxyAgent(proxy);
      proxyAgents.set(proxy, dispatcher);
    }
    try {
      return await undiciFetch(url, { ...options, signal, dispatcher });
    } catch (error) {
      if (options.signal?.aborted || operationSignal.getStore()?.aborted) throw error;
      if (proxyNetworkError(error)) {
        const problem = new ApiProblem(502, "mail.com 代理连接失败，请检查该账号绑定的代理后重试");
        problem.proxyFailure = true;
        throw problem;
      }
      throw error;
    }
  };
}

function proxyFailure(error, proxy = "") {
  if (!proxy) return false;
  return error?.proxyFailure === true
    // A proxy can return an HTTP 502 response instead of dropping the socket;
    // the SDK then exposes it as MailComApiError rather than ApiProblem.
    || error?.status === 502
    || /代理(?:池中的代理均)?连接失败/.test(String(error?.message || ""));
}

function withSignal(signal, action) {
  signal?.throwIfAborted();
  return operationSignal.run(signal, () => {
    const work = Promise.resolve().then(() => {
      signal?.throwIfAborted();
      return action();
    });
    if (!signal) return work;
    return new Promise((resolve, reject) => {
      const aborted = () => reject(signal.reason);
      signal.addEventListener("abort", aborted, { once: true });
      work.then(
        (value) => { signal.removeEventListener("abort", aborted); resolve(value); },
        (error) => { signal.removeEventListener("abort", aborted); reject(error); },
      );
    });
  });
}

function definiteCreateRejection(error) {
  if (error?.name === "MailComApiError" && error.status >= 400 && error.status < 500
    && error.method?.toUpperCase() === "POST") {
    try {
      if (new URL(error.url).pathname === "/mailaccount/primary/emailAddresses") return true;
    } catch { /* An unrecognized failure remains uncertain. */ }
  }
  // These SDK validation errors precede its POST; verification failures do not.
  return error?.name === "MailComValidationError"
    && /^(Alias (?:already exists:|address is not available:|domain is not (?:supported|available)|local part must be)|Invalid alias address:|The maximum number of Alias Addresses)/i.test(error.message);
}

export class MailcodeService {
  constructor({ dataDir, makeWebClient, makeMailClient, readMessages = fetchMailboxMessages }) {
    this.store = new MailboxStore(dataDir);
    this.webClients = new Map();
    this.mailClients = new Map();
    this.domainCache = new Map();
    this.locks = new Map();
    this.mailLocks = new Map();
    this.proxyAgents = new Map();
    this.proxyCooldowns = new Map();
    this.readMessages = readMessages;
    this.makeWebClient = makeWebClient || ((account) => new MailComConfiguredAliasAddon({
      ...account, fetch: upstreamFetch(account.proxy, this.proxyAgents),
    }));
    this.makeMailClient = makeMailClient || ((account) => new MailComClient({
      ...account, fetch: upstreamFetch(account.proxy, this.proxyAgents), sessionStore: new MemorySessionStore(),
    }));
    this._normalizeStoredProxyPool();
  }

  _normalizeStoredProxyPool() {
    const saved = this.store.data.proxyPool || {};
    const proxies = [];
    for (const value of Array.isArray(saved.proxies) ? saved.proxies : []) {
      try {
        const proxy = normalizeProxy(value);
        if (!proxies.includes(proxy)) proxies.push(proxy);
      } catch { /* Ignore malformed legacy values. */ }
    }
    this.store.data.proxyPool = {
      enabled: Boolean(saved.enabled) && proxies.length > 0,
      proxies,
      generation: Number.isSafeInteger(saved.generation) && saved.generation >= 0 ? saved.generation : 0,
    };
  }

  proxyPool() {
    return this.store.data.proxyPool;
  }

  selectedProxy(email, pool = this.proxyPool(), reserved = new Set()) {
    if (!pool.enabled || !pool.proxies.length) return "";
    const digest = createHash("sha256").update(`${normalizeEmail(email)}\0${pool.generation}`).digest();
    const start = digest.readUInt32BE(0) % pool.proxies.length;
    // Hashing gives stable distribution, but every candidate must still be
    // checked against current assignments so two accounts never share a slot.
    for (let offset = 0; offset < pool.proxies.length; offset += 1) {
      const candidate = pool.proxies[(start + offset) % pool.proxies.length];
      if (!reserved.has(candidate) && !this.proxyCoolingDown(candidate)) return candidate;
    }
    return "";
  }

  assignedProxies(pool = this.proxyPool(), excludeEmail = "") {
    const excluded = excludeEmail ? normalizeEmail(excludeEmail) : "";
    return new Set(Object.values(this.store.data.accounts)
      .filter((account) => account.email !== excluded && pool.proxies.includes(account.proxy))
      .map((account) => account.proxy));
  }

  activeProxy(account) {
    const pool = this.proxyPool();
    return pool.enabled && pool.proxies.includes(account.proxy) ? account.proxy : "";
  }

  proxyCoolingDown(proxy) {
    const until = this.proxyCooldowns.get(proxy) || 0;
    if (until > Date.now()) return true;
    this.proxyCooldowns.delete(proxy);
    return false;
  }

  failoverProxy(account, failedProxy, attempted = new Set()) {
    const pool = this.proxyPool();
    if (!failedProxy || !pool.enabled || pool.proxies.length < 2) return false;
    this.proxyCooldowns.set(failedProxy, Date.now() + MAILCOM_PROXY_FAILURE_COOLDOWN_MS);
    const index = pool.proxies.indexOf(failedProxy);
    const start = index >= 0 ? index : -1;
    const assigned = this.assignedProxies(pool, account.email);
    let replacement = "";
    for (let offset = 1; offset < pool.proxies.length; offset += 1) {
      const candidate = pool.proxies[(start + offset + pool.proxies.length) % pool.proxies.length];
      if (!attempted.has(candidate) && !assigned.has(candidate) && !this.proxyCoolingDown(candidate)) {
        replacement = candidate;
        break;
      }
    }
    if (!replacement) return false;
    const previous = account.proxy;
    account.proxy = replacement;
    try { this.store.save(); }
    catch (error) {
      account.proxy = previous;
      throw error;
    }
    this.webClients.delete(account.email);
    this.mailClients.delete(account.email);
    this.domainCache.delete(account.email);
    return true;
  }

  clientAccount(account) {
    return { ...account, proxy: this.activeProxy(account) };
  }

  proxyPoolSummary() {
    const pool = this.proxyPool();
    const assignedValues = Object.values(this.store.data.accounts)
      .map((account) => account.proxy)
      .filter((proxy) => pool.proxies.includes(proxy));
    const assigned = assignedValues.length;
    const uniqueAssigned = new Set(assignedValues).size;
    return {
      enabled: pool.enabled,
      count: pool.proxies.length,
      assigned,
      unique_assigned: uniqueAssigned,
      duplicate_assignments: assigned - uniqueAssigned,
      available: Math.max(0, pool.proxies.length - uniqueAssigned),
      generation: pool.generation,
      items: pool.proxies.map(maskProxy),
    };
  }

  configureProxyPool(input = {}) {
    const current = this.proxyPool();
    let proxies = current.proxies;
    if (input.proxies !== undefined) {
      if (!Array.isArray(input.proxies) || input.proxies.length > MAILCOM_PROXY_POOL_LIMIT) {
        throw new ApiProblem(400, `代理池一次最多保存 ${MAILCOM_PROXY_POOL_LIMIT} 条`);
      }
      proxies = [];
      for (const value of input.proxies) {
        const proxy = normalizeProxy(value);
        if (!proxies.includes(proxy)) proxies.push(proxy);
      }
    }
    const reassign = input.reassign === true;
    const pool = {
      enabled: input.enabled === undefined ? current.enabled && proxies.length > 0 : Boolean(input.enabled) && proxies.length > 0,
      proxies,
      generation: current.generation + (reassign ? 1 : 0),
    };
    const accounts = Object.values(this.store.data.accounts)
      .sort((left, right) => left.email.localeCompare(right.email));
    if (pool.enabled && proxies.length < accounts.length) {
      throw new ApiProblem(409, `代理池可用槽位不足：${proxies.length} 条代理，${accounts.length} 个主邮箱`);
    }
    this.store.data.proxyPool = pool;
    const reserved = new Set();
    for (const account of accounts) {
      const keep = !reassign && pool.proxies.includes(account.proxy) && !reserved.has(account.proxy);
      if (keep) reserved.add(account.proxy);
      else account.proxy = "";
    }
    for (const account of accounts) {
      if (account.proxy || !pool.enabled || !pool.proxies.length) continue;
      const proxy = this.selectedProxy(account.email, pool, reserved);
      if (proxy) {
        account.proxy = proxy;
        reserved.add(proxy);
      }
    }
    this.store.save();
    this.webClients.clear();
    this.mailClients.clear();
    this.domainCache.clear();
    this.proxyCooldowns.clear();
    for (const agent of this.proxyAgents.values()) Promise.resolve(agent.close()).catch(() => {});
    this.proxyAgents.clear();
    return this.proxyPoolSummary();
  }

  async locked(email, action, locks = this.locks) {
    const previous = locks.get(email) || Promise.resolve();
    const running = previous.catch(() => {}).then(action);
    locks.set(email, running);
    try { return await running; }
    finally { if (locks.get(email) === running) locks.delete(email); }
  }

  account(email) {
    const account = this.store.data.accounts[normalizeEmail(email)];
    if (!account) throw new ApiProblem(404, "请先保存并验证主邮箱账号");
    return account;
  }

  aliasCount(email) {
    const primary = normalizeEmail(email);
    return Object.values(this.store.data.aliases).filter((item) => (
      item
      && item.primaryEmail === primary
      && item.status !== "deleted"
    )).length;
  }

  web(account) {
    const transport = this.clientAccount(account);
    let cached = this.webClients.get(account.email);
    if (!cached || cached.password !== account.password || cached.proxy !== transport.proxy || Date.now() - cached.createdAt > 10 * 60_000) {
      cached = { client: this.makeWebClient(transport), password: account.password, proxy: transport.proxy, createdAt: Date.now() };
      this.webClients.set(account.email, cached);
    }
    return cached.client;
  }

  async useWeb(account, action, retry = true) {
    const attempted = new Set();
    let authRetried = false;
    while (true) {
      const proxy = this.activeProxy(account);
      attempted.add(proxy);
      try { return await action(this.web(account)); }
      catch (error) {
        this.webClients.delete(account.email);
        if (proxyFailure(error, proxy)) {
          const changed = this.failoverProxy(account, proxy, attempted);
          if (changed && retry) continue;
          if (changed) throw error;
          if (attempted.size >= this.proxyPool().proxies.length) {
            throw new ApiProblem(502, "mail.com 代理池中的代理均连接失败，请检查代理可用性");
          }
        }
        if (!retry || authRetried || ![401, 403].includes(error?.status)) throw error;
        authRetried = true;
      }
    }
  }

  async importAccount(input) {
    const email = normalizeEmail(input?.email);
    const password = typeof input?.password === "string" ? input.password : "";
    if (!password || password.length > 2048) throw new ApiProblem(400, "请填写邮箱密码");
    return this.locked(email, async () => {
      const previous = this.store.data.accounts[email];
      const pool = this.proxyPool();
      const reserved = this.assignedProxies(pool, email);
      let proxy = "";
      if (pool.enabled && pool.proxies.length) {
        if (pool.proxies.includes(previous?.proxy) && !reserved.has(previous.proxy)) proxy = previous.proxy;
        else proxy = this.selectedProxy(email, pool, reserved);
        if (!proxy) throw new ApiProblem(409, "代理池没有可用槽位，请先增加代理或减少已绑定账号");
      }
      const account = { email, password, importedAt: new Date().toISOString(), proxy };
      // Import saves credentials; the existing caller's following domains query
      // verifies login and persists its per-account ready/failed status.
      this.store.data.accounts[email] = account;
      try { this.store.save(); }
      catch (error) {
        if (previous) this.store.data.accounts[email] = previous;
        else delete this.store.data.accounts[email];
        throw error;
      }
      if (previous?.password !== password || previous?.proxy !== proxy) {
        this.webClients.delete(email);
        this.domainCache.delete(email);
      }
      this.mailClients.delete(email);
      const result = { email, stored: true };
      const active = maskProxy(this.activeProxy(account));
      if (active) result.proxy = active;
      return result;
    });
  }

  async deleteAccount(email) {
    const normalized = normalizeEmail(email);
    return this.locked(normalized, async () => {
      // This removes only the local adapter record. The remote mail.com
      // account and aliases are intentionally left untouched.
      const account = this.account(normalized);
      const aliases = Object.entries(this.store.data.aliases).filter(([, item]) => (
        item
        && item.primaryEmail === account.email
        && item.status !== "deleted"
      ));
      const previousAccount = account;
      const previousAliases = new Map(aliases);
      delete this.store.data.accounts[normalized];
      for (const [address] of aliases) delete this.store.data.aliases[address];
      this.webClients.delete(normalized);
      this.mailClients.delete(normalized);
      this.domainCache.delete(normalized);
      try { this.store.save(); }
      catch (error) {
        this.store.data.accounts[normalized] = previousAccount;
        for (const [address, item] of previousAliases) this.store.data.aliases[address] = item;
        throw error;
      }
      return {
        email: normalized,
        aliases_removed: aliases.length,
        remote_aliases_retained: true,
      };
    });
  }

  async _availableDomains(account) {
    const cached = this.domainCache.get(account.email);
    if (cached && Date.now() - cached.createdAt < MAILCOM_DOMAIN_CACHE_TTL_MS) {
      return [...cached.domains];
    }
    const domains = await this.useWeb(account, (web) => web.availableDomains());
    const normalized = Array.isArray(domains) ? [...domains] : [];
    this.domainCache.set(account.email, { createdAt: Date.now(), domains: normalized });
    return [...normalized];
  }

  async domains(email) {
    const normalized = normalizeEmail(email);
    return this.locked(normalized, () => this._availableDomains(this.account(normalized)));
  }

  saveAlias(address, item) {
    const previous = this.store.data.aliases[address];
    if (item) this.store.data.aliases[address] = item;
    else delete this.store.data.aliases[address];
    try { this.store.save(); }
    catch (error) {
      if (previous) this.store.data.aliases[address] = previous;
      else delete this.store.data.aliases[address];
      throw error;
    }
    return item ? { ...item } : null;
  }

  async liveAliases(email) {
    const result = await this.useMail(email, (client) => {
      const aliases = client?.account?.aliases;
      return typeof aliases === "function" ? aliases() : null;
    });
    if (result === null) return null;
    if (!Array.isArray(result?.mailaddresslist)) throw new Error("Invalid SDK alias listing");
    return result.mailaddresslist;
  }

  async liveAlias(email, address) {
    const aliases = await this.liveAliases(email);
    if (!Array.isArray(aliases)) return undefined;
    return aliases.find((item) => String(item?.address || "").trim().toLowerCase() === address);
  }

  async createAlias(input) {
    const email = normalizeEmail(input?.account_email);
    return this.locked(email, async () => {
      const account = this.account(email);
      const requested = input.address ? normalizeEmail(input.address) : "";
      if (requested === account.email) throw new ApiProblem(409, "主邮箱地址已存在");
      const reserved = requested && this.store.data.aliases[requested];
      if (reserved) {
        if (reserved.primaryEmail !== email || !["pending", "unconfirmed"].includes(reserved.status)) {
          throw new ApiProblem(409, "该别名已存在");
        }
        const live = await this.liveAlias(email, requested);
        if (live) {
          if (live.deletable === false) throw new ApiProblem(409, "该地址不是可管理的别名");
          return this.saveAlias(requested, { ...reserved, status: "active" });
        }
        this.saveAlias(requested, null);
      }
      const selected = String(input.domain || "random").trim().toLowerCase();
      // The panel's configured domain list is already an operator-selected
      // preference.  When a concrete domain is supplied, go straight to the
      // SDK mutation and let mail.com be the authority; a 422 then causes the
      // Python allocator to try the next primary mailbox.  Random mode still
      // discovers domains because it has no deterministic value to submit.
      const directDomain = input.skip_domain_check === true && selected !== "random";
      let domains;
      if (directDomain) {
        domains = [normalizeDomain(selected)];
      } else {
        domains = await this._availableDomains(account);
        if (!domains.length) throw new ApiProblem(422, "此账号目前没有可用的别名域名");
      }
      const domain = selected === "random" ? (requested ? requested.split("@")[1] : domains[randomInt(domains.length)]) : selected;
      if (!directDomain && !domains.includes(domain)) throw new ApiProblem(422, "所选别名域名当前不可用");
      const address = requested || `m${randomBytes(8).toString("hex")}@${domain}`;
      if (address.split("@")[1] !== domain) throw new ApiProblem(400, "别名地址与所选域名不一致");
      if (this.store.data.aliases[address]) throw new ApiProblem(409, "该别名已存在或创建结果等待确认");
      const localAliasCount = this.aliasCount(account.email);
      if (localAliasCount >= MAILCOM_ALIAS_LIMIT) {
        throw new ApiProblem(409, `一个 mail.com 主邮箱最多创建 ${MAILCOM_ALIAS_LIMIT} 个别名`);
      }
      const item = { address, primaryEmail: account.email, key: `alias_${randomBytes(32).toString("hex")}`,
        createdAt: new Date().toISOString(), status: "pending" };
      // Reserve the credential before an upstream mutation so interrupted creates remain recoverable.
      this.saveAlias(address, item);
      try {
        // Retrying the whole SDK mutation after a verification 401 can mask success.
        await this.useWeb(account, (web) => directDomain && web.createAliasInConfiguredDomain
          ? web.createAliasInConfiguredDomain(address, domain)
          : web.createAlias(address), false);
      } catch (error) {
        if (error?.aliasMutationAttempted === false) {
          this.saveAlias(address, null);
          throw error;
        }
        if (definiteCreateRejection(error)) {
          this.saveAlias(address, null);
          throw error;
        }
        this.saveAlias(address, { ...item, status: "unconfirmed" });
        let live;
        try { live = await this.liveAlias(email, address); }
        catch { /* Preserve the reservation until the read-only listing succeeds. */ }
        if (live && live.deletable !== false) return this.saveAlias(address, { ...item, status: "active" });
        throw error;
      }
      return this.saveAlias(address, { ...item, status: "active" });
    });
  }

  aliasItems(email) {
    const primary = normalizeEmail(email);
    return Object.values(this.store.data.aliases)
      .filter((item) => item && item.primaryEmail === primary && item.status !== "deleted")
      .map((item) => ({ ...item }));
  }

  aliasInventory() {
    return Object.values(this.store.data.aliases)
      .filter((item) => item && item.status !== "deleted")
      .map((item) => ({
        address: String(item.address || "").trim().toLowerCase(),
        primaryEmail: String(item.primaryEmail || "").trim().toLowerCase(),
        status: String(item.status || "active").trim().toLowerCase(),
        hasKey: Boolean(item.key),
      }))
      .filter((item) => item.address && item.primaryEmail);
  }

  async _deleteAliasLocked(account, address) {
    const normalized = normalizeEmail(address);
    const item = this.store.data.aliases[normalized];
    if (!item || item.primaryEmail !== account.email) {
      throw new ApiProblem(404, "未找到此主邮箱下由本服务创建的别名");
    }
    if (item.status !== "active" && !await this.liveAlias(account.email, normalized)) {
      this.saveAlias(normalized, null);
      return { address: normalized, remote_deleted: false };
    }
    try {
      await this.useWeb(account, (web) => web.deleteAlias(normalized));
    } catch (error) {
      // A prior remote deletion is safe to reconcile locally during a bulk run.
      if (error?.name === "MailComValidationError" && /alias not found/i.test(String(error.message || ""))) {
        this.saveAlias(normalized, null);
        return { address: normalized, remote_deleted: false };
      }
      throw error;
    }
    this.saveAlias(normalized, null);
    return { address: normalized, remote_deleted: true };
  }

  async deleteAlias(email, address) {
    const primary = normalizeEmail(email);
    const normalized = normalizeEmail(address);
    return this.locked(primary, async () => this._deleteAliasLocked(this.account(primary), normalized));
  }

  async deleteAliases(input) {
    const primary = normalizeEmail(input?.account_email);
    return this.locked(primary, async () => {
      const account = this.account(primary);
      const local = this.aliasItems(primary);
      let targets;
      if (input?.all === true || input?.addresses === undefined) {
        targets = local.map((item) => item.address);
      } else {
        if (!Array.isArray(input.addresses) || input.addresses.length > MAILCOM_ALIAS_LIMIT) {
          throw new ApiProblem(400, `一次最多处理 ${MAILCOM_ALIAS_LIMIT} 个别名`);
        }
        targets = [...new Set(input.addresses.map((address) => normalizeEmail(address)))];
      }
      const deleted = [];
      const failed = [];
      for (const address of targets) {
        try {
          await this._deleteAliasLocked(account, address);
          deleted.push(address);
        } catch (error) {
          failed.push({ address, error: publicError(error).message });
        }
      }
      return {
        email: primary,
        requested: targets.length,
        deleted,
        failed,
        remaining: this.aliasCount(primary),
      };
    });
  }

  aliasForKey(email, key, primary) {
    const normalized = email ? normalizeEmail(email) : "";
    const item = normalized ? this.store.data.aliases[normalized]
      : Object.values(this.store.data.aliases).find((alias) => secretEqual(key, alias.key));
    if (!item || item.status !== "active" || !secretEqual(key, item.key)
      || (primary && item.primaryEmail !== normalizeEmail(primary))) throw new ApiProblem(401, "邮箱取件凭证无效");
    return item;
  }

  async useMail(email, action, signal) {
    const normalized = normalizeEmail(email);
    // SDK login/refresh promises are shared within a client. Serialize its users so
    // aborting one operation never cancels another operation's authentication.
    return withSignal(signal, () => this.locked(normalized, async () => {
      signal?.throwIfAborted();
      const account = this.account(normalized);
      const attempted = new Set();
      while (true) {
        const transport = this.clientAccount(account);
        attempted.add(transport.proxy);
        let cached = this.mailClients.get(normalized);
        if (!cached || cached.password !== account.password || cached.proxy !== transport.proxy) {
          cached = {
            client: this.makeMailClient(transport, { fetch: upstreamFetch(transport.proxy, this.proxyAgents) }),
            password: account.password,
            proxy: transport.proxy,
            ready: false,
          };
          this.mailClients.set(normalized, cached);
        }
        try {
          if (!cached.ready) {
            await cached.client.auth.login();
            signal?.throwIfAborted();
            cached.ready = true;
          }
          const result = await action(cached.client);
          signal?.throwIfAborted();
          return result;
        } catch (error) {
          if (this.mailClients.get(normalized) === cached) this.mailClients.delete(normalized);
          if (proxyFailure(error, transport.proxy) && this.failoverProxy(account, transport.proxy, attempted)) continue;
          if (proxyFailure(error, transport.proxy) && attempted.size >= this.proxyPool().proxies.length) {
            throw new ApiProblem(502, "mail.com 代理池中的代理均连接失败，请检查代理可用性");
          }
          throw error;
        }
      }
    }, this.mailLocks));
  }

  async messages(item, options = {}) {
    this.aliasForKey(item.address, item.key, item.primaryEmail);
    const messages = await this.useMail(item.primaryEmail, (client) => this.readMessages(client, item.address, options), options.signal);
    this.aliasForKey(item.address, item.key, item.primaryEmail);
    // Python's six-digit consumer falls back to scanning subject/text. Omit the
    // entire non-six-digit OTP message so an unrelated reference is not reused.
    return messages.filter((message) => {
      const code = String(message.code ?? "");
      return !/^\d+$/.test(code) || code.length === 6;
    });
  }
}

async function jsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 65536) throw new ApiProblem(413, "请求内容过大");
    chunks.push(chunk);
  }
  try {
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("object required");
    return body;
  } catch { throw new ApiProblem(400, "请求必须是 JSON 对象"); }
}

async function boundedOperation(request, response, budget, action) {
  const disconnected = new AbortController();
  const expired = new AbortController();
  const abortDisconnected = () => disconnected.abort(new DOMException("Client disconnected", "AbortError"));
  request.once("aborted", abortDisconnected);
  response.once("close", abortDisconnected);
  if (request.aborted || response.destroyed) abortDisconnected();
  const timer = setTimeout(
    () => expired.abort(new DOMException("mail.com operation deadline exceeded", "TimeoutError")),
    budget,
  );
  timer.unref?.();
  try {
    return await withSignal(AbortSignal.any([disconnected.signal, expired.signal]), action);
  } finally {
    clearTimeout(timer);
    request.removeListener("aborted", abortDisconnected);
    response.removeListener("close", abortDisconnected);
  }
}

export function makeServer(options) {
  const adminKey = options.adminKey;
  if (typeof adminKey !== "string" || adminKey.length < 24) throw new Error("MAILCODE_ADMIN_KEY must contain at least 24 characters");
  const service = new MailcodeService(options);
  const server = createServer(async (request, response) => {
    const send = (status, value) => {
      if (response.destroyed) return;
      response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
      response.end(JSON.stringify(value));
    };
    try {
      const url = new URL(request.url, "http://localhost");
      const path = url.pathname;
      const params = url.searchParams;
      if (request.method === "GET" && path === "/api/health") {
        return send(200, { ok: true, service: "mailcode-sdk", sdk_version: "1.0.9" });
      }
      if (request.method === "GET" && ["/api/v1/messages", "/api/v1/code", "/api/v1/code/wait"].includes(path)) {
        const key = params.get("key") || request.headers["x-api-key"];
        const initial = service.aliasForKey(params.get("email"), key, params.get("account"));
        const authorize = () => service.aliasForKey(initial.address, key, initial.primaryEmail);
        const settings = {
          after: boundedNumber(params.get("after"), 0, 0, Date.now() / 1000 + 300),
          maxAgeSeconds: boundedNumber(params.get("max_age_seconds"), 600, 1, 2592000),
          limit: boundedNumber(params.get("limit"), 10, 1, 20),
          exclude: (params.get("exclude") || "").split(",").filter(Boolean),
        };
        const timeout = path.endsWith("/wait") ? boundedNumber(params.get("timeout"), 60, 0, 60) : 0;
        const interval = boundedNumber(params.get("interval"), 3, 3, 15);
        // timeout=0 (and /code or /messages) still requests one bounded snapshot.
        const budget = timeout > 0 ? Math.ceil(timeout * 1000) : 25_000;
        const deadline = Date.now() + budget;
        const controller = new AbortController();
        const expiry = new AbortController();
        const timeoutError = new DOMException("Mailbox request deadline exceeded", "TimeoutError");
        const timer = setTimeout(() => expiry.abort(timeoutError), budget);
        timer.unref?.();
        const disconnected = () => controller.abort();
        response.once("close", disconnected);
        const signal = AbortSignal.any([controller.signal, expiry.signal]);
        const expired = () => expiry.signal.aborted || Date.now() >= deadline;
        let messages = [];
        try {
          while (!response.destroyed && !expired()) {
            const item = authorize();
            const snapshot = await service.messages(item, { ...settings, signal });
            authorize();
            if (response.destroyed || expired()) break;
            messages = snapshot;
            const match = messages.find((message) => message.code);
            if (path.endsWith("/messages")) return send(200, { ok: true, messages, items: messages });
            if (match) return send(200, { ok: true, code: match.code, received_at: match.received_at, email: item.address, mail: match, messages });
            if (timeout === 0) break;
            await delay(Math.min(interval * 1000, Math.max(0, deadline - Date.now())), undefined, { signal });
          }
        } catch (error) {
          if (!response.destroyed && !expiry.signal.aborted) throw error;
        } finally {
          clearTimeout(timer);
          response.removeListener("close", disconnected);
        }
        if (response.destroyed) return;
        authorize();
        if (timeout === 0 && expired()) throw timeoutError;
        return send(200, { ok: true, code: "", email: initial.address, messages, timeout: timeout > 0 });
      }
      if (!secretEqual(request.headers["x-api-key"], adminKey)) throw new ApiProblem(401, "管理接口凭证无效");
      if (request.method === "GET" && path === "/api/v1/proxy-pool") {
        return send(200, { ok: true, proxy_pool: service.proxyPoolSummary() });
      }
      if (request.method === "PUT" && path === "/api/v1/proxy-pool") {
        return send(200, { ok: true, proxy_pool: service.configureProxyPool(await jsonBody(request)) });
      }
      if (request.method === "GET" && path === "/api/v1/accounts") {
        const includePasswords = params.get("include_passwords") === "1";
        return send(200, { ok: true, accounts: Object.values(service.store.data.accounts).map((account) => ({
          email: account.email, importedAt: account.importedAt,
           aliasCount: service.aliasCount(account.email),
           aliasLimit: MAILCOM_ALIAS_LIMIT,
           proxy: maskProxy(service.activeProxy(account)),
           proxyAssigned: Boolean(account.proxy),
           proxyActive: Boolean(service.activeProxy(account)),
          ...(includePasswords ? { password: account.password } : {}),
        })) });
      }
      if (request.method === "DELETE" && path.startsWith("/api/v1/accounts/")) {
        const email = decodeURIComponent(path.slice("/api/v1/accounts/".length));
        return send(200, { ok: true, ...(await service.deleteAccount(email)) });
      }
      if (request.method === "POST" && path === "/api/v1/import") {
        const body = await jsonBody(request);
        if (!Array.isArray(body.accounts) || !body.accounts.length || body.accounts.length > 50) throw new ApiProblem(400, "请提供 1 到 50 个主邮箱账号");
        const imported = [], errors = [];
        for (const input of body.accounts) {
          try { imported.push(await service.importAccount(input)); }
          catch (error) { errors.push({ email: String(input?.email || ""), error: publicError(error).message }); }
        }
        return send(200, { ok: true, imported, errors });
      }
      if (request.method === "GET" && path === "/api/v1/alias-domains") {
        return send(200, { ok: true, domains: await service.domains(params.get("account_email")) });
      }
      if (request.method === "GET" && path === "/api/v1/alias-inventory") {
        return send(200, { ok: true, items: service.aliasInventory() });
      }
      if (request.method === "GET" && path === "/api/v1/aliases") {
        const account = service.account(params.get("account_email"));
        return send(200, { ok: true, items: Object.values(service.store.data.aliases).filter((item) => item.primaryEmail === account.email) });
      }
      if (request.method === "POST" && path === "/api/v1/aliases") {
        const body = await jsonBody(request);
        const item = await boundedOperation(
          request, response, MAILCOM_ALIAS_OPERATION_TIMEOUT_MS, () => service.createAlias(body),
        );
        return send(200, { ok: true, item });
      }
      if (request.method === "DELETE" && path === "/api/v1/aliases") {
        return send(200, { ok: true, ...(await service.deleteAliases(await jsonBody(request))) });
      }
      if (request.method === "DELETE" && path.startsWith("/api/v1/aliases/")) {
        await service.deleteAlias(params.get("account_email"), decodeURIComponent(path.slice("/api/v1/aliases/".length)));
        return send(200, { ok: true });
      }
      throw new ApiProblem(404, "接口不存在");
    } catch (error) {
      const safe = publicError(error);
      send(safe.status, { ok: false, error: safe.message });
    }
  });
  server.requestTimeout = 120_000;
  server.headersTimeout = 15_000;
  return { server, service };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const host = process.env.MAILCODE_HOST || "127.0.0.1";
  if (!["127.0.0.1", "::1"].includes(host)) throw new Error("Bind the mailcode service to loopback only");
  const port = Number(process.env.MAILCODE_PORT || 18787);
  const { server } = makeServer({ adminKey: process.env.MAILCODE_ADMIN_KEY, dataDir: process.env.MAILCODE_DATA_DIR || "./data" });
  server.listen(port, host, () => console.log(`mailcode-sdk listening on ${host}:${port}`));
  for (const signal of ["SIGTERM", "SIGINT"]) process.once(signal, () => {
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 10_000).unref();
  });
}
