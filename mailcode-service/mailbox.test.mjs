import assert from 'node:assert/strict';
import test from 'node:test';
import { extractCode, fetchMailboxMessages } from './mailbox.mjs';

const NOW = 1_800_000_000;
const ALIAS = 'ann@mail.com';

function message(id, header = {}, read = false) {
  return {
    mailURI: `../../Mail/${id}`,
    attribute: { mailIdentifier: id, read },
    mailHeader: { from: 'Service <sender@example.test>', to: [ALIAS], subject: 'Sign in', date: NOW - 5, ...header },
    sourceFolder: { folderIdentifier: 'inbox', folderType: 'INBOX' },
  };
}

function fixture(t, messages, bodies = {}) {
  t.mock.method(Date, 'now', () => NOW * 1000);
  const listed = [];
  const fetched = [];
  const client = {
    mail: {
      async listIncoming(options) {
        listed.push(options);
        assert.deepEqual(options, { amount: 50, includeSpam: true, tagsShowAll: true });
        return { mail: messages, totalCount: messages.length };
      },
      async getBody(id, options) {
        fetched.push({ id, options });
        assert.deepEqual(options, { markRead: false });
        const body = bodies[id] ?? 'Verification code: 042819';
        if (body instanceof Error) throw body;
        return body;
      },
    },
    actions: {
      markRead() { assert.fail('read-state writes are out of scope'); },
      markUnread() { assert.fail('read-state writes are out of scope'); },
    },
  };
  return { client, listed, fetched };
}

test('isolates aliases before body reads, including display-name and comment decoys', async (t) => {
  const messages = [
    message('prefix', { to: ['joann@mail.com'] }),
    message('suffix', { to: ['ann@mail.com.evil.test'] }),
    message('plus', { to: ['ann+tag@mail.com'] }),
    message('dots', { to: ['a.nn@mail.com'] }),
    message('display', { to: ['"ann@mail.com" <other@mail.com>'] }),
    message('quoted-angle', { to: ['"Team <ann@mail.com>" <other@mail.com>'] }),
    message('comment', { to: ['Other (ann@mail.com) <other@mail.com>'] }),
    message('group-name', { to: ['"ann@mail.com": other@mail.com;'] }),
    message('bcc', { to: [], bcc: [ALIAS] }),
    message('sender-only', { to: ['other@mail.com'], from: ALIAS, subject: `For ${ALIAS}` }),
    message('exact', { to: ['ANN@MAIL.COM'], date: NOW - 1 }),
    message('cc', { to: ['other@mail.com'], cc: ['"Ann, Team" <ann@mail.com>'], date: NOW - 2 }),
    message('list', { to: ['"Other, Person" <other@mail.com>, Ann <ann@mail.com>'], date: NOW - 3 }),
  ];
  const { client, listed, fetched } = fixture(t, messages);
  const result = await fetchMailboxMessages(client, ' ANN@MAIL.COM ');
  assert.deepEqual(result.map((item) => item.id), ['exact', 'cc', 'list']);
  assert.deepEqual(fetched.map((item) => item.id), ['exact', 'cc', 'list']);
  assert.equal(listed.length, 1);
  assert.deepEqual(result[1].to, ['other@mail.com', ALIAS]);
});

test('parses groups, folded headers, escaped display names and nested comments', async (t) => {
  const headers = [
    'Team: "Other, Person" <other@mail.com>, "Ann" <ann@mail.com>;',
    'other@mail.com,\r\n Ann <ann@mail.com>',
    '"An\\"n, <decoy@mail.com>" <ann@mail.com>',
    'Ann (team (ann2@mail.com)) <ann@mail.com> (primary)',
    'ann@mail.com; other@mail.com',
  ];
  const { client, fetched } = fixture(t, headers.map((to, index) => message(`valid-${index}`, { to, cc: ['copy@example.test'] })));
  assert.equal((await fetchMailboxMessages(client, ALIAS)).length, headers.length);
  assert.equal(fetched.length, headers.length);
});

test('ambiguous and malformed recipients never trigger a body read', async (t) => {
  const headers = [
    'Ann ann@mail.com', 'other@mail.com ann@mail.com',
    '"ann@mail.com', '<ann@mail.com', 'ann@mail.com>',
    '<ann@mail.com> <other@mail.com>', '(ann@mail.com)',
    'other@mail.com (ann@mail.com', '"ann@mail.com"',
    'mailto:ann@mail.com', '<mailto:ann@mail.com>',
    'other@mail.com\r\nCc: ann@mail.com',
    '<ann@mail.com> trailing-text',
    null, { address: ALIAS }, [false, { address: ALIAS }],
  ];
  const { client, fetched } = fixture(t, headers.map((to, index) => message(`bad-${index}`, { to })));
  assert.deepEqual(await fetchMailboxMessages(client, ALIAS), []);
  assert.deepEqual(fetched, []);
});

test('invalid alias input fails before accessing the client', async (t) => {
  const { client, listed } = fixture(t, []);
  for (const alias of ['', null, {}, 'ann', 'Ann <ann@mail.com>', 'ann@mail.com,other@mail.com', 'ann@mail.com\nother@mail.com']) {
    await assert.rejects(fetchMailboxMessages(client, alias), /aliasAddress/);
  }
  assert.deepEqual(listed, []);
});

test('normalizes seconds and milliseconds, excludes missing/invalid/stale/future dates and sorts first', async (t) => {
  const dates = [
    ['boundary', NOW - 600], ['seconds', NOW - 30], ['millis', (NOW - 20) * 1000 + 123],
    ['numeric-string', String(NOW - 10)], ['millis-string', String((NOW - 8) * 1000)],
    ['iso', new Date((NOW - 6) * 1000).toISOString()],
    ['undated', undefined], ['null', null], ['empty', ''], ['boolean', true],
    ['nan', NaN], ['infinity', Infinity], ['invalid', 'yesterday'], ['invalid-number', '0x1234'],
    ['object', {}], ['stale', NOW - 600.001], ['stale-ms', (NOW - 601) * 1000],
    ['future', NOW + 1], ['future-ms', (NOW + 1) * 1000], ['microseconds', NOW * 1_000_000],
  ];
  const { client, fetched } = fixture(t, dates.map(([id, date]) => message(id, { date })));
  const result = await fetchMailboxMessages(client, ALIAS);
  const expected = ['iso', 'millis-string', 'numeric-string', 'millis', 'seconds', 'boundary'];
  assert.deepEqual(result.map((item) => item.id), expected);
  assert.deepEqual(fetched.map((item) => item.id), expected);
  assert.equal(result.find((item) => item.id === 'millis').received_at, NOW - 20 + 0.123);
  assert.ok(result.every((item) => typeof item.received_at === 'number'));
});

test('after and maxAgeSeconds both apply, with inclusive lower boundaries', async (t) => {
  const messages = [
    message('stale', { date: NOW - 700 }), message('older', { date: NOW - 90 }),
    message('boundary', { date: NOW - 60 }), message('new', { date: NOW - 5 }),
  ];
  const { client, fetched } = fixture(t, messages);
  assert.deepEqual((await fetchMailboxMessages(client, ALIAS, { after: NOW - 1000 })).map((item) => item.id), ['new', 'boundary', 'older']);
  fetched.length = 0;
  assert.deepEqual((await fetchMailboxMessages(client, ALIAS, { after: NOW - 60, maxAgeSeconds: 120 })).map((item) => item.id), ['new', 'boundary']);
  assert.deepEqual(fetched.map((item) => item.id), ['new', 'boundary']);
  assert.deepEqual((await fetchMailboxMessages(client, ALIAS, { after: NOW - 1000, maxAgeSeconds: 60 })).map((item) => item.id), ['new', 'boundary']);
});

test('does not round milliseconds across the after boundary', async (t) => {
  const { client, fetched } = fixture(t, [
    message('before', { date: (NOW - 10) * 1000 - 1 }),
    message('at', { date: (NOW - 10) * 1000 }),
    message('after', { date: (NOW - 10) * 1000 + 1 }),
  ]);
  const result = await fetchMailboxMessages(client, ALIAS, { after: NOW - 10 });
  assert.deepEqual(result.map((item) => item.id), ['after', 'at']);
  assert.deepEqual(fetched.map((item) => item.id), ['after', 'at']);
});

test('normalizes output and preserves both read and unread messages without mutating headers', async (t) => {
  const unread = message('unread', { date: NOW - 1, to: ['Ann <ANN@MAIL.COM>'], cc: [ALIAS] });
  const read = message('read', { date: NOW - 2, subject: undefined, from: undefined }, true);
  const messages = [read, unread];
  const before = structuredClone(messages);
  for (const item of messages) { Object.freeze(item.attribute); Object.freeze(item.mailHeader); Object.freeze(item); }
  Object.freeze(messages);
  const { client, fetched } = fixture(t, messages, {
    unread: '<style>.a{color:#123456}</style><p>Verification code: <b>042819</b></p>',
    read: '<p>Welcome &amp; hello.</p>',
  });
  const result = await fetchMailboxMessages(client, ALIAS);
  assert.deepEqual(result[0], {
    id: 'unread', subject: 'Sign in', sender: 'Service <sender@example.test>', to: [ALIAS],
    received_at: NOW - 1, text: 'Verification code: 042819', code: '042819',
  });
  assert.deepEqual(result[1], {
    id: 'read', subject: '', sender: '', to: [ALIAS], received_at: NOW - 2,
    text: 'Welcome & hello.', code: null,
  });
  assert.deepEqual(messages, before);
  assert.ok(fetched.every((call) => call.options.markRead === false));
});

test('excludes whole OTP messages and fills the limit from older eligible messages', async (t) => {
  const { client, fetched } = fixture(t, [
    message('body-excluded', { date: NOW - 1 }),
    message('subject-excluded', { date: NOW - 2, subject: 'Verification code: 222222' }),
    message('fresh', { date: NOW - 3 }), message('unneeded', { date: NOW - 4 }),
  ], { 'body-excluded': 'Your OTP is 111111', fresh: 'Your OTP is 003333' });
  const exclude = Object.freeze([' 111111 ', 222222]);
  const result = await fetchMailboxMessages(client, ALIAS, { exclude, limit: 1 });
  assert.deepEqual(result.map((item) => [item.id, item.code]), [['fresh', '003333']]);
  assert.deepEqual(fetched.map((item) => item.id), ['body-excluded', 'fresh']);
  assert.ok(!JSON.stringify(result).includes('111111'));
  assert.ok(!JSON.stringify(result).includes('222222'));
});

test('does not substitute a second candidate from a message whose chosen code is excluded', async (t) => {
  const { client } = fixture(t, [message('used')], { used: 'Verification code: 111111. Reference 654321.' });
  assert.deepEqual(await fetchMailboxMessages(client, ALIAS, { exclude: ['111111'] }), []);
});

test('OTP extraction keeps subject/body digits separate and prefers labeled body codes', async (t) => {
  const { client } = fixture(t, [
    message('split', { subject: 'Your code: 1234', date: NOW - 1 }),
    message('body-code', { subject: 'Order reference 991199', date: NOW - 2 }),
    message('excluded-body', { subject: 'Order reference 887766', date: NOW - 3 }),
  ], {
    split: '5678 is a reference number.',
    'body-code': 'OTP: 003333',
    'excluded-body': 'OTP: 111111',
  });
  const result = await fetchMailboxMessages(client, ALIAS, { exclude: ['111111'] });
  assert.deepEqual(result.map((item) => [item.id, item.code]), [['split', '1234'], ['body-code', '003333']]);
});

test('default limit is 10, the cap is 20, and bodies are fetched newest first', async (t) => {
  const messages = Array.from({ length: 25 }, (_, index) => message(`m-${index}`, { date: NOW - index }));
  const { client, fetched } = fixture(t, [...messages].reverse());
  const first = await fetchMailboxMessages(client, ALIAS);
  assert.equal(first.length, 10);
  assert.deepEqual(fetched.map((item) => item.id), messages.slice(0, 10).map((item) => item.attribute.mailIdentifier));
  fetched.length = 0;
  const capped = await fetchMailboxMessages(client, ALIAS, { limit: 1000 });
  assert.equal(capped.length, 20);
  assert.equal(fetched.length, 20);
  assert.equal(capped.at(-1).id, 'm-19');
});

test('invalid bounds fail before SDK access and a zero limit makes no requests', async (t) => {
  const { client, listed } = fixture(t, []);
  for (const options of [
    { after: NaN }, { after: -1 }, { after: null }, { maxAgeSeconds: Infinity },
    { maxAgeSeconds: 'bad' }, { limit: -1 }, { limit: true }, { limit: '' }, { exclude: '123456' },
  ]) await assert.rejects(fetchMailboxMessages(client, ALIAS, options), /must be/);
  assert.deepEqual(await fetchMailboxMessages(client, ALIAS, { limit: 0 }), []);
  assert.deepEqual(listed, []);
});

test('normalizes SDK Mail URIs, skips missing IDs and deduplicates IDs across folders', async (t) => {
  const uri = message('fallback', { date: NOW - 1 });
  delete uri.attribute.mailIdentifier;
  uri.mailURI = '../../Mail/%66allback?view=html';
  const preferred = message('preferred', { date: NOW - 2 });
  preferred.mailURI = '../../Mail/unused';
  const duplicate = message('preferred', { date: NOW - 3 });
  const missing = message('missing');
  delete missing.attribute.mailIdentifier;
  delete missing.mailURI;
  const { client, fetched } = fixture(t, [missing, duplicate, uri, preferred, null, {}]);
  assert.deepEqual((await fetchMailboxMessages(client, ALIAS)).map((item) => item.id), ['fallback', 'preferred']);
  assert.deepEqual(fetched.map((item) => item.id), ['fallback', 'preferred']);
});

test('propagates listing errors without polling', async (t) => {
  const { client, listed, fetched } = fixture(t, []);
  const failure = new Error('listing failed');
  client.mail.listIncoming = async () => { listed.push('failed'); throw failure; };
  await assert.rejects(fetchMailboxMessages(client, ALIAS), (error) => error === failure);
  assert.equal(listed.length, 1);
  assert.equal(fetched.length, 0);
});

test('propagates body errors without retries or partial results', async (t) => {
  const failure = new Error('body failed');
  const { client, listed, fetched } = fixture(t, [message('failure')], { failure });
  await assert.rejects(fetchMailboxMessages(client, ALIAS), (error) => error === failure);
  assert.equal(listed.length, 1);
  assert.deepEqual(fetched.map((item) => item.id), ['failure']);
});

test('empty snapshots return [] and malformed SDK responses are surfaced', async (t) => {
  const { client, fetched } = fixture(t, []);
  assert.deepEqual(await fetchMailboxMessages(client, ALIAS), []);
  client.mail.listIncoming = async () => ({ totalCount: 1 });
  await assert.rejects(fetchMailboxMessages(client, ALIAS), /mail array/);
  assert.deepEqual(fetched, []);
});

test('extractCode prioritizes contextual OTPs and preserves leading zeroes', () => {
  for (const [text, expected] of [
    ['Order 991199. Your verification code is 004218.', '004218'],
    ['Copyright 2026. Security code: 879012', '879012'],
    ['OTP: 123-456', '123456'], ['PIN: 1 2 3 4', '1234'],
    ['Your code is 1234 5678', '12345678'], ['Code: 123456, valid for 10 minutes', '123456'],
    ['验证码：028193，请及时使用', '028193'], ['代码为 442819', '442819'],
    ['Reference 2026. 745921', '745921'], ['90817263', '90817263'], ['Use 9274', '9274'],
  ]) assert.equal(extractCode(text), expected, text);
});

test('extractCode ignores HTML metadata and decodes numeric/spacing entities', () => {
  const html = '<!-- code 999999 --><script>code = 888888</script><style>.code{color:#777777}</style>'
    + '<p title="code 666666 > 555555">Your code:&nbsp;<b>&#48;&#x34;2819</b></p>';
  assert.equal(extractCode(html), '042819');
  assert.equal(extractCode('<p>OTP: 1&nbsp;2&nbsp;3&nbsp;4&nbsp;5&nbsp;6</p>'), '123456');
  assert.equal(extractCode('Code: &#999999999; 876543'), '876543');
  assert.equal(extractCode('Code: 123456 &copy; 2026'), '123456');
});

test('extractCode avoids partial long numbers, identifiers, dates and markup-only digits', () => {
  for (const value of [
    '', null, undefined, 123456, 'hello', 'Code: 123456789', 'OTP: 123',
    'abc123456def', 'id_123456', '#123456', '2026-09-16',
    '<style>.a{color:#123456}</style>', '<script>code=123456</script>',
    '<div data-otp="123456">Welcome</div>',
  ]) assert.equal(extractCode(value), null, String(value));
});
