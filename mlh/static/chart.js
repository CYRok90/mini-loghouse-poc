// Minimal SVG charts (no external libraries, CSP script-src 'self').
// Colors come from CSS custom properties; text uses text tokens, never series colors.
"use strict";

const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const W = 560, H = 220, M = { top: 12, right: 12, bottom: 26, left: 44 };
  const PW = W - M.left - M.right, PH = H - M.top - M.bottom;

  function s(tag, attrs = {}, text) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }

  function timeTicks(from, to) {
    const span = to - from;
    const steps = [60e3, 300e3, 600e3, 1800e3, 3600e3, 3 * 3600e3, 6 * 3600e3];
    const step = steps.find((x) => span / x <= 6) || 6 * 3600e3;
    const ticks = [];
    for (let t = Math.ceil(from / step) * step; t <= to; t += step) ticks.push(t);
    return ticks;
  }

  function hhmm(ms) {
    return new Date(ms).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function frame(svg, from, to, yMax, yFmt) {
    const x = (t) => M.left + ((t - from) / (to - from)) * PW;
    const y = (v) => M.top + PH - (v / yMax) * PH;
    for (let i = 0; i <= 4; i += 1) {
      const v = (yMax / 4) * i;
      svg.append(s("line", { x1: M.left, x2: M.left + PW, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }));
      svg.append(s("text", { x: M.left - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--text-muted)" }, yFmt(v)));
    }
    for (const t of timeTicks(from, to)) {
      svg.append(s("text", { x: x(t), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: "var(--text-muted)" }, hhmm(t)));
    }
    return { x, y };
  }

  function shadeUncovered(svg, x, from, to, uncovered) {
    for (const u of uncovered || []) {
      const lo = Math.max(from, u.lo), hi = Math.min(to, u.hi);
      if (hi <= lo) continue;
      svg.append(s("rect", { x: x(lo), y: M.top, width: Math.max(1, x(hi) - x(lo)), height: PH, fill: "var(--uncovered)" }));
      if (x(hi) - x(lo) > 40) {
        svg.append(s("text", { x: (x(lo) + x(hi)) / 2, y: M.top + 14, "text-anchor": "middle", "font-size": 11, fill: "var(--text-muted)" }, u.label || "미수집"));
      }
    }
  }

  function tooltip(wrap) {
    let tip = wrap.querySelector(".tooltip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tooltip";
      tip.hidden = true;
      wrap.append(tip);
    }
    return {
      show(evt, lines) {
        while (tip.firstChild) tip.removeChild(tip.firstChild);
        for (const line of lines) {
          const row = document.createElement("div");
          row.className = "row";
          if (line.color) {
            const sw = document.createElement("span");
            sw.className = "swatch";
            sw.style.background = line.color;
            row.append(sw);
          }
          const strong = document.createElement(line.strong ? "strong" : "span");
          strong.textContent = line.text;
          row.append(strong);
          tip.append(row);
        }
        const box = wrap.getBoundingClientRect();
        tip.hidden = false;
        const left = Math.min(evt.clientX - box.left + 12, box.width - tip.offsetWidth - 4);
        tip.style.left = `${Math.max(0, left)}px`;
        tip.style.top = `${Math.max(0, evt.clientY - box.top - tip.offsetHeight - 8)}px`;
      },
      hide() { tip.hidden = true; },
    };
  }

  function topRoundedBar(x, y, w, h, r) {
    if (h <= 0) return "";
    const rr = Math.min(r, w / 2, h);
    return `M${x},${y + h} V${y + rr} Q${x},${y} ${x + rr},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} V${y + h} Z`;
  }

  function mount(container) {
    while (container.firstChild) container.removeChild(container.firstChild);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
    container.append(svg);
    return svg;
  }

  // Stacked bars: ok (series-1) + failed (status-critical), with a 2px surface gap.
  function stackedBars(container, { buckets, from, to, bucketMs, uncovered, ariaLabel }) {
    const svg = mount(container);
    svg.setAttribute("aria-label", ariaLabel);
    const yMax = niceMax(Math.max(0, ...buckets.map((b) => b.ok + b.failed)));
    const { x, y } = frame(svg, from, to, yMax, (v) => Math.round(v).toString());
    shadeUncovered(svg, x, from, to, uncovered);
    const tip = tooltip(container);
    const bw = Math.max(2, x(from + bucketMs) - x(from) - 2);
    for (const b of buckets) {
      const bx = x(b.t) + 1;
      const okTop = y(b.ok);
      const failTop = y(b.ok + b.failed);
      if (b.ok > 0) svg.append(s("path", { d: topRoundedBar(bx, okTop, bw, M.top + PH - okTop, b.failed > 0 ? 0 : 4), fill: "var(--series-1)" }));
      if (b.failed > 0) svg.append(s("path", { d: topRoundedBar(bx, failTop, bw, okTop - failTop - 2, 4), fill: "var(--status-critical)" }));
      const hit = s("rect", { x: bx - 1, y: M.top, width: bw + 2, height: PH, fill: "transparent", tabindex: 0 });
      const lines = [
        { text: `${hhmm(b.t)} ~ ${hhmm(b.t + bucketMs)}` },
        { text: `${b.ok + b.failed}건 (성공 ${b.ok} · 실패 ${b.failed})`, strong: true },
      ];
      hit.addEventListener("pointermove", (e) => tip.show(e, lines));
      hit.addEventListener("pointerleave", () => tip.hide());
      hit.addEventListener("focus", (e) => tip.show({ clientX: e.target.getBoundingClientRect().x, clientY: e.target.getBoundingClientRect().y + 40 }, lines));
      hit.addEventListener("blur", () => tip.hide());
      svg.append(hit);
    }
  }

  // Single line with crosshair (one series => the title names it, no legend box).
  function line(container, { points, from, to, uncovered, fmt, ariaLabel, label, gapMs = Infinity }) {
    const svg = mount(container);
    svg.setAttribute("aria-label", ariaLabel);
    const yMax = niceMax(Math.max(0, ...points.map((p) => p.v)));
    const { x, y } = frame(svg, from, to, yMax, fmt);
    shadeUncovered(svg, x, from, to, uncovered);
    const sorted = points.slice().sort((a, b) => a.t - b.t);
    if (sorted.length) {
      // Break the line where buckets have no samples instead of bridging the hole.
      const d = sorted.map((p, i) => `${i && p.t - sorted[i - 1].t <= gapMs ? "L" : "M"}${x(p.t)},${y(p.v)}`).join(" ");
      svg.append(s("path", { d, fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round" }));
      for (const p of sorted) svg.append(s("circle", { cx: x(p.t), cy: y(p.v), r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": 2 }));
    }
    const tip = tooltip(container);
    const hair = s("line", { y1: M.top, y2: M.top + PH, stroke: "var(--text-muted)", "stroke-width": 1, visibility: "hidden" });
    svg.append(hair);
    const hit = s("rect", { x: M.left, y: M.top, width: PW, height: PH, fill: "transparent" });
    hit.addEventListener("pointermove", (e) => {
      if (!sorted.length) return;
      const box = svg.getBoundingClientRect();
      const t = from + ((e.clientX - box.left) / box.width * W - M.left) / PW * (to - from);
      const near = sorted.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
      hair.setAttribute("x1", x(near.t));
      hair.setAttribute("x2", x(near.t));
      hair.setAttribute("visibility", "visible");
      tip.show(e, [{ text: hhmm(near.t) }, { text: `${fmt(near.v)}${near.note ? ` (${near.note})` : ""}`, strong: true, color: "var(--series-1)" }, { text: label }]);
    });
    hit.addEventListener("pointerleave", () => { hair.setAttribute("visibility", "hidden"); tip.hide(); });
    svg.append(hit);
  }

  function sparkline(container, { points, from, to }) {
    while (container.firstChild) container.removeChild(container.firstChild);
    const w = 220, h = 44;
    const svg = s("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-hidden": "true" });
    const vals = points.map((p) => p.v);
    const lo = Math.min(...vals, 0), hi = Math.max(...vals, 1);
    const px = (t) => ((t - from) / (to - from)) * (w - 4) + 2;
    const py = (v) => h - 4 - ((v - lo) / (hi - lo || 1)) * (h - 8);
    const sorted = points.slice().sort((a, b) => a.t - b.t);
    if (sorted.length) {
      svg.append(s("path", { d: sorted.map((p, i) => `${i ? "L" : "M"}${px(p.t)},${py(p.v)}`).join(" "), fill: "none", stroke: "var(--series-1)", "stroke-width": 2 }));
    }
    container.append(svg);
  }

  return { stackedBars, line, sparkline };
})();
