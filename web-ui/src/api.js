const configuredApiBase = String(import.meta.env.VITE_API_BASE_URL || "").trim().replace(/\/+$/, "");

// Accept either an API origin (https://api.example.com) or an API path
// (/api), while keeping existing /api/... call sites unchanged.
export function apiUrl(path) {
  const value = String(path || "");
  if (!value) return configuredApiBase || "/";
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)) return value;
  if (!configuredApiBase) return value;
  if ((configuredApiBase === "/api" || configuredApiBase.endsWith("/api")) && value === "/api") {
    return configuredApiBase;
  }
  if ((configuredApiBase === "/api" || configuredApiBase.endsWith("/api")) && value.startsWith("/api/")) {
    return `${configuredApiBase}${value.slice(4)}`;
  }
  return `${configuredApiBase}${value.startsWith("/") ? value : `/${value}`}`;
}

function formatApiError(data, fallback) {
  const detail = data?.detail || data?.message || data?.error || fallback;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      const location = Array.isArray(item?.loc) ? item.loc.filter(Boolean).join(".") : "";
      const message = String(item?.msg || item?.message || "").trim();
      return location && message ? location + ": " + message : message || String(item || "");
    }).filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  if (detail && typeof detail === "object") {
    const message = String(detail.message || detail.msg || "").trim();
    if (message) return message;
  }
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

export async function api(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401) {
    location.replace("/login");
    throw new Error("登录会话已过期");
  }
  if (!response.ok) {
    const error = new Error(formatApiError(data, response.statusText));
    error.status = response.status;
    error.code = typeof data?.code === "string" ? data.code : "";
    throw error;
  }
  return data;
}

export function jsonBody(value) {
  return JSON.stringify(value);
}

export async function downloadPost(path, payload, filename) {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await response.text());
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
  return {
    exported: Number(response.headers.get("X-Exported-Count") || 0),
    missing: Number(response.headers.get("X-Missing-Count") || 0),
  };
}
