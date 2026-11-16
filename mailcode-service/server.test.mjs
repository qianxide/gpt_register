import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { makeServer, publicError } from "./server.mjs";

const adminKey = "test-admin-key-with-at-least-32-chars";
async function fixture(t, overrides = {}) {
  const directory = mkdtempSync(join(tmpdir(), "mailcode-test-"));
  const activity = { created: [], deleted: [], domains: 0, active: 0, maximum: 0 };
  const options = {
    dataDir: directory, adminKey,
    makeWebClient: ({ password }) => ({
      async availableDomains() {
        activity.domains += 1;
        if (password !== "test-password") throw Object.assign(new Error("Wrong password PRIVATE_VALUE"), { name: "MailComAuthError" });
        return ["mail.com", "email.com"];
      },
      async createAlias(address) {
        activity.active += 1;
        activity.maximum = Math.max(activity.maximum, activity.active);
        await delay(15);
        activity.created.push(address);
        activity.active -= 1;
        return { address };
      },
      async deleteAlias(address) { activity.deleted.push(address); },
    }),
    makeMailClient: () => ({ auth: { async login() {} } }),
    readMessages: async (_client, address) => [{ id: "m1", to: [address], code: "482931", subject: "Verification code" }],
    ...overrides,
  };
  const { server, service } = makeServer(options);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = `http://127.0.0.1:${server.address().port}`;
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((done) => server.close(done));
    rmSync(directory, { recursive: true, force: true });
  });
  const request = async (path, { method = "GET", body, key = adminKey } = {}) => {
    const response = await fetch(base + path, {
      method, headers: { "X-API-Key": key, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, value: await response.json() };
  };
  const importAccount = (email = "primary@mail.com", password = "test-password") => request("/api/v1/import", {
    method: "POST", body: { accounts: [{ email, password }] },
  });
  return { request, importAccount, service, options, directory, activity };
}

test("admin authorization protects account passwords and mutations", async (t) => {
  const f = await fixture(t);
  assert.equal((await f.request("/api/health", { key: "" })).value.service, "mailcode-sdk");
  assert.equal((await f.request("/api/v1/accounts?include_passwords=1", { key: "" })).status, 401);
  assert.equal((await f.request("/api/v1/import", { key: "wrong", method: "POST", body: { accounts: [] } })).status, 401);
  assert.equal(Object.keys(f.service.store.data.accounts).length, 0);
});

test("proxy pool assigns one unique slot per account", async (t) => {
  const f = await fixture(t);
  const proxies = [
    "http://user:pass-a@proxy.example:1000",
    "http://user:pass-b@proxy.example:1000",
    "http://user:pass-c@proxy.example:1000",
  ];
  f.service.configureProxyPool({ enabled: true, proxies, reassign: true });
  await f.service.importAccount({ email: "one@mail.com", password: "test-password" });
  await f.service.importAccount({ email: "two@mail.com", password: "test-password" });
  await f.service.importAccount({ email: "three@mail.com", password: "test-password" });
  const assigned = Object.values(f.service.store.data.accounts).map((account) => account.proxy);
  assert.equal(new Set(assigned).size, 3);
  assert.equal(f.service.proxyPoolSummary().duplicate_assignments, 0);
  await assert.rejects(
    f.service.importAccount({ email: "four@mail.com", password: "test-password" }),
    (error) => error?.status === 409,
  );
});

test("saving a pool repairs duplicate existing bindings", async (t) => {
  const f = await fixture(t);
  const proxies = [
    "http://user:pass-a@proxy.example:1000",
    "http://user:pass-b@proxy.example:1000",
    "http://user:pass-c@proxy.example:1000",
  ];
  f.service.configureProxyPool({ enabled: true, proxies, reassign: true });
  await f.service.importAccount({ email: "one@mail.com", password: "test-password" });
  await f.service.importAccount({ email: "two@mail.com", password: "test-password" });
  const first = f.service.store.data.accounts["one@mail.com"].proxy;
  f.service.store.data.accounts["two@mail.com"].proxy = first;
  f.service.configureProxyPool({ enabled: true, reassign: false });
  const assigned = Object.values(f.service.store.data.accounts).map((account) => account.proxy);
  assert.equal(new Set(assigned).size, 2);
  assert.equal(f.service.proxyPoolSummary().duplicate_assignments, 0);
});

test("undersized enabled pool is rejected without changing existing bindings", async (t) => {
  const f = await fixture(t);
  const proxies = [
    "http://user:pass-a@proxy.example:1000",
    "http://user:pass-b@proxy.example:1000",
  ];
  f.service.configureProxyPool({ enabled: true, proxies, reassign: true });
  await f.service.importAccount({ email: "one@mail.com", password: "test-password" });
  await f.service.importAccount({ email: "two@mail.com", password: "test-password" });
  const before = structuredClone(f.service.store.data);
  assert.throws(
    () => f.service.configureProxyPool({ enabled: true, proxies: proxies.slice(0, 1), reassign: true }),
    (error) => error?.status === 409,
  );
  assert.deepEqual(f.service.store.data, before);
});

test("masked proxy includes a stable anonymous slot identifier", async (t) => {
  const f = await fixture(t);
  f.service.configureProxyPool({
    enabled: true,
    proxies: ["http://user:pass-a@proxy.example:1000"],
    reassign: true,
  });
  const imported = await f.service.importAccount({ email: "one@mail.com", password: "test-password" });
  assert.match(imported.proxy, /\[槽位 [0-9a-f]{8}\]$/);
  assert.ok(!imported.proxy.includes("pass-a"));
});

test("accounts persist encrypted and survive restart with real domain validation", async (t) => {
  const f = await fixture(t);
  const imported = await f.importAccount("PRIMARY@mail.com");
  assert.equal(imported.status, 200);
  assert.equal(imported.value.imported[0].email, "primary@mail.com");
  assert.deepEqual((await f.request("/api/v1/alias-domains?account_email=primary%40mail.com")).value.domains, ["mail.com", "email.com"]);
  assert.equal((await f.request("/api/v1/accounts")).value.accounts[0].password, undefined);
  assert.equal((await f.request("/api/v1/accounts?include_passwords=1")).value.accounts[0].password, "test-password");
  const disk = readFileSync(join(f.directory, "accounts.enc"), "utf8");
  assert.ok(!disk.includes("test-password") && !disk.includes("primary@mail.com"));
  const restored = makeServer(f.options);
  assert.equal(restored.service.account("primary@mail.com").password, "test-password");
  restored.server.close();
});

test("concurrent domain discovery reuses one upstream result per mailbox", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  const responses = await Promise.all(Array.from({ length: 5 }, () => (
    f.request("/api/v1/alias-domains?account_email=primary%40mail.com")
  )));
  assert.ok(responses.every((response) => response.status === 200));
  assert.equal(f.activity.domains, 1);
});

test("failed login keeps stored credentials while domain validation reports an error", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  const result = await f.importAccount("primary@mail.com", "wrong-password");
  assert.equal(result.value.imported.length, 1);
  assert.equal(result.value.imported[0].stored, true);
  const domains = await f.request("/api/v1/alias-domains?account_email=primary%40mail.com");
  assert.equal(domains.status, 502);
  assert.ok(!JSON.stringify(domains).includes("PRIVATE_VALUE"));
  assert.equal(f.service.account("primary@mail.com").password, "wrong-password");
  const restored = makeServer(f.options);
  assert.equal(restored.service.account("primary@mail.com").password, "wrong-password");
  restored.server.close();
});

test("deleting an account removes only its local credentials and aliases", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  const created = await f.request("/api/v1/aliases", {
    method: "POST", body: { account_email: "primary@mail.com", domain: "mail.com" },
  });
  const path = `/api/v1/accounts/${encodeURIComponent("primary@mail.com")}`;
  const deleted = await f.request(path, { method: "DELETE" });
  assert.equal(deleted.status, 200);
  assert.equal(deleted.value.email, "primary@mail.com");
  assert.equal(deleted.value.aliases_removed, 1);
  assert.equal(deleted.value.remote_aliases_retained, true);
  assert.deepEqual((await f.request("/api/v1/accounts")).value.accounts, []);
  assert.equal((await f.request("/api/v1/alias-domains?account_email=primary%40mail.com")).status, 404);
  assert.equal(f.service.store.data.aliases[created.value.item.address], undefined);
});

test("alias creation returns compatible credentials with exact mailbox key isolation", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  const a = await f.request("/api/v1/aliases", { method: "POST", body: { account_email: "primary@mail.com", domain: "mail.com" } });
  const b = await f.request("/api/v1/aliases", { method: "POST", body: { account_email: "primary@mail.com", domain: "email.com" } });
  assert.equal(a.status, 200);
  assert.equal(a.value.item.primaryEmail, "primary@mail.com");
  assert.match(a.value.item.address, /^m[a-f0-9]+@mail\.com$/);
  const query = new URLSearchParams({ email: a.value.item.address, key: a.value.item.key });
  assert.equal((await f.request(`/api/v1/code/wait?${query}&timeout=0`, { key: "" })).value.code, "482931");
  query.set("key", b.value.item.key);
  assert.equal((await f.request(`/api/v1/messages?${query}`, { key: "" })).status, 401);
  query.set("key", a.value.item.key);
  query.set("account", "other@mail.com");
  assert.equal((await f.request(`/api/v1/messages?${query}`, { key: "" })).status, 401);
});

test("alias deletion requires its owning account and invalidates the old key", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  await f.importAccount("other@mail.com");
  const created = await f.request("/api/v1/aliases", { method: "POST", body: { account_email: "primary@mail.com", domain: "mail.com" } });
  const { address, key } = created.value.item;
  const path = `/api/v1/aliases/${encodeURIComponent(address)}`;
  assert.equal((await f.request(`${path}?account_email=other%40mail.com`, { method: "DELETE" })).status, 404);
  assert.equal(f.activity.deleted.length, 0);
  assert.equal((await f.request(`${path}?account_email=primary%40mail.com`, { method: "DELETE" })).status, 200);
  assert.equal(f.activity.deleted.length, 1);
  assert.equal((await f.request(`/api/v1/messages?${new URLSearchParams({ email: address, key })}`, { key: "" })).status, 401);
});

test("alias deletion switches to an unused proxy and continues in the same request", async (t) => {
  const attempts = [];
  const f = await fixture(t, {
    makeWebClient: ({ proxy }) => ({
      async deleteAlias(address) {
        attempts.push({ address, proxy });
        if (attempts.length === 1) {
          throw Object.assign(new Error("proxy connection failed"), { status: 502, proxyFailure: true });
        }
      },
    }),
  });
  const proxies = [
    "http://user:pass-a@proxy.example:1000",
    "http://user:pass-b@proxy.example:1000",
    "http://user:pass-c@proxy.example:1000",
  ];
  f.service.configureProxyPool({ enabled: true, proxies, reassign: true });
  await f.service.importAccount({ email: "primary@mail.com", password: "test-password" });
  await f.service.importAccount({ email: "other@mail.com", password: "test-password" });
  const account = f.service.account("primary@mail.com");
  const otherProxy = f.service.account("other@mail.com").proxy;
  const failedProxy = account.proxy;
  const address = "delete-me@mail.com";
  f.service.saveAlias(address, { address, primaryEmail: account.email, key: "alias-key", status: "active" });

  const result = await f.service.deleteAlias(account.email, address);

  assert.equal(result.remote_deleted, true);
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].proxy, failedProxy);
  assert.notEqual(attempts[1].proxy, failedProxy);
  assert.notEqual(attempts[1].proxy, otherProxy);
  assert.equal(account.proxy, attempts[1].proxy);
  assert.ok(f.service.proxyCoolingDown(failedProxy));
  assert.equal(f.service.store.data.aliases[address], undefined);
});

test("alias deletion reconciles a lost success after proxy failover", async (t) => {
  let remotePresent = true;
  let mutations = 0;
  const f = await fixture(t, {
    makeWebClient: ({ proxy }) => ({
      async deleteAlias(address) {
        if (!remotePresent) {
          throw Object.assign(new Error(`Alias not found: ${address}`), { name: "MailComValidationError" });
        }
        mutations += 1;
        remotePresent = false;
        throw Object.assign(new Error(`response lost through ${proxy}`), { status: 502, proxyFailure: true });
      },
    }),
  });
  f.service.configureProxyPool({
    enabled: true,
    proxies: ["http://user:pass-a@proxy.example:1000", "http://user:pass-b@proxy.example:1000"],
    reassign: true,
  });
  await f.service.importAccount({ email: "primary@mail.com", password: "test-password" });
  const address = "already-gone@mail.com";
  f.service.saveAlias(address, { address, primaryEmail: "primary@mail.com", key: "alias-key", status: "active" });

  const result = await f.service.deleteAlias("primary@mail.com", address);

  assert.equal(result.remote_deleted, false);
  assert.equal(mutations, 1);
  assert.equal(f.service.store.data.aliases[address], undefined);
});

test("alias deletion keeps local state when every available proxy fails", async (t) => {
  const attempts = [];
  const f = await fixture(t, {
    makeWebClient: ({ proxy }) => ({
      async deleteAlias() {
        attempts.push(proxy);
        throw Object.assign(new Error("proxy connection failed"), { status: 502, proxyFailure: true });
      },
    }),
  });
  f.service.configureProxyPool({
    enabled: true,
    proxies: ["http://user:pass-a@proxy.example:1000", "http://user:pass-b@proxy.example:1000"],
    reassign: true,
  });
  await f.service.importAccount({ email: "primary@mail.com", password: "test-password" });
  const address = "keep-me@mail.com";
  f.service.saveAlias(address, { address, primaryEmail: "primary@mail.com", key: "alias-key", status: "active" });

  await assert.rejects(
    f.service.deleteAlias("primary@mail.com", address),
    (error) => error?.status === 502,
  );

  assert.equal(attempts.length, 2);
  assert.equal(new Set(attempts).size, 2);
  assert.equal(f.service.store.data.aliases[address].status, "active");
});

test("concurrent alias mutations are serialized per primary mailbox", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  const responses = await Promise.all(Array.from({ length: 3 }, () => f.request("/api/v1/aliases", {
    method: "POST", body: { account_email: "primary@mail.com", domain: "random" },
  })));
  assert.ok(responses.every((response) => response.status === 200));
  assert.equal(f.activity.maximum, 1);
  assert.equal(new Set(responses.map((response) => response.value.item.key)).size, 3);
});

test("enforces the nine-alias quota and reports it without exposing passwords", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  for (let index = 1; index <= 9; index += 1) {
    const address = `existing-${index}@mail.com`;
    f.service.saveAlias(address, {
      address,
      primaryEmail: "primary@mail.com",
      key: `existing-key-${index}`,
      status: "active",
    });
  }
  const rejected = await f.request("/api/v1/aliases", {
    method: "POST",
    body: { account_email: "primary@mail.com", domain: "mail.com" },
  });
  assert.equal(rejected.status, 409);
  assert.match(rejected.value.error, /9/);
  assert.deepEqual(f.activity.created, []);
  const accounts = await f.request("/api/v1/accounts");
  assert.equal(accounts.value.accounts[0].aliasCount, 9);
  assert.equal(accounts.value.accounts[0].aliasLimit, 9);
  assert.equal(accounts.value.accounts[0].password, undefined);
});

test("unknown or mismatched domains never reach upstream creation", async (t) => {
  const f = await fixture(t);
  await f.importAccount();
  assert.equal((await f.request("/api/v1/aliases", { method: "POST", body: { account_email: "primary@mail.com", domain: "example.invalid" } })).status, 422);
  assert.equal((await f.request("/api/v1/aliases", { method: "POST", body: { account_email: "primary@mail.com", domain: "mail.com", address: "x@email.com" } })).status, 400);
  assert.equal(f.activity.created.length, 0);
});

test("upstream URL and token details are never exposed in public errors", () => {
  const result = publicError(Object.assign(new Error("https://example.test?token=PRIVATE_TOKEN password=PRIVATE_PASSWORD"), { status: 500 }));
  assert.equal(result.status, 502);
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
});
