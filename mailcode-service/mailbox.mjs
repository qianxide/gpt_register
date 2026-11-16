// Intentionally dependency-free: the caller owns the authenticated SDK client.
const ADDRESS = /^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$/i;

function address(value) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase();
  return ADDRESS.test(normalized) ? normalized : null;
}

// Parse the SDK's string/string[] headers, ignoring display names and comments.
// Quoted local parts and malformed/ambiguous headers fail closed. Mail.com
// aliases use ordinary addr-specs; plus tags and dots are never removed.
function recipients(value) {
  if (Array.isArray(value)) return value.flatMap(recipients);
  if (typeof value !== 'string') return [];
  const header = value.replace(/\r?\n[\t ]+/g, ' ');
  if (/[\r\n\0]/.test(header)) return [];

  const parts = [];
  let part = '';
  let quoted = false;
  let escaped = false;
  let comments = 0;
  let angle = false;
  let group = false;
  const flush = () => { parts.push(part.trim()); part = ''; };

  for (const char of header) {
    if (escaped) { escaped = false; continue; }
    if (comments) {
      if (char === '\\') escaped = true;
      else if (char === '(') comments += 1;
      else if (char === ')') comments -= 1;
      continue;
    }
    if (quoted) {
      if (char === '\\') escaped = true;
      else if (char === '"') quoted = false;
      continue;
    }
    if (char === '"') { quoted = true; part += '""'; }
    else if (char === '(') { comments = 1; part += ' '; }
    else if (char === ')' || char === '\\') return [];
    else if (char === '<') {
      if (angle) return [];
      angle = true;
      part += char;
    } else if (char === '>') {
      if (!angle) return [];
      angle = false;
      part += char;
    } else if (char === ':' && !angle) {
      if (group || /[<>]/.test(part)) return [];
      group = true;
      part = ''; // A group name is not a recipient, even if it resembles one.
    } else if ((char === ',' || char === ';') && !angle) {
      flush();
      if (char === ';') group = false;
    } else part += char;
  }
  if (quoted || escaped || comments || angle || group) return [];
  flush();

  return parts.flatMap((item) => {
    const match = item.match(/^(?:[^<>]*<([^<>]+)>|([^<>]+))$/);
    const parsed = match && address(match[1] ?? match[2]);
    return parsed ? [parsed] : [];
  });
}

function searchableText(value) {
  if (typeof value !== 'string') return '';
  const entities = { nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", ensp: ' ', emsp: ' ', thinsp: ' ', ndash: '-', mdash: '-' };
  return value
    .replace(/<!--[\s\S]*?(?:-->|$)/g, ' ')
    .replace(/<(script|style)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gi, ' ')
    .replace(/<\/?[a-z][^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>/gi, ' ')
    .replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, name) => {
      if (name[0] !== '#') return entities[name.toLowerCase()] ?? ' ';
      const hex = name[1].toLowerCase() === 'x';
      const point = Number.parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10);
      return point > 0 && point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff)
        ? String.fromCodePoint(point) : ' ';
    })
    .replace(/\s+/g, ' ')
    .trim();
}

/** Extract a 4–8 digit OTP from plain text or HTML, retaining leading zeroes.
 * Contextual codes take priority; otherwise prefer the Python adapter's common
 * six-digit format before other standalone lengths. Return null when absent.
 */
export function extractCode(text) {
  const content = searchableText(text);
  return contextualCode(content) ?? standaloneCode(content);
}

function contextualCode(content) {
  const context = /(?:\b(?:verification(?:\s+code)?|security(?:\s+code)?|authentication(?:\s+code)?|one[- ]time(?:\s+(?:password|code))?|code|otp|pin|temporary)\b|验证码|校验码|代码为)[^\d]{0,80}(?<![\w#&])((?:\d{3}[ -]\d{3}|\d{4}[ -]\d{4}|\d(?:[ -]\d){3,7})(?![ -]?\d)|\d{4,8})(?![\w-])/gi;
  const contextual = context.exec(content);
  return contextual ? contextual[1].replace(/\D/g, '') : null;
}

function standaloneCode(content) {
  return content.match(/(?<![\w#&-])(\d{6})(?![\w-])/)?.[1]
    ?? content.match(/(?<![\w#&-])(\d{4,8})(?![\w-])/)?.[1]
    ?? null;
}

function timestamp(value) {
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.getTime() / 1000 : null;
  if (typeof value === 'string') {
    value = value.trim();
    if (!value) return null;
    if (!/^\d+(?:\.\d+)?$/.test(value)) {
      // Restrict Date.parse to date-shaped strings (not arbitrary numeric text).
      if (!/^\d{4}-\d{2}-\d{2}T|^(?:[A-Za-z]{3},?\s)?\d{1,2}\s[A-Za-z]{3}\s\d{4}\s/.test(value)) return null;
      const millis = Date.parse(value);
      return Number.isFinite(millis) ? millis / 1000 : null;
    }
    value = Number(value);
  }
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  return value >= 100_000_000_000 ? value / 1000 : value;
}

function optionNumber(value, fallback, name) {
  if (value === undefined) return fallback;
  if ((typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') {
    throw new TypeError(`${name} must be a non-negative finite number`);
  }
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) throw new RangeError(`${name} must be a non-negative finite number`);
  return number;
}

function messageId(message) {
  const raw = [message.attribute?.mailIdentifier, message.mailURI]
    .find((value) => typeof value === 'string' && value.trim());
  if (!raw) return null;
  let decoded = raw.trim();
  try { decoded = decodeURIComponent(decoded); } catch { /* SDK also keeps undecodable IDs. */ }
  // Same Mail URI normalization as maildotcom-sdk@1.0.9, without an SDK import.
  return decoded.match(/(?:^|\/)Mail\/([^/?#]+)/)?.[1]
    ?? (decoded.replace(/^(\.\.\/)*Mail\//, '').replace(/^\/+/, '') || null);
}

/**
 * Fetch one snapshot using maildotcom-sdk@1.0.9; errors propagate to the caller.
 * `after` is an inclusive Unix-seconds boundary. It never overrides the age cap.
 * `limit` defaults to 10, is floored/capped at 20; zero returns an empty snapshot.
 * `received_at` is Unix seconds (fractional seconds are retained), `to` is the
 * unique, lowercase array of parsed To/Cc recipients, and `code` is string|null.
 * Excluded OTP messages are omitted entirely, including their subject and text.
 *
 * @param {object} client An already authenticated SDK client.
 * @param {string} aliasAddress One bare alias address.
 * @param {{after?: number, maxAgeSeconds?: number, limit?: number, exclude?: (string|number)[]}} options
 * @returns {Promise<Array<{id:string, subject:string, sender:string, to:string[], received_at:number, text:string, code:string|null}>>}
 */
export async function fetchMailboxMessages(client, aliasAddress, options = {}) {
  const alias = address(aliasAddress);
  if (!alias) throw new TypeError('aliasAddress must be one bare email address');
  const after = optionNumber(options.after, 0, 'after');
  const maxAge = optionNumber(options.maxAgeSeconds, 600, 'maxAgeSeconds');
  const limit = Math.min(20, Math.floor(optionNumber(options.limit, 10, 'limit')));
  if (options.exclude !== undefined && !Array.isArray(options.exclude)) throw new TypeError('exclude must be an array of codes');
  const excluded = new Set((options.exclude ?? []).map((code) => String(code).trim()));
  if (limit === 0) return [];

  const now = Date.now() / 1000;
  const cutoff = Math.max(after, now - maxAge);
  const incoming = await client.mail.listIncoming({ amount: 50, includeSpam: true, tagsShowAll: true });
  if (!Array.isArray(incoming?.mail)) throw new TypeError('mail.listIncoming() must return a mail array');

  const candidates = [];
  for (const message of incoming.mail) {
    const header = message?.mailHeader;
    if (!header) continue;
    const to = [...new Set([...recipients(header.to), ...recipients(header.cc)])];
    if (!to.includes(alias)) continue;
    const received_at = timestamp(header.date);
    if (received_at === null || received_at < cutoff || received_at > now) continue;
    const id = messageId(message);
    if (!id) continue;
    const subject = typeof header.subject === 'string' ? header.subject : '';
    if (excluded.has(extractCode(subject))) continue;
    candidates.push({ id, subject, sender: typeof header.from === 'string' ? header.from : '', to, received_at });
  }
  candidates.sort((a, b) => b.received_at - a.received_at);

  const result = [];
  const seen = new Set();
  for (const candidate of candidates) {
    if (seen.has(candidate.id)) continue;
    seen.add(candidate.id);
    const body = await client.mail.getBody(candidate.id, { markRead: false });
    const text = searchableText(body);
    const subjectText = searchableText(candidate.subject);
    // Prefer a labeled OTP in either field over incidental standalone numbers.
    // Parse fields separately so split digits never join across subject/body.
    const code = contextualCode(subjectText) ?? contextualCode(text)
      ?? standaloneCode(subjectText) ?? standaloneCode(text);
    if (excluded.has(code)) continue;
    result.push({ ...candidate, text, code });
    if (result.length === limit) break;
  }
  return result;
}
