// One explicit login attempt with redacted form/response diagnostics.
import { MailComWebAliasAddon } from "maildotcom-sdk/web-aliases";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const credentials = JSON.parse(input);
const events = [];
const clean = (value) => String(value).replaceAll(credentials.password, "[password]")
  .replaceAll(credentials.email, "[account]").replaceAll(credentials.email.toLowerCase(), "[account]");
const pageText = (html) => clean(html.replace(/<(script|style|svg)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
  .replace(/<[^>]*>/g, " ").replace(/&(?:nbsp|amp|quot);/g, " ").replace(/\s+/g, " ").trim()).slice(0, 1800);
const inspectFetch = async (url, options = {}) => {
  const parsed = new URL(url);
  const event = { method: options.method || "GET", host: parsed.host, path: parsed.pathname };
  if (parsed.host === "login.mail.com" && parsed.pathname === "/login") {
    const parameters = options.body;
    event.fields = [...parameters.keys()];
    event.service = parameters.get("service");
    event.usernameMatchesInput = parameters.get("username") === credentials.email;
    event.passwordMatchesInput = parameters.get("password") === credentials.password;
  }
  const response = await fetch(url, { ...options, signal: AbortSignal.timeout(20_000) });
  event.status = response.status;
  const location = response.headers.get("location");
  if (location) {
    const target = new URL(location, url);
    event.redirect = `${target.origin}${target.pathname}`;
    event.statusFlag = target.searchParams.get("status");
  }
  if (response.status === 200 && ["mlogin.mail.com", "auth.mail.com"].includes(parsed.host)) {
    const html = await response.clone().text();
    event.text = pageText(html);
    event.forms = [...html.matchAll(/<form\b[^>]*>/gi)].map((match) => clean(match[0]
      .replace(/(action=["'])([^"']+)/gi, (_full, prefix, address) => {
        try { const target = new URL(address.replaceAll("&amp;", "&"), url); return prefix + target.origin + target.pathname; }
        catch { return prefix + "[relative]"; }
      })));
    event.inputs = [...html.matchAll(/<input\b[^>]*>/gi)].map((match) => ({
      name: match[0].match(/\bname=["']([^"']*)/i)?.[1],
      type: match[0].match(/\btype=["']([^"']*)/i)?.[1],
    }));
    event.externalScriptHosts = [...html.matchAll(/<script[^>]*src=["']([^"']+)/gi)].map((match) => {
      try { return new URL(match[1], url).host; } catch { return "relative"; }
    });
  }
  events.push(event);
  return response;
};
try {
  const addon = new MailComWebAliasAddon({ email: credentials.email, password: credentials.password, fetch: inspectFetch });
  const domains = await addon.availableDomains();
  console.log(JSON.stringify({ success: true, domains, events }));
} catch (error) {
  console.log(JSON.stringify({ success: false, errorType: error.name, events }));
  process.exitCode = 1;
}
