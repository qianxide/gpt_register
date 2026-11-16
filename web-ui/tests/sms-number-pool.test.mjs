import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as vue from 'vue';
import ts from 'typescript';

const plain = (value) => JSON.parse(JSON.stringify(value));
const poolSummary = { total: 4, available: 1, reserved: 1, cooling: 1, exhausted: 1 };
const number = (id, status = 'available') => ({ id, phone: `+1202555010${id}`, country: 'US', source_date: '2026-09-17', uses: status === 'exhausted' ? 3 : 0, max_uses: 3, remaining_uses: status === 'exhausted' ? 0 : 3, status, cooldown_until: 0, cooldown_remaining: 0, last_used_at: 0 });
const poolPage = (extra = {}) => ({ items: [number(1), number(2, 'reserved'), number(3, 'cooling'), number(4, 'exhausted')], total: 4, page: 1, page_size: 20, summary: poolSummary, max_uses: 3, cooldown_seconds: 3600, ...extra });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function pool() {
  const source = readFileSync(new URL('../src/components/SmsNumberPool.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const names = 'items, summary, total, page, pageSize, pageInput, pageCount, status, queryInput, query, selected, loading, listError, actionError, notice, text, readingFile, mutation, activated, configLoading, configError, importResult, pendingDelete, allSelected, someSelected, now, loadedAt, controlsDisabled, load, loadConfig, applyFilters, goToPage, selectPage, readFile, importNumbers, activatePool, requestDelete, cancelDelete, confirmDelete, cooldownLabel';
  const { outputText } = ts.transpileModule(`${source}\nexport { ${names} };`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  const exports = {}, requests = [], responses = [], mounted = [], cleanup = [], intervals = new Map();
  const configRequests = [], configResponses = [];
  let timerId = 0;
  const imports = {
    vue: { ...vue, useId: () => 'pool-test', onMounted: (fn) => mounted.push(fn), onBeforeUnmount: (fn) => cleanup.push(fn) },
    '../api.js': { jsonBody: JSON.stringify, api: async (path, options = {}) => {
      if (path === '/api/sms-config' && !options.method) {
        configRequests.push({ path, options });
        const result = configResponses.length ? configResponses.shift() : { provider: 'smsbower', enabled: true };
        return typeof result === 'function' ? result(path, options) : result;
      }
      requests.push({ path, options });
      assert.ok(responses.length, `Unexpected request: ${path}`);
      const result = responses.shift();
      return typeof result === 'function' ? result(path, options) : result;
    } },
  };
  runInNewContext(outputText, {
    exports, require: (id) => { assert.ok(id in imports, id); return imports[id]; },
    URLSearchParams, AbortController, Date, Error, defineProps: () => ({}),
    setInterval: (fn) => { intervals.set(++timerId, fn); return timerId; },
    clearInterval: (id) => intervals.delete(id),
  });
  return { ...exports, requests, responses, configRequests, configResponses, intervals, mount: () => mounted.forEach((fn) => fn()), dispose: () => cleanup.forEach((fn) => fn()) };
}

test('pool filtering sends encoded queries, resets pagination, and selects only the visible page', async () => {
  const h = pool();
  h.responses.push(poolPage());
  await h.load();
  assert.equal(h.requests[0].path, '/api/sms-pool?page=1&page_size=20&status=all&q=');
  h.selectPage(true);
  assert.deepEqual(plain(h.selected.value), [1, 2, 3, 4]);
  assert.equal(h.allSelected.value, true);
  h.selected.value = [1];
  assert.equal(h.someSelected.value, true);
  h.status.value = 'exhausted';
  h.queryInput.value = ' +1202 ';
  h.page.value = 3;
  h.pageSize.value = 50;
  h.responses.push(poolPage({ items: [number(4, 'exhausted')], total: 1, page_size: 50 }));
  await h.applyFilters();
  assert.equal(h.requests[1].path, '/api/sms-pool?page=1&page_size=50&status=exhausted&q=%2B1202');
  assert.equal(h.page.value, 1);
  assert.deepEqual(plain(h.selected.value), []);
  h.selectPage(true);
  assert.deepEqual(plain(h.selected.value), [4]);
  h.selectPage(false);
  assert.equal(h.allSelected.value, false);
});

test('page navigation clears selections and rejects out-of-range or fractional pages', async () => {
  const h = pool();
  h.responses.push(poolPage({ total: 21 }), poolPage({ items: [number(9)], total: 21, page: 2 }));
  await h.load();
  h.selectPage(true);
  await h.goToPage(2);
  assert.match(h.requests[1].path, /page=2&page_size=20/);
  assert.deepEqual(plain(h.selected.value), []);
  assert.equal(h.pageInput.value, 2);
  await h.goToPage(3);
  await h.goToPage(1.5);
  assert.equal(h.requests.length, 2);
  assert.equal(h.pageInput.value, 2);
});

test('markdown import preserves the exact input and keeps partial failures for correction', async () => {
  const h = pool();
  const input = '\n2026-09-17|+12025550101|[取码](https://168smsapi.com/sms/openai-/demo-01)\r\ninvalid-line\n';
  h.text.value = input;
  h.responses.push({ imported: 1, duplicates: 2, invalid: 201, errors: [{ line: 3, error: '格式错误' }], summary: poolSummary }, poolPage());
  await h.importNumbers();
  assert.equal(h.requests[0].path, '/api/sms-pool/import');
  assert.deepEqual(JSON.parse(h.requests[0].options.body), { text: input });
  assert.equal(h.text.value, input);
  assert.equal(h.importResult.value.invalid, 201);
  assert.equal(h.importResult.value.errors[0].line, 3);
  assert.match(h.notice.value, /导入 1 个，重复 2 个，无效 201 行/);
  h.responses.push({ imported: 0, duplicates: 1, invalid: 0, errors: [], summary: poolSummary }, poolPage());
  await h.importNumbers();
  assert.equal(h.text.value, '');
});

test('text-file import fills the editor without submitting, and failures keep pasted data', async () => {
  const h = pool();
  const input = { files: [{ text: async () => '2026-09-17|+12025550101|https://168smsapi.com/sms/openai-/demo-01' }], value: 'numbers.txt' };
  await h.readFile({ target: input });
  assert.equal(h.requests.length, 0);
  assert.match(h.text.value, /168smsapi.com/);
  assert.equal(input.value, '');
  h.responses.push(() => { throw new Error('import offline'); });
  await h.importNumbers();
  assert.match(h.actionError.value, /import offline/);
  assert.match(h.text.value, /168smsapi.com/);
  assert.equal(h.mutation.value, '');
  await h.readFile({ target: { files: [{ text: async () => { throw new Error('read failed'); } }], value: 'bad.txt' } });
  assert.match(h.actionError.value, /读取失败/);
  assert.equal(h.readingFile.value, false);
});

test('selection delete waits for built-in confirmation and cancellation performs no request', async () => {
  const h = pool();
  h.responses.push(poolPage());
  await h.load();
  h.selected.value = [1, 2, 999];
  await h.requestDelete(false);
  assert.deepEqual(plain(h.pendingDelete.value), { ids: [1, 2] });
  assert.equal(h.requests.length, 1);
  assert.equal(h.controlsDisabled.value, true);
  await h.cancelDelete();
  assert.equal(h.pendingDelete.value, null);
  assert.equal(h.requests.length, 1);
  await h.requestDelete(false);
  h.responses.push({ deleted: 1, skipped: 1, summary: poolSummary }, poolPage({ items: [number(2, 'reserved')] }));
  await h.confirmDelete();
  assert.equal(h.requests[1].options.method, 'DELETE');
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { ids: [1, 2] });
  assert.match(h.notice.value, /已删除 1 个，跳过 1 个/);
  assert.equal(h.pendingDelete.value, null);
  assert.deepEqual(plain(h.selected.value), []);
});

test('delete all exhausted ignores current filters and recovers an empty last page', async () => {
  const h = pool();
  h.page.value = 3;
  h.status.value = 'exhausted';
  h.query.value = '+1202';
  h.responses.push(poolPage({ page: 3, total: 41, items: [number(4, 'exhausted')] }));
  await h.load();
  await h.requestDelete(true);
  h.responses.push({ deleted: 4, skipped: 0, summary: poolSummary }, poolPage({ page: 3, total: 37, items: [] }), poolPage({ page: 2, total: 37, items: [number(9)] }));
  await h.confirmDelete();
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { exhausted_only: true });
  assert.equal(h.page.value, 2);
  assert.equal(h.pageInput.value, 2);
  assert.equal(h.loading.value, false);
  assert.equal(h.items.value[0].id, 9);
});

test('deletion errors retain confirmation for retry and concurrent clicks cannot submit twice', async () => {
  const h = pool(), pending = deferred();
  h.responses.push(poolPage());
  await h.load();
  h.selected.value = [1];
  await h.requestDelete(false);
  h.responses.push(pending.promise);
  const first = h.confirmDelete();
  await h.confirmDelete();
  await h.cancelDelete();
  assert.equal(h.requests.length, 2);
  assert.equal(h.mutation.value, 'delete');
  pending.reject(new Error('delete offline'));
  await first;
  assert.match(h.actionError.value, /delete offline/);
  assert.deepEqual(plain(h.pendingDelete.value), { ids: [1] });
  assert.deepEqual(plain(h.selected.value), [1]);
  assert.equal(h.mutation.value, '');
});

test('stale requests never overwrite new filters; load failures allow an explicit retry', async () => {
  const h = pool(), old = deferred();
  h.responses.push(old.promise, poolPage({ items: [number(4, 'exhausted')], total: 1 }));
  const first = h.load();
  h.status.value = 'exhausted';
  await h.load();
  assert.equal(h.requests[0].options.signal.aborted, true);
  old.resolve(poolPage());
  await first;
  assert.equal(h.items.value.length, 1);
  assert.equal(h.items.value[0].id, 4);
  h.responses.push(() => { throw new Error('list offline'); });
  await h.load();
  assert.match(h.listError.value, /list offline/);
  assert.equal(h.items.value.length, 0);
  assert.equal(h.loading.value, false);
  await h.requestDelete(true);
  assert.equal(h.pendingDelete.value, null);
  h.responses.push(poolPage());
  await h.load();
  assert.equal(h.listError.value, '');
});

test('cooldown uses seconds, never goes negative, and leaves availability to the server', async () => {
  const h = pool();
  h.now.value = 1000;
  h.loadedAt.value = 990;
  const item = { ...number(1, 'cooling'), cooldown_until: 4600, source_date: '2020-01-01' };
  assert.equal(h.cooldownLabel(item), '60:00');
  item.cooldown_until = 999;
  assert.equal(h.cooldownLabel(item), '已到期，请刷新');
  assert.equal(item.status, 'cooling');
  item.cooldown_until = 0;
  item.cooldown_remaining = 100;
  assert.equal(h.cooldownLabel(item), '1:30');
  assert.equal(h.cooldownLabel(number(2, 'exhausted')), '—');
  h.responses.push(poolPage({ items: [{ ...number(3), source_date: '2020-01-01' }] }));
  await h.load();
  assert.equal(h.items.value[0].status, 'available', 'source dates never expire numbers in the UI');
});

test('leaving the page aborts the list request and releases the countdown clock', async () => {
  const h = pool(), pending = deferred();
  h.responses.push(pending.promise);
  h.mount();
  assert.equal(h.intervals.size, 1);
  h.dispose();
  assert.equal(h.requests[0].options.signal.aborted, true);
  assert.equal(h.configRequests[0].options.signal.aborted, true);
  assert.equal(h.intervals.size, 0);
  pending.resolve(poolPage());
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.items.value.length, 0);
});

test('activating the pool sends only provider and enabled; failure remains retryable', async () => {
  const h = pool();
  h.responses.push(() => { throw new Error('settings offline'); });
  await h.activatePool();
  assert.equal(h.activated.value, null);
  assert.equal(h.mutation.value, '');
  assert.match(h.actionError.value, /settings offline/);
  h.responses.push({ ok: true });
  await h.activatePool();
  assert.equal(h.requests[1].path, '/api/sms-config');
  assert.equal(h.requests[1].options.method, 'PUT');
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { provider: 'imported_pool', enabled: true });
  assert.equal(h.activated.value, true);
  assert.equal(h.actionError.value, '');
});

test('page load and refresh restore activation from saved config without blocking the list on config failure', async () => {
  const h = pool();
  assert.equal(h.activated.value, null);
  h.configResponses.push({ provider: 'imported_pool', enabled: true });
  h.responses.push(poolPage());
  await h.load();
  assert.equal(h.activated.value, true);
  h.configResponses.push({ provider: 'firefox', enabled: true });
  h.responses.push(poolPage());
  await h.load();
  assert.equal(h.activated.value, false);
  h.configResponses.push({ provider: 'imported_pool', enabled: false });
  await h.loadConfig();
  assert.equal(h.activated.value, false);
  h.configResponses.push(() => { throw new Error('config offline'); });
  h.responses.push(poolPage());
  await h.load();
  assert.equal(h.activated.value, null);
  assert.match(h.configError.value, /config offline/);
  assert.equal(h.listError.value, '');
  assert.equal(h.items.value.length, 4);
  assert.equal(h.controlsDisabled.value, false);
});

test('a config read started before activation cannot overwrite the successful write', async () => {
  const h = pool(), stale = deferred();
  h.configResponses.push(stale.promise);
  h.responses.push(poolPage());
  await h.load();
  assert.equal(h.loading.value, false);
  assert.equal(h.configLoading.value, true);
  h.responses.push({ ok: true });
  await h.activatePool();
  assert.equal(h.configRequests[0].options.signal.aborted, true);
  assert.equal(h.activated.value, true);
  stale.resolve({ provider: 'smsbower', enabled: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.activated.value, true);
  assert.equal(h.configLoading.value, false);
  assert.equal(h.configError.value, '');
});
