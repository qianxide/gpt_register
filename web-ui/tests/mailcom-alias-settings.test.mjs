import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";
import { MAILCOM_ALIAS_DOMAINS } from "../../mailcode-service/node_modules/maildotcom-sdk/dist/src/web-alias-domains.js";

// Exercise the component's real reactive state and handlers with an in-memory API.
const componentSource = readFileSync(
  new URL("../src/components/MailcomRebindSettings.vue", import.meta.url), "utf8",
);
const source = componentSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
const { outputText } = ts.transpileModule(`${source}\nexport {
  aliasNameMode, aliasNameLength, aliasNameMinLength, aliasNameLengthValid,
  aliasNamePreview, changeAliasNameMode, applyMetadata, selectedDomains, domainOptions,
  visibleDomainOptions, domainSearch, liveDomainSet, domainDiagnosticComplete,
  dirty, bulkText, canSave, canBulkImport, ready, domains, domainsError, accounts,
  loadSettings, refreshDomains, save, bulkImport,
};`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });

async function component(t, overrides = {}, { failDomains = false } = {}) {
  const settings = {
    ok: true, configured: true, admin_key_configured: true,
    api_base: "http://127.0.0.1:18787", account_email: "owner@mail.com",
    accounts: [{ email: "owner@mail.com" }], domains: ["dr.com"],
    alias_domain: "dr.com", alias_domains: ["dr.com"],
    alias_name_mode: "random", alias_name_length: 17,
    ...overrides,
  };
  const requests = [];
  const cleanup = [];
  const scope = vue.effectScope();
  t.after(() => {
    cleanup.forEach((fn) => fn());
    scope.stop();
  });
  const imports = {
    vue: { ...vue, onMounted: () => {}, onBeforeUnmount: (fn) => cleanup.push(fn) },
    "../constants/mailcomAliasDomains.js": { MAILCOM_ALIAS_DOMAINS },
    "../api.js": {
      jsonBody: JSON.stringify,
      async api(url, options = {}) {
        const payload = options.body ? JSON.parse(options.body) : undefined;
        requests.push({ url, method: options.method || "GET", payload });
        if (url.startsWith("/api/mailcom/settings") || url === "/api/mailcom/accounts/import-batch") {
          return { ...settings, ...payload, login_ok: true };
        }
        if (url.startsWith("/api/mailcom/domains?")) {
          if (failDomains) return { ok: false, error: "mail.com alias domains HTTP 502" };
          return { ok: true, domains: ["dr.com"] };
        }
        if (url.startsWith("/api/mailcom/aliases?")) return { ok: true, items: [] };
        assert.fail(`Unexpected request: ${url}`);
      },
    },
  };
  const exports = {};
  scope.run(() => runInNewContext(outputText, {
    exports, AbortController,
    defineProps: () => ({}),
    defineEmits: () => () => {},
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
  }));
  await exports.loadSettings();
  return { ...exports, settings, requests };
}

test("bundled alias-domain catalog exactly matches maildotcom-sdk 1.0.9", () => {
  const catalogSource = readFileSync(new URL("../src/constants/mailcomAliasDomains.ts", import.meta.url), "utf8");
  const catalogJs = ts.transpileModule(catalogSource, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const exports = {};
  runInNewContext(catalogJs, { exports });
  assert.equal(MAILCOM_ALIAS_DOMAINS.length, 189);
  assert.deepEqual([...exports.MAILCOM_ALIAS_DOMAINS], [...MAILCOM_ALIAS_DOMAINS]);
  assert.ok(exports.MAILCOM_ALIAS_DOMAINS.includes("dr.com"));
});

for (const length of [8, 9, 10, 11, 12]) {
  test(`loading a legacy full-name length of ${length} raises it to 13`, async (t) => {
    const h = await component(t, { alias_name_mode: "name_digits", alias_name_length: length });
    assert.equal(h.aliasNameLength.value, 13);
    assert.equal(h.aliasNameMinLength.value, 13);
    assert.equal(h.canSave.value, true);
    assert.equal(h.dirty.value, false);
    await h.save();
    assert.equal(h.requests.find((item) => item.method === "PUT").payload.alias_name_length, 13);
  });
}

test("random mode retains 12 characters and both modes retain the default 17", async (t) => {
  const h = await component(t, { alias_name_length: 12 });
  assert.equal(h.aliasNameLength.value, 12);
  assert.equal(h.aliasNameMinLength.value, 8);
  assert.equal(h.canSave.value, true);
  for (const mode of ["random", "name_digits"]) {
    h.applyMetadata({ ...h.settings, alias_name_mode: mode, alias_name_length: undefined });
    assert.equal(h.aliasNameLength.value, 17);
  }
});

test("switching to full names raises short values and preserves longer choices", async (t) => {
  const h = await component(t, { alias_name_length: 12 });
  h.aliasNameMode.value = "name_digits";
  h.changeAliasNameMode();
  assert.equal(h.aliasNameLength.value, 13);
  assert.equal(h.aliasNameMinLength.value, 13);
  assert.equal(h.dirty.value, true);
  h.aliasNameLength.value = 20;
  for (const mode of ["random", "name_digits"]) {
    h.aliasNameMode.value = mode;
    h.changeAliasNameMode();
    assert.equal(h.aliasNameLength.value, 20);
  }
});

test("invalid manual lengths remain visible and block save and bulk import", async (t) => {
  const h = await component(t, { alias_name_mode: "name_digits" });
  h.bulkText.value = "owner@mail.com---fixture-password";
  for (const length of [12, 0, 33, 13.5, "", NaN]) {
    h.aliasNameLength.value = length;
    assert.equal(h.aliasNameLength.value, length);
    assert.equal(h.aliasNameLengthValid.value, false);
    assert.equal(h.aliasNamePreview.value, "");
    assert.equal(h.canSave.value, false);
    assert.equal(h.canBulkImport.value, false);
    await h.save();
    await h.bulkImport();
  }
  assert.equal(h.requests.some((item) => item.method !== "GET"), false);
  h.aliasNameLength.value = 13;
  assert.equal(h.canSave.value, true);
  assert.equal(h.canBulkImport.value, true);
  await h.bulkImport();
  const request = h.requests.find((item) => item.method === "POST");
  assert.equal(request.payload.alias_name_mode, "name_digits");
  assert.equal(request.payload.alias_name_length, 13);
});

test("full-name previews match the chosen length and keep at least five digits", async (t) => {
  const h = await component(t, { alias_name_mode: "name_digits" });
  for (let length = 13; length <= 32; length++) {
    h.aliasNameLength.value = length;
    const [localPart, domain] = h.aliasNamePreview.value.split("@");
    assert.equal(localPart.length, length);
    assert.match(localPart, /^(lilyward|elenaparker)\d{5,}$/);
    assert.equal(domain, "dr.com");
  }
  h.aliasNameLength.value = 16;
  assert.equal(h.aliasNamePreview.value, "elenaparker48271@dr.com");
  h.selectedDomains.value = ["email.com"];
  assert.equal(h.aliasNamePreview.value, "elenaparker48271@email.com");
});

test("random previews support the full 8 to 32 character range", async (t) => {
  const h = await component(t);
  for (let length = 8; length <= 32; length++) {
    h.aliasNameLength.value = length;
    assert.equal(h.aliasNameLengthValid.value, true);
    const localPart = h.aliasNamePreview.value.split("@")[0];
    assert.equal(localPart.length, length);
    assert.match(localPart, /^[a-z0-9]+$/);
  }
});

test("management metadata refresh preserves an unsaved alias draft", async (t) => {
  const h = await component(t);
  h.aliasNameMode.value = "name_digits";
  h.changeAliasNameMode();
  h.aliasNameLength.value = 12;
  h.applyMetadata({ ...h.settings, alias_name_length: 20 }, true);
  assert.equal(h.aliasNameMode.value, "name_digits");
  assert.equal(h.aliasNameLength.value, 12);
  assert.equal(h.aliasNameLengthValid.value, false);
  assert.equal(h.dirty.value, true);
});

test("saved mailboxes are ready without a domain probe", async (t) => {
  const h = await component(t, { domains: [], alias_domain: "random", alias_domains: [] });
  assert.equal(h.ready.value, true);
  assert.equal(h.domains.value.length, 0);
  assert.deepEqual(h.requests.map((request) => request.url), ["/api/mailcom/settings?include_usage=0"]);
});

test("fast settings load keeps all local alias inventory counters", async (t) => {
  const h = await component(t, {
    accounts: [{
      email: "owner@mail.com",
      alias_count: 9,
      alias_limit: 9,
      bound_alias_count: 5,
      unbound_alias_count: 4,
      available_alias_count: 2,
      quarantined_alias_count: 2,
    }],
  });
  const account = h.accounts.value[0];
  assert.equal(account.alias_count, 9);
  assert.equal(account.bound_alias_count, 5);
  assert.equal(account.unbound_alias_count, 4);
  assert.equal(account.available_alias_count, 2);
  assert.equal(account.quarantined_alias_count, 2);
  assert.deepEqual(h.requests.map((request) => request.url), ["/api/mailcom/settings?include_usage=0"]);
  assert.match(componentSource, />隔离<\/dt><dd>\{\{ account\.quarantined_alias_count/);
});

test("saved domains remain selectable after reopening without a login query", async (t) => {
  const h = await component(t, {
    domains: [], alias_domain: "dr.com", alias_domains: ["dr.com", "mail.com"],
  });
  assert.equal(h.ready.value, true);
  assert.equal(h.selectedDomains.value.join(","), "dr.com,mail.com");
  assert.equal(h.domainOptions.value.length, 189);
  assert.ok(h.domainOptions.value.includes("dr.com"));
  assert.ok(h.domainOptions.value.includes("mail.com"));
  assert.equal(h.requests.some((request) => request.url.startsWith("/api/mailcom/domains?")), false);
});

test("the complete static catalog is shown and can be filtered without any login query", async (t) => {
  const h = await component(t, { domains: [], alias_domain: "random", alias_domains: [] });
  assert.deepEqual([...h.domainOptions.value], [...MAILCOM_ALIAS_DOMAINS]);
  assert.equal(h.visibleDomainOptions.value.length, 189);
  h.domainSearch.value = "dr.com";
  assert.deepEqual([...h.visibleDomainOptions.value], ["dr.com"]);
  assert.equal(h.requests.some((request) => request.url.startsWith("/api/mailcom/domains?")), false);
});

test("saving settings skips login verification and leaves automatic allocation ready", async (t) => {
  const h = await component(t, { domains: [], alias_domain: "random", alias_domains: [] });
  await h.save();
  const saveRequest = h.requests.find((request) => request.method === "PUT");
  assert.equal(saveRequest.payload.verify, false);
  assert.equal(h.requests.some((request) => request.url.startsWith("/api/mailcom/domains?")), false);
  assert.equal(h.ready.value, true);
});

test("an optional domain diagnostic failure does not block saved accounts", async (t) => {
  const h = await component(t, { domains: [], alias_domain: "random", alias_domains: [] }, { failDomains: true });
  await h.refreshDomains();
  assert.match(h.domainsError.value, /HTTP 502/);
  assert.equal(h.ready.value, true);
  assert.equal(h.domainDiagnosticComplete.value, false);
});

test("the optional diagnostic checks only the current account and never changes selected domains", async (t) => {
  const h = await component(t, { domains: [], alias_domain: "dr.com", alias_domains: ["dr.com", "mail.com"] });
  const before = [...h.selectedDomains.value];
  await h.refreshDomains();
  assert.deepEqual(h.requests.slice(1).map((request) => request.url), [
    "/api/mailcom/domains?account_email=owner%40mail.com",
  ]);
  assert.deepEqual([...h.selectedDomains.value], before);
  assert.equal(h.domainDiagnosticComplete.value, true);
  assert.equal(h.liveDomainSet.value.has("dr.com"), true);
  assert.equal(h.liveDomainSet.value.has("mail.com"), false);
  assert.equal(h.ready.value, true);
});

test("bulk import saves credentials without checking each mail.com login", async (t) => {
  const h = await component(t);
  h.bulkText.value = "second@mail.com---fixture-password";
  await h.bulkImport();
  const importRequest = h.requests.find((request) => request.method === "POST");
  assert.equal(importRequest.payload.verify, false);
  assert.equal(h.requests.some((request) => request.url.startsWith("/api/mailcom/domains?")), false);
});

test("both rebind frontends submit the documented concurrency cap of 50", () => {
  const legacyApp = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const modernStore = readFileSync(new URL("../src/new/stores/rebind.ts", import.meta.url), "utf8");
  assert.match(
    legacyApp,
    /async function startRebind[\s\S]*?const concurrency = Math\.min\(50,/,
  );
  assert.match(
    modernStore,
    /async function start\(providerType[\s\S]*?concurrency: Math\.min\(50,/,
  );
});
