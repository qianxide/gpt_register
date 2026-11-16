import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { Readable } from "node:stream";
import { setImmediate as turn } from "node:timers/promises";
import test from "node:test";
import { MailcodeService, makeServer } from "./server.mjs";

const PRIMARY = "primary@mail.com";
const ADDRESS = "managed-fixture@mail.com";
const ADMIN = "regression-admin-key-with-at-least-32-characters";
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const validationError = (message) => Object.assign(new Error(message), { name: "MailComValidationError" });
const codeMessage = (code = "012345") => ({ id: "message", code, received_at: 1_800_000_000.125, text: `OTP: ${code}` });

class Response extends EventEmitter {
  destroyed = false;
  writeHead(status, headers) { this.status = status; this.headers = headers; }
  end(text) { this.body = JSON.parse(text); }
  destroy() { this.destroyed = true; this.emit("close"); }
}

async function fixture(t, overrides = {}) {
  const directory = mkdtempSync(join(tmpdir(), "mailcode-regression-"));
  const live = new Set();
  const activity = { creates: [], deletes: [], listings: 0, logins: 0 };
  const options = {
    dataDir: directory, adminKey: ADMIN,
    makeWebClient: () => ({
      availableDomains: async () => ["mail.com", "email.com"],
      createAlias: async (address) => { activity.creates.push(address); live.add(address); },
      deleteAlias: async (address) => { activity.deletes.push(address); live.delete(address); },
    }),
    makeMailClient: () => ({
      auth: { login: async () => { activity.logins++; } },
      account: { aliases: async () => {
        activity.listings++;
        return { mailaddresslist: [...live].map((address) => ({ address, deletable: true })) };
      } },
    }),
    readMessages: async () => [],
    ...overrides,
  };
  const { server, service } = makeServer(options);
  t.after(() => {
    server.close();
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith("mailcode-regression-"));
    rmSync(directory, { recursive: true, force: true });
  });
  await service.importAccount({ email: PRIMARY, password: "old-password" });
  const create = (address = ADDRESS) => service.createAlias({ account_email: PRIMARY, domain: "mail.com", address });
  // Invoke the real HTTP listener without opening a socket or contacting upstream.
  const begin = (path, key = ADMIN) => {
    const request = Object.assign(Readable.from([]), { method: "GET", url: path, headers: { "x-api-key": key } });
    const response = new Response();
    const done = server.listeners("request")[0](request, response).then(() => response);
    return { response, done };
  };
  const query = (item, extra = {}) => new URLSearchParams({ email: item.address, key: item.key, ...extra });
  return { service, options, directory, live, activity, create, begin, query };
}

test("import remains storage-only and failed persistence restores the previous credentials", async (t) => {
  const f = await fixture(t, { makeWebClient: () => assert.fail("import must not validate login") });
  assert.deepEqual(await f.service.importAccount({ email: PRIMARY, password: "new-password" }), { email: PRIMARY, stored: true });
  t.mock.method(f.service.store, "save", () => { throw new Error("ENOSPC"); });
  await assert.rejects(f.service.importAccount({ email: PRIMARY, password: "rejected-password" }), /ENOSPC/);
  assert.equal(f.service.account(PRIMARY).password, "new-password");
  assert.equal(f.activity.logins, 0);
});

test("successful ordinary creates do not query mobile aliases", async (t) => {
  const f = await fixture(t);
  assert.equal((await f.create()).status, "active");
  assert.equal(f.activity.listings, 0);
  assert.equal(f.activity.logins, 0);
});

test("an uncertain create reconciles through public mobile aliases and keeps its reserved key", async (t) => {
  const f = await fixture(t);
  let reservedKey;
  f.service.makeWebClient = () => ({
    availableDomains: async () => ["mail.com"],
    createAlias: async (address) => {
      f.activity.creates.push(address);
      reservedKey = f.service.store.data.aliases[address].key;
      f.live.add(address);
      throw Object.assign(new Error("verification timed out"), { name: "TimeoutError" });
    },
  });
  const item = await f.create();
  assert.equal(item.status, "active");
  assert.equal(item.key, reservedKey);
  assert.equal(f.service.aliasForKey(item.address, item.key).key, reservedKey);
  assert.equal(f.activity.listings, 1);
  assert.equal(f.activity.creates.length, 1);
  assert.deepEqual(f.activity.deletes, []);
});

test("pending reservations survive failed reconciliation and recover exact addresses after restart", async (t) => {
  const f = await fixture(t);
  f.service.makeWebClient = () => ({
    availableDomains: async () => ["mail.com"],
    createAlias: async () => { throw Object.assign(new Error("verification unauthorized"), { status: 401 }); },
  });
  let aliases = [{ address: `x${ADDRESS}` }, { address: `${ADDRESS}.evil.test` }];
  f.options.makeMailClient = () => ({
    auth: { login: async () => {} },
    account: { aliases: async () => ({ mailaddresslist: aliases }) },
  });
  f.service.makeMailClient = f.options.makeMailClient;
  await assert.rejects(f.create(), /unauthorized/);
  const pending = f.service.store.data.aliases[ADDRESS];
  assert.equal(pending.status, "unconfirmed");
  assert.throws(() => f.service.aliasForKey(ADDRESS, pending.key), (error) => error.status === 401);
  const restored = new MailcodeService(f.options);
  aliases = [{ address: ADDRESS.toUpperCase(), deletable: true }];
  const recovered = await restored.createAlias({ account_email: PRIMARY, address: ADDRESS });
  assert.equal(recovered.status, "active");
  assert.equal(recovered.key, pending.key);
  assert.deepEqual(f.activity.creates, []);
  assert.deepEqual(f.activity.deletes, []);
});

test("failed mobile listing preserves uncertain state instead of inventing absence", async (t) => {
  const f = await fixture(t, {
    makeWebClient: () => ({ availableDomains: async () => ["mail.com"], createAlias: async () => { throw new Error("lost reply"); } }),
    makeMailClient: () => ({ auth: { login: async () => {} }, account: { aliases: async () => ({}) } }),
  });
  await assert.rejects(f.create(), /lost reply/);
  const key = f.service.store.data.aliases[ADDRESS].key;
  await assert.rejects(f.create(), /Invalid SDK alias listing/);
  assert.equal(f.service.store.data.aliases[ADDRESS].key, key);
});

test("definite SDK rejections clear reservations without adopting or deleting existing aliases", async (t) => {
  const f = await fixture(t);
  f.live.add(ADDRESS);
  for (const message of ["Alias already exists: existing", "The maximum number of Alias Addresses has been created", "Alias address is not available: existing"]) {
    f.service.makeWebClient = () => ({ availableDomains: async () => ["mail.com"], createAlias: async () => { throw validationError(message); } });
    await assert.rejects(f.create(), (error) => error.message === message);
    assert.equal(f.service.store.data.aliases[ADDRESS], undefined);
    await assert.rejects(f.service.deleteAlias(PRIMARY, ADDRESS), (error) => error.status === 404);
  }
  assert.ok(f.live.has(ADDRESS));
  assert.equal(f.activity.listings, 0);
  assert.deepEqual(f.activity.deletes, []);
});

test("absent pending reservations support retry and local cleanup without upstream deletion", async (t) => {
  const f = await fixture(t);
  const pending = { address: ADDRESS, primaryEmail: PRIMARY, key: "pending-key", status: "pending" };
  f.service.saveAlias(ADDRESS, pending);
  assert.equal((await f.create()).status, "active");
  assert.deepEqual(f.activity.creates, [ADDRESS]);
  const absent = "never-created@mail.com";
  f.service.saveAlias(absent, { ...pending, address: absent });
  await f.service.deleteAlias(PRIMARY, absent);
  assert.equal(f.service.store.data.aliases[absent], undefined);
  assert.deepEqual(f.activity.deletes, []);
});

test("creation POST 4xx clears reservations but a subsequent verification GET 4xx reconciles", async (t) => {
  const f = await fixture(t);
  const url = "https://settings-cats.mail.com/mailaccount/primary/emailAddresses?absoluteURI=false";
  for (const status of [409, 422]) {
    f.service.makeWebClient = () => ({
      availableDomains: async () => ["mail.com"],
      createAlias: async () => { throw Object.assign(new Error("creation rejected"), { name: "MailComApiError", method: "POST", url, status }); },
    });
    await assert.rejects(f.create(), /creation rejected/);
    assert.equal(f.service.store.data.aliases[ADDRESS], undefined);
  }
  assert.equal(f.activity.listings, 0);
  f.service.makeWebClient = () => ({
    availableDomains: async () => ["mail.com"],
    createAlias: async (address) => {
      f.live.add(address);
      throw Object.assign(new Error("verification rejected"), { name: "MailComApiError", method: "GET", url, status: 409 });
    },
  });
  assert.equal((await f.create()).status, "active");
  assert.equal(f.activity.listings, 1);
  assert.deepEqual(f.activity.deletes, []);
});

test("failed reservation persistence performs no upstream create and leaves no local reservation", async (t) => {
  const f = await fixture(t);
  t.mock.method(f.service.store, "save", () => { throw new Error("ENOSPC"); });
  await assert.rejects(f.create(), /ENOSPC/);
  assert.equal(f.service.store.data.aliases[ADDRESS], undefined);
  assert.deepEqual(f.activity.creates, []);
});

test("domain, create and delete operations read credentials after acquiring the account lock", async (t) => {
  const f = await fixture(t);
  await f.create();
  const passwords = [];
  f.service.webClients.clear();
  f.service.makeWebClient = ({ password }) => ({
    availableDomains: async () => { passwords.push(password); return ["mail.com"]; },
    createAlias: async () => { passwords.push(password); },
    deleteAlias: async () => { passwords.push(password); },
  });
  const gate = deferred();
  const blocked = f.service.locked(PRIMARY, () => gate.promise);
  const imported = f.service.importAccount({ email: PRIMARY, password: "new-password" });
  const domains = f.service.domains(PRIMARY);
  const created = f.create("second-fixture@mail.com");
  const deleted = f.service.deleteAlias(PRIMARY, ADDRESS);
  gate.resolve();
  await Promise.all([blocked, imported, domains, created, deleted]);
  // Domain discovery is cached, so the second create reuses the post-import
  // result while create/delete still read the current account credentials.
  assert.equal(passwords.length, 3);
  assert.ok(passwords.every((password) => password === "new-password"));
});

test("all message responses recheck revoked keys after an in-flight read", async (t) => {
  for (const path of ["messages", "code", "code/wait"]) {
    await t.test(path, async (t) => {
      const reading = deferred(), finish = deferred();
      const f = await fixture(t, { readMessages: async () => { reading.resolve(); await finish.promise; return [codeMessage()]; } });
      const old = await f.create();
      const call = f.begin(`/api/v1/${path}?${f.query(old, { timeout: "10" })}`);
      await reading.promise;
      await f.service.deleteAlias(PRIMARY, ADDRESS);
      const replacement = await f.create();
      assert.notEqual(replacement.key, old.key);
      finish.resolve();
      const response = await call.done;
      assert.equal(response.status, 401);
      assert.ok(!JSON.stringify(response.body).includes("012345"));
    });
  }
});

test("each new poll rechecks authorization before fetching another snapshot", async (t) => {
  let reads = 0;
  const f = await fixture(t, { readMessages: async () => { reads++; return []; } });
  const item = await f.create();
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_800_000_000_000 });
  const call = f.begin(`/api/v1/code/wait?${f.query(item, { timeout: "10" })}`);
  await turn();
  assert.equal(reads, 1);
  await f.service.deleteAlias(PRIMARY, ADDRESS);
  await f.create();
  t.mock.timers.tick(3000);
  assert.equal((await call.done).status, 401);
  assert.equal(reads, 1);
});

test("deadline is checked before another poll starts", async (t) => {
  let reads = 0;
  const f = await fixture(t, { readMessages: async () => { reads++; return []; } });
  const item = await f.create();
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_800_000_000_000 });
  const call = f.begin(`/api/v1/code/wait?${f.query(item, { timeout: "1" })}`);
  await turn();
  t.mock.timers.tick(1000);
  const response = await call.done;
  assert.equal(response.status, 200);
  assert.equal(response.body.timeout, true);
  assert.equal(response.body.code, "");
  assert.equal(reads, 1);
});

test("deadlines abort the SDK fetch during login and body reads", async (t) => {
  for (const stage of ["login", "body"]) {
    await t.test(stage, async (t) => {
      const started = deferred();
      let upstreamSignal;
      t.mock.method(globalThis, "fetch", (_url, { signal }) => {
        upstreamSignal = signal;
        started.resolve();
        return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
      });
      const f = await fixture(t, {
        makeMailClient: (_account, { fetch }) => ({
          auth: { login: async () => { if (stage === "login") await fetch("https://fixture.invalid/login"); } },
          body: () => fetch("https://fixture.invalid/body"),
        }),
        readMessages: async (client) => { await client.body(); return []; },
      });
      const item = await f.create();
      t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_800_000_000_000 });
      const call = f.begin(`/api/v1/code/wait?${f.query(item, { timeout: "1" })}`);
      await started.promise;
      t.mock.timers.tick(1000);
      const response = await call.done;
      assert.equal(upstreamSignal.aborted, true);
      assert.equal(response.body.timeout, true);
      assert.equal(response.status, 200);
    });
  }
});

test("cancelling one SDK client user leaves another account request's signal and result intact", async (t) => {
  const started = deferred();
  const signals = [];
  t.mock.method(globalThis, "fetch", (url, { signal }) => {
    signals.push(signal);
    if (url.endsWith("/slow")) {
      started.resolve();
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
    }
    return Promise.resolve({});
  });
  const f = await fixture(t, {
    makeMailClient: (_account, { fetch }) => ({ auth: { login: async () => {} }, fetch }),
    readMessages: async (client, address) => {
      await client.fetch(`https://fixture.invalid/${address === ADDRESS ? "slow" : "fast"}`);
      return [codeMessage()];
    },
  });
  const slow = await f.create(), fast = await f.create("fast-fixture@mail.com");
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_800_000_000_000 });
  const first = f.begin(`/api/v1/code/wait?${f.query(slow, { timeout: "1" })}`);
  await started.promise;
  const second = f.begin(`/api/v1/code/wait?${f.query(fast, { timeout: "10" })}`);
  t.mock.timers.tick(1000);
  assert.equal((await first.done).body.timeout, true);
  const result = await second.done;
  assert.equal(result.status, 200);
  assert.equal(result.body.code, "012345");
  assert.equal(signals.length, 2);
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);
});

test("disconnect cancels only that operation and a queued cancelled operation never reaches the SDK", async (t) => {
  const started = deferred(), finish = deferred();
  let reads = 0, signal;
  const f = await fixture(t, { readMessages: async (_client, _address, options) => {
    reads++;
    signal = options.signal;
    started.resolve();
    await finish.promise;
    return [];
  } });
  const item = await f.create();
  const first = f.begin(`/api/v1/code/wait?${f.query(item, { timeout: "10" })}`);
  await started.promise;
  const queued = f.begin(`/api/v1/code/wait?${f.query(item, { timeout: "10" })}`);
  queued.response.destroy();
  await queued.done;
  first.response.destroy();
  await first.done;
  assert.equal(signal.aborted, true);
  assert.equal(first.response.body, undefined);
  finish.resolve();
  await turn();
  assert.equal(reads, 1);
});

test("six-digit compatibility drops whole incompatible OTP messages and exposes received_at", async (t) => {
  const f = await fixture(t, { readMessages: async () => [
    { ...codeMessage("1234"), text: "OTP: 1234. Reference 654321." },
    { ...codeMessage("12345678"), text: "OTP: 12345678. Reference 987654." },
    codeMessage("012345"),
    { id: "ordinary", code: null, text: "Welcome" },
  ] });
  const item = await f.create();
  const messages = await f.begin(`/api/v1/messages?${f.query(item)}`).done;
  assert.deepEqual(messages.body.messages.map((message) => message.code), ["012345", null]);
  assert.ok(!JSON.stringify(messages.body).includes("654321"));
  assert.ok(!JSON.stringify(messages.body).includes("987654"));
  for (const endpoint of ["code", "code/wait"]) {
    const response = await f.begin(`/api/v1/${endpoint}?${f.query(item, { timeout: "0" })}`).done;
    assert.equal(response.body.code, "012345");
    assert.equal(response.body.received_at, codeMessage().received_at);
    assert.equal(response.body.received_at, response.body.mail.received_at);
  }
});
