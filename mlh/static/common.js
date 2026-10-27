// Shared helpers. Every dynamic string is inserted with textContent (never innerHTML):
// cluster names, SQL text and server error messages are untrusted data.
"use strict";

const MLH = (() => {
  const JSON_HEADERS = { "Content-Type": "application/json", "X-MLH-Request": "1" };

  async function api(path, options = {}) {
    const method = options.method || (options.body !== undefined ? "POST" : "GET");
    const init = { method, headers: {}, cache: "no-store" };
    if (options.body !== undefined) {
      init.headers = { ...JSON_HEADERS };
      init.body = JSON.stringify(options.body);
    } else if (init.method !== "GET") {
      init.headers = { "X-MLH-Request": "1" };
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (_) {
      return { ok: false, status: 0, data: null };
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  const tzName = Intl.DateTimeFormat().resolvedOptions().timeZone;
  function tzLabel() {
    const offset = -new Date().getTimezoneOffset();
    const sign = offset >= 0 ? "+" : "-";
    const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
    const mm = String(Math.abs(offset) % 60).padStart(2, "0");
    return `${tzName} (UTC${sign}${hh}:${mm})`;
  }

  function fmtTime(ms, withDate = false) {
    if (!ms) return "—";
    const d = new Date(ms);
    const opts = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
    if (withDate) Object.assign(opts, { year: "numeric", month: "2-digit", day: "2-digit" });
    return d.toLocaleString("ko-KR", opts);
  }

  function fmtAge(ms) {
    if (!ms) return "—";
    const sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (sec < 60) return `${sec}초 전`;
    if (sec < 3600) return `${Math.floor(sec / 60)}분 ${sec % 60}초 전`;
    return `${Math.floor(sec / 3600)}시간 ${Math.floor((sec % 3600) / 60)}분 전`;
  }

  function fmtNum(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString("ko-KR");
  }

  function fmtBytes(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let v = Math.abs(n);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${n < 0 ? "-" : ""}${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  // Collection states (server vocabulary) -> badge with icon + label (never color alone).
  const STATE_ICON = { collecting: "●", waiting_first: "◌", preparing: "…", delayed: "▲", error: "✕" };
  function stateBadge(state, label) {
    return el("span", { class: `badge state-${state}` }, el("span", { class: "badge-icon", "aria-hidden": "true", text: STATE_ICON[state] || "?" }), label);
  }

  function errorText(error) {
    if (!error) return "";
    const codes = error.codes && error.codes.length ? ` (code ${error.codes.join(", ")})` : "";
    return `${error.message}${codes}`;
  }

  return { api, el, clear, tzLabel, fmtTime, fmtAge, fmtNum, fmtBytes, stateBadge, errorText };
})();
