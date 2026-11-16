import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as vue from 'vue';
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc';
import { renderToString } from '@vue/server-renderer';
import ts from 'typescript';

// Real component code, synthetic credentials, fake clocks and a strict mocked API.
// No server, browser account, external service or payment request is involved.
const file = new URL('../src/components/ExternalApiView.vue', import.meta.url);
const source = readFileSync(file, 'utf8');
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
const names = `activeTab, visible, services, settings, settingsLoading, settingsSaving, settingsError, settingsNotice, secrets, clearSecrets,
  upiReady, scanReady, loadSettings, refreshSettings, saveSettings, parseSessions, newRequestId, safeHttpUrl, supportedScanLink, resultScanLink, qrImageUrl, localTime,
  upiText, usePromo, upiInput, upiConfirmation, upiBusy, upiUnknown, upiReconciled, upiReceipt, upiError, upiNotice,
  prepareUpi, cancelUpiConfirmation, confirmUpi, finishUpiReconciliation, credits, creditsBusy, creditsError, loadCredits,
  jobs, jobPager, jobBusy, pendingCancel, jobActionError, jobAction, canCancel, scanText, scanInput, scanConfig, configLoading, configError, loadScanConfig,
  quoteBusy, scanQuote, scanBusy, scanError, scanNotice, scanAttempt, scanPayload, scanMissing, scanLocked, quoteScan, confirmScan, retryCurrentScan, resendMissingScan,
  retrySubmission, retrying, retrySeconds, retryDeadlines, submissions, submissionPager, historyOpen, toggleHistory, orders, orderPager, balance, balanceError, loadBalance,
  fillScan, copyText, copyNotice, manualCopy, schedulePoll, selectTab, onVisibilityChange,
  upiDefaults, upiOptions, savedUpiOptions, upiSettingsDirty, upiPaymentReady, paymentPlatformLabel, paymentSubmitLabel, paymentStatusLabel,
  fuseText, fuseLabel, fuseDraft, fuseBusy, fuseUnknown, fuseResult, fuseError, fuseLocked, fuseInput, maskFuseTarget, prepareFuse, editFuse, confirmFuse`;
const transpiled = ts.transpileModule(`${script}\nexport { ${names} };`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { await vue.nextTick(); await new Promise((resolve) => setImmediate(resolve)); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const uuidPattern = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/;
const stripeLink = 'https://payments.stripe.com/upi/instructions/fixture';
const niceLink = 'https://pay.nicepay.co.kr/fixture';
const config = { available: true, unit_price: '1.2300', max_batch: 50, accepting_supplier_count: 3 };
const savedSettings = () => ({
  scan: { base_url: 'https://scan.fishmail.vip', has_api_key: true, api_key_masked: 'key…1234', has_bearer_token: false, bearer_token_masked: '' },
  upi: { base_url: 'https://upi.43-255-159-106.sslip.io', has_cdk: true, cdk_masked: 'cdk…1234', has_entry_proxy: true, entry_proxy_masked: 'entry…', has_exit_proxy: true, exit_proxy_masked: 'exit…' },
});
const job = (id = 'job-fixture', status = 'queued') => ({ id, batch_id: 'batch-fixture', position: 1, status, queue_position: 0, retryable: false });
const jobPage = (items = [], extra = {}) => ({ items, total: items.length, page: 1, page_size: 20, ...extra });
const orderPage = (items = [], extra = {}) => ({ items, pagination: { page: 1, page_size: 20, total: items.length, total_pages: 1, ...extra } });
const quote = (extra = {}) => ({ available: true, unit_price: '1.2300', count: 1, total_amount: '1.2300', max_batch: 50, ...extra });
const submission = (id, status = 'submitted', extra = {}) => ({ id, status, retryable: status === 'unknown', ...extra });
const httpError = (status, message = `HTTP ${status}`) => Object.assign(new Error(message), { status });
const paymentDefaults = { payment_platform: 'none', foarge_auth_mode: 'pbk', foarge_api_base: 'https://foarge.com/api/publisher/v1', masi_api_base: 'https://masi.cc.cd', masi_require_online_workers: true };
const paymentSettings = (platform = 'none', extra = {}) => ({ ...savedSettings(), upi: { ...savedSettings().upi, ...paymentDefaults, payment_platform: platform,
  has_foarge_credential: true, foarge_credential_masked: 'foa…123', has_masi_cdk_ticket: true, masi_cdk_ticket_masked: 'mas…123', has_seashore_cdk: true, seashore_cdk_masked: 'sea…123', ...extra } });
const fusion = (extra = {}) => ({ ok: true, duplicate: false, transferred_points: '7', target: { remaining_uses: 17, total_uses: 27, enabled: true }, ...extra });
const fuseCodes = ['keep-fixture-123', 'source-fixture-456', 'source-fixture-789'];

function harness(options = {}) {
  const exports = {}, requests = [], routes = [], mounted = [], cleanup = [], timers = new Map(), listeners = new Map();
  const failures = [], clipboardCalls = [], domCalls = [];
  const scope = vue.effectScope();
  let timerId = 0;
  const document = {
    hidden: false, activeElement: { focus: () => domCalls.push('restore-focus') },
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name),
    body: { appendChild: () => domCalls.push('append') },
    createElement: (name) => { assert.equal(name, 'textarea'); return { style: {}, focus: () => domCalls.push('focus'), select: () => domCalls.push('select'), remove: () => domCalls.push('remove') }; },
    execCommand: (command) => { domCalls.push(command); return options.execCopy !== false; },
  };
  const defaults = (path, opts) => {
    if (!opts.method && path === '/api/external/settings') return savedSettings();
    if (!opts.method && path.startsWith('/api/external/upi/jobs?')) return jobPage();
    if (!opts.method && path === '/api/external/scan/config') return { ...config };
    if (!opts.method && path.startsWith('/api/external/scan/orders?')) return orderPage();
    if (!opts.method && path.startsWith('/api/external/scan/submissions?')) return jobPage();
    throw new Error(`Unexpected mutation: ${opts.method} ${path}`);
  };
  const imports = {
    vue: { ...vue, useId: () => 'external-test', onMounted: (fn) => mounted.push(fn), onBeforeUnmount: (fn) => cleanup.push(fn) },
    '../api.js': { jsonBody: JSON.stringify, api: async (path, opts = {}) => {
      requests.push({ path, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : undefined, options: opts });
      const index = routes.findIndex((route) => (typeof route.path === 'string' ? route.path === path : route.path.test(path)) && (route.method || 'GET') === (opts.method || 'GET'));
      if (index < 0) {
        try { return defaults(path, opts); } catch (error) { failures.push(error.message); throw error; }
      }
      const route = routes.splice(index, 1)[0];
      if (route.result instanceof Error) throw route.result;
      return typeof route.result === 'function' ? route.result(path, opts) : route.result;
    } },
  };
  scope.run(() => runInNewContext(transpiled, {
    exports, require: (id) => { assert.ok(id in imports, `Unexpected dependency: ${id}`); return imports[id]; },
    URL, URLSearchParams, AbortController, Error, Uint8Array,
    crypto: { getRandomValues: (bytes) => webcrypto.getRandomValues(bytes) }, // Deliberately no randomUUID.
    navigator: options.clipboard === false ? {} : { clipboard: { writeText: async (text) => { if (options.clipboardError) throw new Error('denied'); clipboardCalls.push(text); } } },
    document,
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
    fetch: () => assert.fail('all requests must use the shared API'),
    localStorage: { setItem: () => assert.fail('no secret persistence'), getItem: () => assert.fail('no store dependency') },
    console: { log: () => assert.fail('no token logging'), error: () => assert.fail('no token logging') },
  }));
  if (options.configured !== false) exports.settings.value = savedSettings();
  let disposed = false;
  return {
    ...exports, requests, routes, timers, document, listeners, clipboardCalls, domCalls,
    respond: (path, result, method = 'GET') => routes.push({ path, result, method }),
    mount: async () => { await Promise.all(mounted.map((fn) => fn())); await flush(); },
    tick: async () => { const scheduled = [...timers.entries()]; for (const [id, { fn }] of scheduled) { timers.delete(id); await fn(); } await flush(); },
    hide: async (hidden = true) => { document.hidden = hidden; await listeners.get('visibilitychange')?.(); await flush(); },
    dispose: () => { if (disposed) return; disposed = true; cleanup.forEach((fn) => fn()); scope.stop(); assert.deepEqual(failures, []); },
  };
}
function useHarness(t, options) { const h = harness(options); t.after(() => h.dispose()); return h; }
async function readyScan(h) { await h.selectTab('scan'); h.scanText.value = stripeLink; }
async function makeQuote(h, value = quote()) { h.respond('/api/external/scan/quote', value, 'POST'); await h.quoteScan(); }
const mutations = (h, path) => h.requests.filter((request) => request.method === 'POST' && (!path || request.path === path));
async function loadPaymentSettings(h, platform, extra) { h.respond('/api/external/settings', paymentSettings(platform, extra)); await h.loadSettings(); }
function prepareFusion(h, codes = fuseCodes, label = '融合标签') { h.fuseText.value = codes.join('\n'); h.fuseLabel.value = label; h.prepareFuse(); }
let renderedComponent;
async function renderView(configure = () => {}) {
  if (!renderedComponent) {
    const { descriptor, errors } = parse(source, { filename: file.pathname });
    assert.deepEqual(errors, []);
    const compiled = compileScript(descriptor, { id: 'external-api-behavior' });
    const template = compileTemplate({ source: descriptor.template.content, filename: file.pathname, id: 'external-api-behavior', compilerOptions: { bindingMetadata: compiled.bindings } });
    assert.deepEqual(template.errors, []);
    const code = ts.transpileModule(`${compiled.content}\n${template.code}`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
    const exports = {};
    runInNewContext(code, { exports, require: (id) => id === 'vue' ? vue : { api: () => assert.fail('SSR must not call services'), jsonBody: JSON.stringify },
      URL, URLSearchParams, AbortController, Error, Uint8Array, crypto: webcrypto, setTimeout, clearTimeout });
    renderedComponent = { ...exports.default, render: exports.render };
  }
  const component = { ...renderedComponent, setup(props, ctx) {
    const state = renderedComponent.setup(props, ctx);
    configure(state);
    return state;
  } };
  return renderToString(vue.createSSRApp(component));
}

test('sessions accept single/array/line JSON and deduplicate by the actual token', (t) => {
  const h = useHarness(t);
  for (const text of ['one\none\n{"accessToken":"two"}', '["one", {"access_token":"two"}, {"accessToken":"one"}]']) {
    assert.deepEqual(plain(h.parseSessions(text)), { sessions_text: 'one\ntwo', count: 2, duplicates: 1, error: '' });
  }
  assert.equal(h.parseSessions('{\n  "accessToken": "one",\n  "user": {"email":"fixture@example.test"}\n}').sessions_text, 'one');
  assert.equal(h.parseSessions('{"accessToken":"one"}\n{"access_token":"two"}').count, 2);
  assert.equal(h.parseSessions(Array.from({ length: 100 }, (_, index) => `token-${index}`).join('\n')).count, 100);
  assert.match(h.parseSessions(Array.from({ length: 101 }, (_, index) => `token-${index}`).join('\n')).error, /1–100/);
  for (const text of ['[]', '{}', '{"accessToken":123}', '{"accessToken":{}}', 'contains whitespace', '[invalid', '{"accessToken":']) assert.ok(h.parseSessions(text).error, text);
  assert.equal(h.parseSessions('').count, 0);
});

test('UUIDs work without randomUUID and link handling rejects executable and lookalike URLs', (t) => {
  const h = useHarness(t);
  const ids = new Set(Array.from({ length: 100 }, () => h.newRequestId()));
  assert.equal(ids.size, 100);
  for (const id of ids) assert.match(id, uuidPattern);
  for (const link of [stripeLink, 'https://www.pay.nicepay.co.kr/fixture', niceLink]) assert.ok(h.supportedScanLink(link));
  for (const link of ['javascript:alert(1)', 'data:text/html,payload', '//example.test', 'https://payments.stripe.com.evil.test/upi/instructions/x', 'https://pay.nicepay.co.kr.evil.test/x', 'https://user:pass@pay.nicepay.co.kr/x', 'https://checkout.stripe.com/c/pay/fixture', 'https://payments.stripe.com/upi/instructions/', 'https://pay.nicepay.co.kr:9999/x', 'https://pay.nicepay.co.kr/', 'https://pay.nicepay.io/fixture']) assert.equal(h.supportedScanLink(link), '');
  assert.equal(h.safeHttpUrl('http://example.test/link'), 'http://example.test/link');
});

test('settings send only changed secrets and explicit clears, then erase saved inputs', async (t) => {
  const h = useHarness(t);
  h.secrets.cdk = '  synthetic-cdk  ';
  h.clearSecrets.exit_proxy = true;
  h.respond('/api/external/settings', { ok: true }, 'PUT');
  await h.saveSettings('upi');
  const put = h.requests.find((request) => request.method === 'PUT');
  assert.deepEqual(put.body, { upi: { cdk: 'synthetic-cdk', clear_exit_proxy: true, ...paymentDefaults } });
  assert.equal(h.secrets.cdk, '');
  assert.equal(h.clearSecrets.exit_proxy, false);
  assert.match(h.settingsNotice.value, /输入已清空/);
  const count = h.requests.length;
  await h.saveSettings('upi');
  assert.equal(h.requests.length, count);
  h.secrets.api_key = 'new-fixture-key'; h.secrets.bearer_token = 'ignored'; h.clearSecrets.bearer_token = true;
  h.respond('/api/external/settings', { ok: true }, 'PUT');
  await h.saveSettings('scan');
  assert.deepEqual(h.requests.filter((request) => request.method === 'PUT')[1].body, { scan: { api_key: 'new-fixture-key', clear_bearer_token: true } });
  assert.equal(h.secrets.bearer_token, '');
});

test('configuration errors preserve editable inputs, clear secrets after a successful PUT even if the following GET fails', async (t) => {
  const h = useHarness(t);
  h.secrets.cdk = 'fixture-secret';
  h.respond('/api/external/settings', new Error('save offline'), 'PUT');
  await h.saveSettings('upi');
  assert.equal(h.secrets.cdk, 'fixture-secret');
  assert.match(h.settingsError.value, /save offline/);
  h.respond('/api/external/settings', { ok: true }, 'PUT');
  h.respond('/api/external/settings', new Error('read offline'));
  await h.saveSettings('upi');
  assert.equal(h.secrets.cdk, '');
  assert.equal(h.upiReady.value, false);
  assert.match(h.settingsError.value, /read offline/);
});

test('CDK and optional Bearer gate manual balance queries; POST bodies contain no secrets', async (t) => {
  const h = useHarness(t);
  await h.loadBalance();
  assert.equal(h.requests.length, 0);
  h.settings.value.scan.has_bearer_token = true;
  h.respond('/api/external/scan/balance', { balance: '10.00', frozen_balance: '2.00', unit_price: '1.00' }, 'POST');
  await h.loadBalance();
  assert.equal(h.balance.value.balance, '10.00');
  assert.deepEqual(mutations(h)[0].body, {});
  h.respond('/api/external/upi/credits', { data: { credits: 17 } }, 'POST');
  await h.loadCredits();
  assert.deepEqual(plain(h.credits.value), { credits: 17 });
  h.settings.value.upi.has_cdk = false;
  await h.loadCredits();
  assert.equal(mutations(h).length, 2);
});

test('UPI requires saved proxies and explicit cost confirmation, snapshots deduplicated input, and blocks double click', async (t) => {
  const h = useHarness(t);
  h.upiText.value = 'one\n{"accessToken":"one"}\n{"accessToken":"two"}';
  h.settings.value.upi.has_exit_proxy = false;
  h.prepareUpi();
  assert.equal(h.upiConfirmation.value, null);
  h.settings.value.upi.has_exit_proxy = true;
  h.usePromo.value = true;
  h.prepareUpi();
  assert.equal(mutations(h).length, 0);
  assert.equal(h.upiConfirmation.value.count, 2);
  const id = h.upiConfirmation.value.request_id;
  const response = deferred();
  h.respond('/api/external/upi/jobs', response.promise, 'POST');
  const first = h.confirmUpi();
  await h.confirmUpi(); h.prepareUpi(); h.cancelUpiConfirmation();
  assert.equal(mutations(h).length, 1);
  assert.deepEqual(mutations(h)[0].body, { request_id: id, sessions_text: 'one\ntwo', use_promo: true, payment_platform: 'none' });
  response.resolve({ ok: true, batch_id: 'accepted-batch', items: [job()] });
  await first;
  assert.equal(h.upiText.value, '');
  assert.equal(h.upiConfirmation.value, null);
  assert.equal(h.upiReceipt.value.request_id, id);
  assert.equal(h.upiReceipt.value.batch_id, 'accepted-batch');
});

test('UPI confirmation cancellation never submits and network ambiguity is never retried', async (t) => {
  const h = useHarness(t);
  await h.mount();
  h.upiText.value = 'synthetic-token';
  h.prepareUpi(); h.cancelUpiConfirmation(); await h.confirmUpi();
  assert.equal(mutations(h).length, 0);
  h.prepareUpi(); const id = h.upiConfirmation.value.request_id;
  h.respond('/api/external/upi/jobs', new Error('timeout'), 'POST');
  await h.confirmUpi();
  assert.equal(h.upiUnknown.value, true);
  assert.match(h.upiError.value, /不要重新提交/);
  assert.equal(h.upiReceipt.value.request_id, id);
  await h.confirmUpi(); h.prepareUpi(); await h.tick();
  assert.equal(mutations(h).length, 1);
  h.finishUpiReconciliation(); assert.equal(h.upiUnknown.value, true);
  h.upiReconciled.value = true; h.finishUpiReconciliation();
  assert.equal(h.upiUnknown.value, false);
  assert.equal(h.upiText.value, '');
});

test('UPI polls only current nonterminal page, pauses on hidden/tab switches and stops at completion', async (t) => {
  const h = useHarness(t);
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('page-one')], { total: 25 }));
  await h.mount();
  assert.deepEqual([...h.timers.values()].map((timer) => timer.delay), [10000]);
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('page-two', 'running')], { total: 25, page: 2 }));
  await h.jobPager.go(2);
  assert.match(h.requests.at(-1).path, /page=2&page_size=20&refresh=true/);
  const pending = deferred();
  h.respond(/^\/api\/external\/upi\/jobs\?/, pending.promise);
  await h.tick();
  const inFlight = h.requests.at(-1);
  assert.match(inFlight.path, /page=2/);
  assert.equal(h.jobs.loading, true);
  const count = h.requests.length;
  await h.jobPager.load(true); await h.tick();
  assert.equal(h.requests.length, count);
  await h.hide();
  assert.equal(inFlight.options.signal.aborted, true);
  assert.equal(h.timers.size, 0);
  pending.resolve(jobPage([job('stale', 'done')])); await flush();
  assert.equal(h.jobs.items[0].id, 'page-two');
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('page-two', 'running')], { total: 25, page: 2 }));
  await h.hide(false);
  assert.equal(h.timers.size, 1);
  await h.selectTab('scan'); assert.equal(h.timers.size, 0);
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('finished', 'done'), job('bad', 'failed'), job('stopped', 'cancelled'), job('uncertain', 'unknown')], { total: 25 }));
  await h.selectTab('upi');
  assert.equal(h.timers.size, 0);
  assert.equal(h.canCancel(job('uncertain', 'unknown')), false);
  assert.equal(mutations(h).length, 0);
});

test('job cancellation waits for an inline confirmation and row refresh never creates a job', async (t) => {
  const h = useHarness(t);
  h.jobs.items = [job()];
  await h.jobAction(h.jobs.items[0], 'cancel');
  assert.equal(mutations(h).length, 0);
  h.pendingCancel.value = 'job-fixture';
  const response = deferred(); h.respond('/api/external/upi/jobs/job-fixture/cancel', response.promise, 'POST');
  const first = h.jobAction(h.jobs.items[0], 'cancel');
  await h.jobAction(h.jobs.items[0], 'cancel');
  response.resolve(job('job-fixture', 'cancelling')); await first;
  assert.equal(h.canCancel(h.jobs.items[0]), false);
  h.respond('/api/external/upi/jobs/job-fixture/refresh', job('job-fixture', 'cancelled'), 'POST');
  await h.jobAction(h.jobs.items[0], 'refresh');
  assert.equal(h.jobs.items[0].status, 'cancelled');
  assert.deepEqual(mutations(h).map((request) => request.body), [{}, {}]);
  assert.equal(mutations(h, '/api/external/upi/jobs').length, 0);
});

test('fill into scan changes the editor and tab, without quote/order/automatic-payment POSTs', async (t) => {
  const h = useHarness(t);
  await h.fillScan(stripeLink);
  assert.equal(h.activeTab.value, 'scan');
  assert.equal(h.scanText.value, stripeLink);
  assert.equal(h.scanQuote.value, null);
  assert.equal(mutations(h).length, 0);
  await h.fillScan('javascript:alert(1)');
  assert.equal(h.scanText.value, stripeLink);
});

test('scan does no service queries without a saved key, and unavailable config blocks quote/order', async (t) => {
  const h = useHarness(t);
  h.settings.value.scan.has_api_key = false;
  await h.selectTab('scan'); await h.loadScanConfig(); await h.orderPager.load(); await h.submissionPager.load();
  assert.equal(h.requests.length, 0);
  h.settings.value.scan.has_api_key = true;
  h.respond('/api/external/scan/config', { ...config, available: false });
  await h.loadScanConfig();
  h.scanText.value = niceLink;
  await h.quoteScan(); await h.confirmScan();
  assert.equal(mutations(h).length, 0);
});

test('scan quotes deduplicated links and uses returned string price only after explicit confirmation', async (t) => {
  const h = useHarness(t);
  await readyScan(h); h.scanText.value += `\n${stripeLink}`;
  await makeQuote(h, quote({ unit_price: '2.000000', total_amount: '2.000000' }));
  assert.deepEqual(mutations(h)[0].body, { links: [stripeLink] });
  assert.equal(mutations(h, '/api/external/scan/orders').length, 0);
  const response = deferred(); h.respond('/api/external/scan/orders', response.promise, 'POST');
  const first = h.confirmScan(); await h.confirmScan();
  const posts = mutations(h, '/api/external/scan/orders');
  assert.equal(posts.length, 1); assert.match(posts[0].body.request_id, uuidPattern);
  assert.deepEqual(posts[0].body.links, [stripeLink]);
  assert.equal(posts[0].body.unit_price, '2.000000');
  response.resolve({ ok: true, submission: submission(posts[0].body.request_id, 'submitted', { data: { batch_no: 'scan-batch', order_count: 1, total_amount: '2.000000' } }) });
  await first;
  assert.equal(h.scanText.value, ''); assert.equal(h.scanLocked.value, false);
  assert.equal(h.historyOpen.value, true);
});

test('edits invalidate a quote and late quote responses never confirm a different input', async (t) => {
  const h = useHarness(t);
  await readyScan(h); await makeQuote(h);
  h.scanText.value = niceLink;
  assert.equal(h.scanQuote.value, null);
  await h.confirmScan();
  const pending = deferred(); h.respond('/api/external/scan/quote', pending.promise, 'POST');
  const first = h.quoteScan();
  h.scanText.value = stripeLink;
  pending.resolve(quote()); await first;
  assert.equal(h.scanQuote.value, null);
  assert.equal(mutations(h, '/api/external/scan/orders').length, 0);
});

test('unavailable, mismatched and malformed quotes remain non-submittable', async (t) => {
  const h = useHarness(t);
  await readyScan(h);
  for (const value of [quote({ count: 2 }), quote({ unit_price: 1 }), quote({ max_batch: 0 }), quote({ total_amount: 'NaN' })]) {
    await makeQuote(h, value); assert.equal(h.scanQuote.value, null);
  }
  await makeQuote(h, quote({ available: false }));
  assert.equal(h.scanQuote.value, null); assert.equal(h.scanConfig.value.available, false);
  await h.confirmScan(); assert.equal(mutations(h, '/api/external/scan/orders').length, 0);
});

test('scan uncertainty calls the dedicated retry endpoint without replaying create or an edited draft', async (t) => {
  const h = useHarness(t);
  await h.mount(); await readyScan(h); await makeQuote(h);
  h.respond('/api/external/scan/orders', new Error('network timeout'), 'POST');
  await h.confirmScan();
  const original = mutations(h, '/api/external/scan/orders')[0].body;
  assert.equal(h.scanAttempt.value.status, 'unknown'); assert.equal(h.scanLocked.value, true);
  assert.equal(Object.isFrozen(h.scanPayload.value), true);
  assert.equal(Object.isFrozen(h.scanPayload.value.links), true);
  await h.tick(); await h.confirmScan(); await h.quoteScan();
  assert.equal(mutations(h, '/api/external/scan/orders').length, 1);
  h.scanText.value = niceLink; // Even an out-of-band edit must never alter the retry payload.
  h.respond(`/api/external/scan/submissions/${original.request_id}/retry`, { ok: true, submission: submission(original.request_id, 'submitted', { data: { idempotent: true } }) }, 'POST');
  await h.retryCurrentScan();
  assert.equal(mutations(h, '/api/external/scan/orders').length, 1);
  assert.deepEqual(mutations(h).at(-1).body, {});
  assert.equal(h.scanAttempt.value.status, 'submitted');
  assert.equal(h.scanPayload.value, null);
});

test('only explicit retry 404 enables a second user action to resubmit the identical frozen create payload', async (t) => {
  const h = useHarness(t);
  await readyScan(h); await makeQuote(h);
  h.respond('/api/external/scan/orders', httpError(502), 'POST');
  await h.confirmScan();
  const original = mutations(h, '/api/external/scan/orders')[0].body;
  await h.resendMissingScan();
  assert.equal(mutations(h, '/api/external/scan/orders').length, 1);
  h.respond(`/api/external/scan/submissions/${original.request_id}/retry`, httpError(503), 'POST');
  await h.retryCurrentScan(); await h.resendMissingScan();
  assert.equal(h.scanMissing.value, false);
  assert.equal(mutations(h, '/api/external/scan/orders').length, 1);
  h.respond(`/api/external/scan/submissions/${original.request_id}/retry`, httpError(404), 'POST');
  await h.retryCurrentScan();
  assert.equal(h.scanMissing.value, true);
  assert.equal(mutations(h, '/api/external/scan/orders').length, 1);
  h.scanText.value = niceLink;
  h.respond('/api/external/scan/orders', { ok: true, submission: submission(original.request_id) }, 'POST');
  await h.resendMissingScan();
  assert.deepEqual(mutations(h, '/api/external/scan/orders')[1].body, original);
  assert.equal(h.scanMissing.value, false);
});

test('explicit validation/billing 4xx failures unlock both editors; network and 5xx retain uncertainty', async (t) => {
  const h = useHarness(t);
  await readyScan(h);
  for (const status of [400, 402, 403, 409, 413, 422]) {
    h.scanText.value = stripeLink; await makeQuote(h);
    h.respond('/api/external/scan/orders', httpError(status), 'POST');
    await h.confirmScan();
    assert.equal(h.scanAttempt.value.status, 'failed', `scan ${status}`);
    assert.equal(h.scanLocked.value, false);
    assert.equal(h.scanPayload.value, null);
    assert.equal(h.scanText.value, stripeLink);
    h.upiText.value = 'synthetic-token'; h.prepareUpi();
    h.respond('/api/external/upi/jobs', httpError(status), 'POST');
    await h.confirmUpi();
    assert.equal(h.upiUnknown.value, false, `upi ${status}`);
    assert.equal(h.upiText.value, 'synthetic-token');
    assert.match(h.upiError.value, /校验失败/);
  }
  h.prepareUpi(); h.respond('/api/external/upi/jobs', httpError(502), 'POST');
  await h.confirmUpi(); assert.equal(h.upiUnknown.value, true);
});

test('timestamps use seconds and scan transfer prefers supported provider links over shortlinks; QR only uses image sources', (t) => {
  const h = useHarness(t);
  const timestamp = 1789000000.5;
  const expected = new Date(timestamp * 1000).toLocaleString('zh-CN', { hour12: false });
  assert.equal(h.localTime(timestamp), expected);
  assert.equal(h.localTime(String(timestamp)), expected);
  assert.equal(h.localTime(new Date(timestamp * 1000).toISOString()), expected);
  assert.equal(h.localTime('bad timestamp'), '—');
  assert.equal(h.resultScanLink({ payment_link: 'https://short.example.test/a', provider_redirect_url: stripeLink, verification_url: niceLink }), stripeLink);
  assert.equal(h.resultScanLink({ scan_link: niceLink, provider_redirect_url: stripeLink }), niceLink);
  assert.equal(h.resultScanLink({ scan_link: 'javascript:alert(1)', verification_url: stripeLink }), stripeLink);
  assert.equal(h.resultScanLink({ payment_link: 'https://short.example.test/a' }), '');
  for (const value of ['data:image/png;base64,aW1hZ2U=', 'data:image/svg+xml;base64,PHN2Zy8+', 'data:image/svg+xml,%3Csvg%2F%3E', 'https://example.test/qr.png']) assert.equal(h.qrImageUrl(value), value);
  for (const value of ['javascript:alert(1)', 'data:text/html;base64,PHN2Zy8+', '<svg onload="alert(1)"/>', 'data:image/png;base64,<script/>']) assert.equal(h.qrImageUrl(value), '');
});

test('scan 429 preserves the intent and enables only a manual same-ID retry after the delay', async (t) => {
  const h = useHarness(t);
  await h.mount(); await readyScan(h); await makeQuote(h);
  h.respond('/api/external/scan/orders', httpError(429, '平台限流，请 30 秒后再试'), 'POST');
  await h.confirmScan();
  const original = mutations(h, '/api/external/scan/orders')[0].body;
  assert.equal(h.scanAttempt.value.status, 'unknown');
  assert.equal(h.retrySeconds(original.request_id), 30);
  const count = mutations(h).length;
  await h.retryCurrentScan(); await h.tick();
  assert.equal(mutations(h).length, count);
  assert.deepEqual(plain(h.scanPayload.value), original);
  await h.hide(); assert.equal(h.timers.size, 0);
  h.retryDeadlines[original.request_id] = Date.now() - 1;
  await h.hide(false);
  assert.equal(h.retrySeconds(original.request_id), 0);
  assert.equal(mutations(h).length, count);
  h.respond(`/api/external/scan/submissions/${original.request_id}/retry`, { ok: true, submission: submission(original.request_id) }, 'POST');
  await h.retryCurrentScan();
  assert.equal(h.scanAttempt.value.status, 'submitted');
  assert.equal(mutations(h, '/api/external/scan/orders').length, 1);
});

test('business failure allows correction and receives a new request ID instead of retrying the old submission', async (t) => {
  const h = useHarness(t);
  await readyScan(h); await makeQuote(h);
  h.respond('/api/external/scan/orders', (_path, opts) => ({ ok: true, submission: submission(JSON.parse(opts.body).request_id, 'failed', { error: 'invalid business input', retryable: true }) }), 'POST');
  await h.confirmScan();
  const first = mutations(h, '/api/external/scan/orders')[0].body.request_id;
  assert.equal(h.scanLocked.value, false); await h.retryCurrentScan();
  h.scanText.value = niceLink; await makeQuote(h);
  h.respond('/api/external/scan/orders', (_path, opts) => ({ ok: true, submission: submission(JSON.parse(opts.body).request_id) }), 'POST');
  await h.confirmScan();
  const second = mutations(h, '/api/external/scan/orders')[1].body;
  assert.notEqual(second.request_id, first); assert.deepEqual(second.links, [niceLink]);
});

test('refreshed submission history retries the persisted ID without sending the edited draft', async (t) => {
  const h = useHarness(t);
  await readyScan(h);
  h.respond(/^\/api\/external\/scan\/submissions\?/, jobPage([submission('old-request-id', 'unknown')], { total: 21 }));
  await h.toggleHistory({ target: { open: true } });
  h.scanText.value = niceLink;
  h.respond('/api/external/scan/submissions/old-request-id/retry', { ok: true, submission: submission('old-request-id') }, 'POST');
  await h.retrySubmission(h.submissions.items[0]);
  assert.deepEqual(mutations(h).map(({ path, body }) => ({ path, body })), [{ path: '/api/external/scan/submissions/old-request-id/retry', body: {} }]);
  await h.retrySubmission(submission('business-failure', 'failed', { retryable: true }));
  await h.retrySubmission(submission('not-retryable', 'unknown', { retryable: false }));
  assert.equal(mutations(h).length, 1);
});

test('scan polls selected page at 15 seconds, stops on completion or missing key, and history paginates independently', async (t) => {
  const h = useHarness(t);
  await h.mount();
  h.respond(/^\/api\/external\/scan\/orders\?/, orderPage([{ id: 'first', status: 'pending' }], { total: 25, total_pages: 2 }));
  await readyScan(h);
  assert.deepEqual([...h.timers.values()].map((timer) => timer.delay), [15000]);
  h.respond(/^\/api\/external\/scan\/orders\?/, orderPage([{ id: 'second', status: 'processing' }], { page: 2, total: 25, total_pages: 2 }));
  await h.orderPager.go(2);
  assert.match(h.requests.at(-1).path, /scan\/orders\?page=2&page_size=20$/);
  h.respond(/^\/api\/external\/scan\/orders\?/, orderPage([{ id: 'second', status: 'completed' }], { page: 2, total: 25, total_pages: 2 }));
  await h.tick(); assert.equal(h.timers.size, 0);
  h.orders.items = [{ id: 'again', status: 'claimed' }]; assert.equal(h.timers.size, 1);
  h.settings.value.scan.has_api_key = false; await h.tick();
  assert.equal(h.timers.size, 0);
  h.settings.value.scan.has_api_key = true;
  h.respond(/^\/api\/external\/scan\/submissions\?/, jobPage([submission('history-one')], { total: 21 }));
  await h.submissionPager.load();
  h.respond(/^\/api\/external\/scan\/submissions\?/, jobPage([submission('history-two')], { total: 21, page: 2 }));
  await h.submissionPager.go(2);
  assert.match(h.requests.at(-1).path, /submissions\?page=2&page_size=20$/);
  assert.equal(h.orders.page, 2);
});

test('poll errors pause automatic requests and a manual refresh recovers an out-of-range page', async (t) => {
  const h = useHarness(t);
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job()], { total: 25 }));
  await h.mount();
  h.respond(/^\/api\/external\/upi\/jobs\?/, new Error('list offline'));
  await h.tick();
  assert.match(h.jobs.error, /list offline/); assert.equal(h.timers.size, 0);
  h.jobs.page = 3;
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([], { total: 1, page: 3 }));
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('survivor', 'done')]));
  await h.jobPager.load(true);
  assert.equal(h.jobs.page, 1); assert.equal(h.jobs.items[0].id, 'survivor'); assert.equal(h.jobs.error, '');
});

test('unmount aborts GETs, clears clocks, removes visibility listener and ignores late replies', async (t) => {
  const h = useHarness(t);
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job()]));
  await h.mount();
  const pending = deferred(); h.respond(/^\/api\/external\/upi\/jobs\?/, pending.promise);
  const load = h.jobPager.load(true);
  const request = h.requests.at(-1);
  h.upiText.value = 'ephemeral-token'; h.secrets.cdk = 'ephemeral-cdk';
  h.dispose();
  assert.equal(request.options.signal.aborted, true); assert.equal(h.timers.size, 0); assert.equal(h.listeners.size, 0);
  assert.equal(h.upiText.value, ''); assert.equal(h.secrets.cdk, '');
  pending.resolve(jobPage([job('late')])); await load;
  assert.equal(h.jobs.items[0].id, 'job-fixture');
});

test('HTTP clipboard fallback restores focus, and denied copy offers selectable text', async (t) => {
  const h = useHarness(t, { clipboard: false });
  await h.copyText(stripeLink);
  assert.deepEqual(h.domCalls, ['append', 'focus', 'select', 'copy', 'remove', 'restore-focus']);
  assert.equal(h.copyNotice.value, '已复制。');
  const denied = useHarness(t, { clipboardError: true, execCopy: false });
  await denied.copyText(niceLink);
  assert.equal(denied.manualCopy.value, niceLink);
  assert.match(denied.copyNotice.value, /选中文本复制/);
  const clipboard = useHarness(t);
  await clipboard.copyText(stripeLink);
  assert.deepEqual(clipboard.clipboardCalls, [stripeLink]); assert.equal(clipboard.domCalls.length, 0);
});

test('old settings default to link-only and promo starts enabled without overriding a user toggle', async (t) => {
  const h = useHarness(t);
  await h.loadSettings();
  assert.deepEqual(plain(h.upiOptions), paymentDefaults);
  assert.equal(h.upiSettingsDirty.value, false);
  assert.equal(h.upiPaymentReady.value, true);
  assert.equal(h.usePromo.value, true);
  h.usePromo.value = false;
  await h.refreshSettings();
  h.upiText.value = 'one'; h.prepareUpi();
  assert.equal(h.upiConfirmation.value.use_promo, false);
  assert.equal(h.upiConfirmation.value.payment_platform, 'none');
});

test('each selected payment platform saves its options and new credential without clearing the others', async (t) => {
  for (const [platform, key, changes] of [
    ['foarge', 'foarge_credential', { foarge_auth_mode: 'api_key', foarge_api_base: 'https://foarge.example.test/api' }],
    ['masi', 'masi_cdk_ticket', { masi_api_base: 'https://masi.example.test/api', masi_require_online_workers: false }],
    ['seashore', 'seashore_cdk', {}],
  ]) {
    const h = useHarness(t);
    await loadPaymentSettings(h, 'none');
    Object.assign(h.upiOptions, changes, { payment_platform: platform }); h.secrets[key] = '  new-fixture-credential  ';
    h.respond('/api/external/settings', { ok: true }, 'PUT');
    h.respond('/api/external/settings', paymentSettings(platform, changes));
    await h.saveSettings('upi');
    assert.deepEqual(h.requests.find((request) => request.method === 'PUT').body, { upi: { ...paymentDefaults, ...changes, payment_platform: platform, [key]: 'new-fixture-credential' } });
    assert.equal(h.secrets[key], ''); assert.equal(h.upiSettingsDirty.value, false);
    assert.equal(h.settings.value.upi.has_foarge_credential, true);
    assert.equal(h.settings.value.upi.has_masi_cdk_ticket, true);
    assert.equal(h.settings.value.upi.has_seashore_cdk, true);
  }
});

test('Foarge accepts all three documented auth modes and explicit clears win over entered secrets', async (t) => {
  const h = useHarness(t);
  for (const mode of ['pbk', 'api_key', 'cdk_session']) {
    h.upiOptions.payment_platform = 'foarge'; h.upiOptions.foarge_auth_mode = mode;
    h.secrets.foarge_credential = 'fixture-credential';
    h.respond('/api/external/settings', { ok: true }, 'PUT');
    h.respond('/api/external/settings', paymentSettings('foarge', { foarge_auth_mode: mode }));
    await h.saveSettings('upi');
    assert.equal(h.requests.filter((request) => request.method === 'PUT').at(-1).body.upi.foarge_auth_mode, mode);
  }
  h.upiOptions.payment_platform = 'none';
  h.secrets.foarge_credential = 'discard-this-input'; h.clearSecrets.foarge_credential = true;
  h.clearSecrets.masi_cdk_ticket = true; h.clearSecrets.seashore_cdk = true;
  h.respond('/api/external/settings', { ok: true }, 'PUT');
  await h.saveSettings('upi');
  assert.deepEqual(h.requests.filter((request) => request.method === 'PUT').at(-1).body.upi, {
    ...paymentDefaults, foarge_auth_mode: 'cdk_session', clear_foarge_credential: true, clear_masi_cdk_ticket: true, clear_seashore_cdk: true,
  });
  assert.equal(h.secrets.foarge_credential, ''); assert.equal(h.clearSecrets.foarge_credential, false);
});

test('platform-only settings edits persist and failed saves retain the draft and block creation', async (t) => {
  const h = useHarness(t);
  await loadPaymentSettings(h, 'foarge');
  h.upiOptions.payment_platform = 'none';
  h.respond('/api/external/settings', new Error('offline'), 'PUT');
  await h.saveSettings('upi');
  assert.equal(h.upiOptions.payment_platform, 'none'); assert.equal(h.upiSettingsDirty.value, true);
  h.upiText.value = 'one'; h.prepareUpi();
  assert.equal(h.upiConfirmation.value, null); assert.match(h.upiError.value, /先保存/);
  h.respond('/api/external/settings', { ok: true }, 'PUT');
  h.respond('/api/external/settings', paymentSettings('none'));
  await h.saveSettings('upi');
  assert.deepEqual(h.requests.filter((request) => request.method === 'PUT').at(-1).body, { upi: paymentDefaults });
  h.prepareUpi(); assert.equal(h.upiConfirmation.value.payment_platform, 'none');
});

test('unsaved options and secrets block creation and refreshing settings preserves the pending choice', async (t) => {
  const h = useHarness(t);
  await loadPaymentSettings(h, 'foarge'); h.upiText.value = 'one';
  h.upiOptions.payment_platform = 'masi'; h.prepareUpi();
  assert.equal(h.upiConfirmation.value, null); assert.match(h.upiError.value, /先保存/);
  h.respond('/api/external/settings', paymentSettings('foarge')); await h.refreshSettings();
  assert.equal(h.upiOptions.payment_platform, 'masi'); assert.equal(h.upiSettingsDirty.value, true);
  h.upiOptions.payment_platform = 'foarge'; h.secrets.foarge_credential = 'unsaved-credential'; h.prepareUpi();
  assert.equal(h.upiConfirmation.value, null);
  h.secrets.foarge_credential = ''; h.prepareUpi();
  assert.equal(h.upiConfirmation.value.payment_platform, 'foarge'); assert.equal(Object.isFrozen(h.upiConfirmation.value), true);
  h.upiOptions.foarge_auth_mode = 'cdk_session'; await h.confirmUpi();
  assert.equal(mutations(h).length, 0); assert.match(h.upiError.value, /重新核对/);
  h.upiOptions.foarge_auth_mode = 'pbk';
  h.settings.value.upi.payment_platform = 'masi'; h.upiOptions.payment_platform = 'masi';
  await h.confirmUpi(); assert.equal(mutations(h).length, 0);
});

test('automatic payment requires the saved credential of the selected platform', async (t) => {
  for (const [platform, key] of [['foarge', 'foarge_credential'], ['masi', 'masi_cdk_ticket'], ['seashore', 'seashore_cdk']]) {
    const h = useHarness(t); await loadPaymentSettings(h, platform, { [`has_${key}`]: false });
    h.upiText.value = 'one'; h.prepareUpi();
    assert.equal(h.upiPaymentReady.value, false); assert.equal(h.upiConfirmation.value, null);
    assert.match(h.upiError.value, /支付平台的凭证/);
    h.settings.value.upi[`has_${key}`] = true; h.prepareUpi();
    assert.equal(h.upiConfirmation.value.payment_platform, platform);
  }
});

test('single and batch creates send one backend request with the frozen platform and preserve partial batch results', async (t) => {
  for (const platform of ['none', 'foarge', 'masi', 'seashore']) {
    const h = useHarness(t); await loadPaymentSettings(h, platform);
    h.upiText.value = platform === 'none' ? 'one' : 'one\ntwo\none';
    h.usePromo.value = false; h.prepareUpi(); const draft = plain(h.upiConfirmation.value);
    h.usePromo.value = true; h.upiText.value = 'out-of-band-edit';
    h.respond('/api/external/upi/jobs', { ok: true, batch_id: 'remote-batch' }, 'POST');
    const items = [{ ...job('accepted', 'running'), payment_platform: platform }, { ...job('rejected', 'failed'), error: 'rejected input', payment_platform: platform }];
    h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage(items));
    await h.confirmUpi();
    assert.deepEqual(mutations(h).map(({ path, body }) => ({ path, body })), [{ path: '/api/external/upi/jobs', body: {
      request_id: draft.request_id, sessions_text: draft.sessions_text, use_promo: false, payment_platform: platform,
    } }]);
    assert.equal(h.upiReceipt.value.payment_platform, platform);
    assert.deepEqual(plain(h.jobs.items), items);
    assert.match(h.upiNotice.value, platform === 'none' ? /只提链，不自动支付/ : /批量提交到提链平台.*会自动提交支付/);
    assert.doesNotMatch(h.upiNotice.value, /逐条/);
  }
});

test('rendered payment selector shows exactly one platform configuration and fixed Seashore address', async () => {
  for (const platform of ['none', 'foarge', 'masi', 'seashore']) {
    const html = await renderView((state) => {
      state.settings.value = paymentSettings(platform, { seashore_api_base: 'https://ignored.example.test' });
      Object.assign(state.upiOptions, paymentDefaults, { payment_platform: platform });
      state.upiText.value = 'one\ntwo'; state.prepareUpi();
    });
    const selector = html.match(/<select[^>]+id="[^"]+-payment-platform"[\s\S]*?<\/select>/)[0];
    assert.equal((selector.match(/<option /g) || []).length, 4);
    assert.deepEqual([...selector.matchAll(/<option value="([^"]+)"/g)].map((match) => match[1]), ['none', 'foarge', 'masi', 'seashore']);
    for (const [key, selected] of [['foarge_credential', 'foarge'], ['masi_cdk_ticket', 'masi'], ['seashore_cdk', 'seashore']]) {
      assert.equal(html.includes(`-${key}" type="password"`), platform === selected);
    }
    assert.match(html, /批量提交到提链平台/);
    assert.doesNotMatch(html, /逐条创建|ignored\.example\.test/);
    if (platform === 'seashore') assert.match(html, /<input value="https:\/\/seashore\.lol\/api\/publisher" readonly/);
    if (platform === 'none') assert.match(html, /本批平台：只提链，不自动支付/);
    else { assert.match(html, /本批平台：.*会自动提交支付/); assert.doesNotMatch(html, /只提链，不自动支付/); }
  }
});

test('job rendering separates submission from final payment, keeps unknown statuses visible and defaults historical jobs to none', async (t) => {
  const h = useHarness(t);
  assert.match(h.paymentSubmitLabel('submitted'), /已提交，待确认到账/);
  for (const status of [undefined, 'unknown', 'future_state']) {
    assert.doesNotMatch(h.paymentStatusLabel(status), /已到账|成功/);
    assert.doesNotMatch(h.paymentSubmitLabel(status), /已到账|成功/);
  }
  const html = await renderView((state) => {
    state.jobs.items = [job('historical', 'done'), { ...job('auto', 'done'), payment_platform: 'masi', result: {
      payment_submit_status: 'submitted', payment_submit_id: 'payment-123', payment_submit_error: 'provider message', payment_status: 'future_state',
    } }, { ...job('paid', 'done'), payment_platform: 'seashore', result: { payment_status: 'completed' } }];
  });
  assert.match(html, /本次平台：只提链/); assert.match(html, /本次平台：Masi/);
  assert.match(html, /自动支付提交：已提交，待确认到账/); assert.match(html, /支付提交编号：payment-123/);
  assert.match(html, /支付提交错误：provider message/); assert.match(html, /最终到账状态：未知状态（future_state）/);
  assert.match(html, /最终到账状态：已到账/);
});

test('fusion validates 2–100 distinct cards and the 120 character label before any POST', (t) => {
  const h = useHarness(t);
  for (const codes of [[], ['one'], ['one', 'one'], ['one two', 'three'], Array.from({ length: 101 }, (_, i) => `cdk-${i}`)]) {
    prepareFusion(h, codes); assert.equal(h.fuseDraft.value, null); assert.ok(h.fuseError.value);
  }
  prepareFusion(h, fuseCodes, 'x'.repeat(121)); assert.equal(h.fuseDraft.value, null);
  prepareFusion(h, Array.from({ length: 100 }, (_, i) => `cdk-${i}`), 'x'.repeat(120));
  assert.equal(h.fuseDraft.value.codes.length, 100); h.editFuse();
  h.fuseText.value = '  keep-fixture-123 \r\n\n source-fixture-456 '; h.fuseLabel.value = ' label '; h.prepareFuse();
  assert.deepEqual(plain(h.fuseDraft.value), { codes: fuseCodes.slice(0, 2), label: 'label' });
  assert.equal(h.requests.length, 0);
});

test('fusion preview freezes the order, hides full codes, and returning to edit sends nothing', async (t) => {
  const h = useHarness(t); prepareFusion(h);
  assert.equal(Object.isFrozen(h.fuseDraft.value), true); assert.equal(Object.isFrozen(h.fuseDraft.value.codes), true);
  const html = await renderView((state) => { state.fuseText.value = fuseCodes.join('\n'); state.prepareFuse(); });
  assert.match(html, /确认融合 3 张 CDK/); assert.match(html, /keep|kee••••123/);
  assert.match(html, /其余 2 张源卡/);
  for (const code of fuseCodes) assert.equal(html.includes(code), false);
  assert.equal(h.maskFuseTarget('abc'), '••••');
  h.editFuse(); await h.confirmFuse(); assert.equal(h.fuseLocked.value, false); assert.equal(mutations(h).length, 0);
});

test('backend string fusion receipt clears only fusion inputs and preserves saved CDK, drafts and history', async (t) => {
  const h = useHarness(t); prepareFusion(h);
  h.secrets.cdk = 'pending-cdk-edit'; h.jobs.items = [job('history', 'done')];
  h.respond('/api/external/upi/fuse', fusion(), 'POST');
  h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('history', 'done')]));
  await h.confirmFuse();
  assert.deepEqual(mutations(h).map(({ path, body }) => ({ path, body })), [{ path: '/api/external/upi/fuse', body: { codes: fuseCodes, label: '融合标签' } }]);
  assert.deepEqual(plain(h.fuseResult.value), { duplicate: false, transferred_points: '7', target: { remaining_uses: '17', total_uses: '27', enabled: true } });
  assert.equal(h.fuseUnknown.value, false); assert.equal(h.fuseDraft.value, null); assert.equal(h.fuseText.value, ''); assert.equal(h.fuseLabel.value, '');
  assert.equal(h.secrets.cdk, 'pending-cdk-edit'); assert.equal(h.settings.value.upi.cdk_masked, 'cdk…1234');
  assert.equal(h.jobs.items[0].id, 'history'); assert.equal(h.requests.some((request) => request.method === 'PUT' || /activate/.test(request.path)), false);
});

test('fusion accepts numeric and decimal-string amounts, optional target fields and duplicate receipts without losing precision', async (t) => {
  const h = useHarness(t);
  for (const [points, remaining] of [[7, 17], ['7', '17'], ['0.000000000000000001', '9007199254740993.25'], [0, 0]]) {
    prepareFusion(h); h.respond('/api/external/upi/fuse', fusion({ duplicate: true, transferred_points: points, target: { remaining_uses: remaining } }), 'POST');
    await h.confirmFuse();
    assert.deepEqual(plain(h.fuseResult.value), { duplicate: true, transferred_points: String(points), target: { remaining_uses: String(remaining) } });
    assert.equal(h.fuseUnknown.value, false);
  }
  const html = await renderView((state) => { state.fuseResult.value = { duplicate: true, transferred_points: '7', target: { remaining_uses: '17' } }; });
  assert.match(html, /已复用同组合的融合结果 · 转入 7 点/); assert.match(html, /保留卡：剩余 17 点/);
  assert.doesNotMatch(html, /总额度| · 已启用| · 已停用/);
});

test('incomplete or invalid fusion core receipts keep the original input pending manual resolution', async (t) => {
  const h = useHarness(t);
  for (const result of [fusion({ ok: false }), fusion({ duplicate: undefined }), fusion({ transferred_points: '-1' }), fusion({ transferred_points: Infinity }), fusion({ transferred_points: 'NaN' }), fusion({ target: {} }), fusion({ target: { remaining_uses: -1 } })]) {
    prepareFusion(h); h.respond('/api/external/upi/fuse', result, 'POST'); await h.confirmFuse();
    assert.equal(h.fuseUnknown.value, true); assert.equal(h.fuseResult.value, null);
    assert.deepEqual(plain(h.fuseDraft.value.codes), fuseCodes); h.editFuse();
  }
});

test('fusion network uncertainty never auto-retries and manual retry uses the identical frozen combination', async (t) => {
  const h = useHarness(t); await h.mount(); prepareFusion(h);
  h.respond('/api/external/upi/fuse', new Error('network timeout'), 'POST'); await h.confirmFuse();
  const original = mutations(h)[0].body;
  assert.equal(h.fuseUnknown.value, true); assert.equal(h.fuseLocked.value, true);
  h.fuseText.value = 'another-card\nwrong-source'; h.fuseLabel.value = 'edited-label'; h.prepareFuse();
  await h.tick(); await h.hide(); await h.hide(false); await h.refreshSettings();
  assert.equal(mutations(h).length, 1);
  h.respond('/api/external/upi/fuse', fusion({ duplicate: true }), 'POST'); await h.confirmFuse();
  assert.deepEqual(mutations(h)[1].body, original); assert.equal(h.fuseResult.value.duplicate, true); assert.equal(h.fuseUnknown.value, false);
});

test('FUSION_UNKNOWN stays locked even if a later retry is rejected, until the user explicitly edits', async (t) => {
  const h = useHarness(t); prepareFusion(h);
  h.respond('/api/external/upi/fuse', Object.assign(httpError(502, `check ${fuseCodes[0]}`), { code: 'FUSION_UNKNOWN' }), 'POST');
  await h.confirmFuse(); assert.equal(h.fuseUnknown.value, true); assert.equal(h.fuseError.value.includes(fuseCodes[0]), false);
  h.respond('/api/external/upi/fuse', httpError(422), 'POST'); await h.confirmFuse();
  assert.equal(h.fuseLocked.value, true); assert.equal(h.fuseUnknown.value, true);
  h.fuseText.value = 'out-of-band'; h.editFuse();
  assert.equal(h.fuseText.value, fuseCodes.join('\n')); assert.equal(h.fuseLabel.value, '融合标签'); assert.equal(h.fuseUnknown.value, false);
  h.fuseText.value = 'different-target\ndifferent-source'; h.prepareFuse();
  assert.deepEqual(plain(h.fuseDraft.value.codes), ['different-target', 'different-source']);
});

test('fusion double clicks and edits are blocked during a request and unmount clears cards and ignores a late success', async (t) => {
  const h = useHarness(t); prepareFusion(h); const pending = deferred();
  h.respond('/api/external/upi/fuse', pending.promise, 'POST'); const first = h.confirmFuse();
  await h.confirmFuse(); h.editFuse(); h.prepareFuse(); await h.saveSettings('upi');
  assert.equal(mutations(h).length, 1); assert.equal(h.fuseBusy.value, true); assert.ok(h.fuseDraft.value);
  h.dispose(); assert.equal(h.fuseText.value, ''); assert.equal(h.fuseLabel.value, ''); assert.equal(h.fuseDraft.value, null);
  pending.resolve(fusion()); await first; assert.equal(h.fuseResult.value, null);
});

test('saving, creating and fusing stop timers, abort stale reads and resume only after completion', async (t) => {
  for (const mode of ['save', 'create', 'fuse']) {
    const h = useHarness(t); h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job()])); await h.mount();
    assert.equal(h.timers.size, 1);
    const read = deferred(); h.respond(/^\/api\/external\/upi\/jobs\?/, read.promise); const reading = h.jobPager.load(true);
    const signal = h.requests.at(-1).options.signal;
    const response = deferred(); let action;
    if (mode === 'save') { h.secrets.cdk = 'updated-cdk'; h.respond('/api/external/settings', response.promise, 'PUT'); action = h.saveSettings('upi'); }
    if (mode === 'create') { h.upiText.value = 'one'; h.prepareUpi(); h.respond('/api/external/upi/jobs', response.promise, 'POST'); action = h.confirmUpi(); }
    if (mode === 'fuse') { prepareFusion(h); h.respond('/api/external/upi/fuse', response.promise, 'POST'); action = h.confirmFuse(); }
    assert.equal(h.timers.size, 0); assert.equal(signal.aborted, true);
    const count = h.requests.length;
    await h.jobPager.load(true); await h.jobPager.go(2); await h.loadCredits(); await h.selectTab('scan'); await h.selectTab('upi');
    await h.hide(); await h.hide(false); await h.tick();
    assert.equal(h.requests.length, count); assert.equal(h.timers.size, 0);
    read.resolve(jobPage([job('stale')])); await reading; assert.equal(h.jobs.items[0].id, 'job-fixture');
    h.respond(/^\/api\/external\/upi\/jobs\?/, jobPage([job('resumed')]));
    response.resolve(mode === 'fuse' ? fusion() : { ok: true, batch_id: 'batch' }); await action;
    assert.equal(h.jobs.items[0].id, 'resumed'); assert.equal(h.timers.size, 1);
  }
});

test('the real SFC compiles/renders without a router/store and the wrapper/menu use the shared view', async () => {
  const { descriptor, errors } = parse(source, { filename: file.pathname });
  assert.deepEqual(errors, []);
  const compiled = compileScript(descriptor, { id: 'external-api-test' });
  const template = compileTemplate({ source: descriptor.template.content, filename: file.pathname, id: 'external-api-test', compilerOptions: { bindingMetadata: compiled.bindings } });
  assert.deepEqual(template.errors, []);
  const code = ts.transpileModule(`${compiled.content}\n${template.code}`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const exports = {};
  runInNewContext(code, { exports, require: (id) => id === 'vue' ? vue : { api: () => assert.fail('SSR must not call services'), jsonBody: JSON.stringify },
    URL, URLSearchParams, AbortController, Error, Uint8Array, crypto: webcrypto, setTimeout, clearTimeout });
  exports.default.render = exports.render;
  const html = await renderToString(vue.createSSRApp(exports.default));
  assert.match(html, /外部 API/); assert.match(html, /UPI 提链/); assert.match(html, /CDK-Scan 扫码/);
  assert.match(html, /https:\/\/scan\.fishmail\.vip/); assert.match(html, /每成功提链 1 条消耗 1 点/);
  assert.match(html, /保存 API Key 后查询订单/);
  assert.doesNotMatch(source, /\b(?:fetch|localStorage|sessionStorage|console|use[A-Za-z]+Store)\s*[.(]/);
  const originalSetup = exports.default.setup;
  exports.default.setup = (props, ctx) => {
    const state = originalSetup(props, ctx);
    state.settings.value = savedSettings();
    state.jobs.items = [{ ...job('qr-job', 'done'), created_at: 1789000000.5, result: { payment_link: 'https://short.example.test/a', scan_link: stripeLink, qr_code: 'data:image/png;base64,aW1hZ2U=' } }];
    state.orders.items = [{ order_no: 'order-charge-test', status: 'completed', charge_amount: '2.75', unit_price: '3.00' }];
    return state;
  };
  const populatedHtml = await renderToString(vue.createSSRApp(exports.default));
  assert.match(populatedHtml, /<img[^>]+src="data:image\/png;base64,aW1hZ2U="/);
  assert.match(populatedHtml, /填入扫码/);
  assert.match(populatedHtml, /实际收费 2.75/);
  assert.match(populatedHtml, /单价 3.00/);
  assert.doesNotMatch(populatedHtml, /1789000000/);
  assert.doesNotMatch(populatedHtml, /<pre[^>]*>data:image/);
  assert.doesNotMatch(source, /v-html/);
  const wrapper = readFileSync(new URL('../src/new/views/integrations/ExternalApiView.vue', import.meta.url), 'utf8');
  assert.match(wrapper, /import ExternalApiView from "\.\.\/\.\.\/\.\.\/components\/ExternalApiView.vue"/);
  const router = readFileSync(new URL('../src/new/router/index.ts', import.meta.url), 'utf8');
  assert.match(router, /path: "\/external-api", component: ExternalApiView/);
  assert.match(router, /to: "\/sms-pool"[^\n]+\n\s*\{ to: "\/external-api"/);
});
