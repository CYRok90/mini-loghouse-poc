"use strict";
(() => {
  const { api, el, clear, fmtTime, fmtAge, fmtNum, fmtBytes, stateBadge, errorText, tzLabel } = MLH;
  const $ = (id) => document.getElementById(id);
  const clusterId = window.location.pathname.split("/").pop();
  const params = new URLSearchParams(window.location.search);
  const state = { range: params.get("range") || "1h", node: params.get("node") || "", tab: "recent", detail: null, dash: null };
  let timer = null;

  // ------------------------------------------------------------------ header & nodes
  function renderHead(c) {
    const cov = c.coverage;
    const head = clear($("head"));
    head.append(
      el("h1", { text: c.name }),
      el("p", { class: "sub mono", text: `${c.host}:${c.port} · ${c.tls ? "TLS (remoteSecure)" : "평문 (remote, 로컬 테스트 전용)"} · user ${c.username} · ClickHouse ${c.server_version || "?"}` }),
      el("div", { class: "toolbar" },
        stateBadge(c.state, c.state_label),
        el("span", { class: c.last_check.ok ? "check-ok small" : "check-fail small", text: `연결 확인 ${c.last_check.ok ? "정상" : "실패"} · ${fmtTime(c.last_check.at_ms)}` }),
        el("span", { class: "small", text: `마지막 성공 수집(모든 노드·소스): ${fmtTime(c.last_success_ms)} (${fmtAge(c.last_success_ms)})` }),
        el("span", { class: "badge", text: `수집 범위: ${cov.label}` }),
        el("span", { class: "small muted", text: cov.discovered_cluster ? `${cov.confirmed_nodes}/${cov.candidate_nodes} 노드 수집 확인 (${cov.discovered_cluster})` : `${cov.confirmed_nodes}노드 · 클러스터 구성 미발견` })));
  }

  function sourceCell(src) {
    return el("td", {}, stateBadge(src.state, src.state_label),
      el("div", { class: "small muted", text: src.last_success_ms ? `성공 ${fmtTime(src.last_success_ms)} (${fmtAge(src.last_success_ms)})` : "성공 기록 없음" }),
      src.error ? el("div", { class: "small", text: `${errorText(src.error)} · 연속 ${src.error.consecutive_failures}회` }) : null);
  }

  function renderNodes(c) {
    const rows = c.nodes.map((n) => el("tr", {},
      el("td", {}, el("strong", { text: n.display_name || n.id }), el("div", { class: "small muted mono", text: `${n.host}:${n.port}` })),
      sourceCell(n.sources.metrics), sourceCell(n.sources.query_log),
      el("td", { class: "small", text: n.confirmed ? "수집 확인" : "미확인" })));
    clear($("nodes")).append(el("table", {}, el("thead", {}, el("tr", {}, ["노드 (source displayName)", "system.metrics", "system.query_log", "범위"].map((t) => el("th", { text: t })))), el("tbody", {}, rows)));
    const select = $("node");
    if (select.options.length === 1) {
      c.nodes.forEach((n) => select.append(el("option", { value: n.id, text: `${n.display_name} (${n.host}:${n.port})` })));
      select.value = state.node;
    }
  }

  function renderBanners(c, dash) {
    const box = clear($("banners"));
    const now = Date.now();
    if (!c.last_success_ms && c.state !== "error") {
      box.append(el("div", { class: "banner" }, el("strong", { text: "첫 수집 대기" }), "아직 모든 노드·소스의 첫 수집이 끝나지 않았습니다. 아래 값이 비어 있으면 '미수집'이며 실제 0이 아닙니다."));
    }
    if (c.newest_success_ms && now - c.newest_success_ms > c.stale_after_seconds * 1000) {
      box.append(el("div", { class: "banner warn" }, el("strong", { text: "stale" }), `마지막 성공 수집 후 ${fmtAge(c.newest_success_ms)} — 지연 기준 ${c.stale_after_seconds}초를 넘었습니다. 아래 차트는 과거 수집분입니다.`));
    }
    for (const n of c.nodes) {
      const errs = Object.entries(n.sources).filter(([, s]) => s.state === "error");
      if (errs.length) box.append(el("div", { class: "banner error" }, el("strong", { text: `${n.display_name} 수집 오류` }), errs.map(([k, s]) => `${k}: ${errorText(s.error)}`).join(" / ")));
    }
    if (dash && dash.error) box.append(el("div", { class: "banner error" }, el("strong", { text: "Dashboard 조회 실패" }), dash.error));
  }

  // ------------------------------------------------------------------ coverage helpers
  function uncoveredRanges(dash) {
    const out = [];
    for (const cov of Object.values(dash.coverage)) {
      if (!cov.collected_from_ms) { out.push({ lo: dash.from_ms, hi: dash.to_ms, label: "미수집" }); continue; }
      if (cov.collected_from_ms > dash.from_ms) out.push({ lo: dash.from_ms, hi: cov.collected_from_ms, label: "수집 전" });
      cov.gaps.forEach((g) => out.push({ lo: g.lo_ms, hi: g.hi_ms, label: g.state === "pending" ? "backfill 대기" : "미수집" }));
      if (cov.committed_hi_ms) out.push({ lo: cov.committed_hi_ms, hi: dash.to_ms, label: "flush 대기" });
    }
    return out;
  }

  function anyCoverage(dash) {
    return Object.values(dash.coverage).some((c) => c.collected_from_ms);
  }

  // ------------------------------------------------------------------ tiles & charts
  function tile(label, value, note) {
    return el("div", { class: "tile" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }), el("div", { class: "note", text: note }));
  }

  function renderTiles(dash) {
    const s = dash.queries.summary;
    const covered = anyCoverage(dash);
    const v = (x) => (covered ? fmtNum(x) : "미수집");
    const zeroNote = (x) => (covered && Number(x) === 0 ? "실제 0 (수집 완료 구간 기준)" : "");
    clear($("tiles")).append(
      tile("기록된 initial 쿼리 수", v(s.initial_queries), zeroNote(s.initial_queries) || `QueryStart ${fmtNum(s.query_start_rows)}행은 제외`),
      tile("실패 수", v(s.initial_failed), zeroNote(s.initial_failed) || "Exception* (initial)"),
      tile("노드 실행 이벤트 수", v(s.node_events), "분산 하위 쿼리 포함 (노드별 실행)"),
      tile("성공 쿼리 지연 p95", covered && s.p95_ms !== null ? `${fmtNum(s.p95_ms)} ms` : covered ? "—" : "미수집",
        covered ? `표본 ${fmtNum(s.p95_samples)}건${s.p95_ms === null ? " (성공 쿼리 없음)" : ""}` : ""));
  }

  function renderCharts(dash) {
    const bucketMs = dash.bucket_seconds * 1000;
    const uncovered = uncoveredRanges(dash);
    const buckets = dash.queries.series.map((r) => ({ t: r.bucket * 1000, ok: r.queries - r.failed, failed: r.failed }));
    Charts.stackedBars($("chart-count"), { buckets, from: dash.from_ms, to: dash.to_ms, bucketMs, uncovered,
      ariaLabel: `시간대별 initial 쿼리 수, ${buckets.length}개 버킷` });
    const points = dash.queries.series.filter((r) => r.p95_ms !== null).map((r) => ({ t: r.bucket * 1000 + bucketMs / 2, v: r.p95_ms, note: `표본 ${r.ok_samples}` }));
    Charts.line($("chart-p95"), { points, from: dash.from_ms, to: dash.to_ms, uncovered, fmt: (x) => `${Math.round(x)}`,
      ariaLabel: "성공 쿼리 지연 p95 추이", label: "성공 쿼리 지연 p95 (ms)", gapMs: bucketMs * 1.5 });
  }

  function renderMetrics(dash) {
    const box = clear($("metrics"));
    const staleMs = (state.detail ? state.detail.stale_after_seconds : 60) * 1000;
    for (const name of dash.metrics.curated) {
      const cur = dash.metrics.current.filter((r) => r.metric === name);
      const total = cur.reduce((a, r) => a + Number(r.value), 0);
      const at = cur.length ? Math.max(...cur.map((r) => r.at_ms)) : null;
      const fmt = name === "MemoryTracking" ? fmtBytes : fmtNum;
      const spark = el("div", { class: "chart-wrap" });
      const points = dash.metrics.series.filter((r) => r.metric === name).map((r) => ({ t: r.bucket * 1000, v: Number(r.value) }));
      Charts.sparkline(spark, { points, from: dash.from_ms, to: dash.to_ms });
      const stale = at && Date.now() - at > staleMs;
      box.append(el("div", { class: "metric" }, el("div", { class: "name mono", text: name }),
        el("div", { class: "now", text: cur.length ? fmt(total) : "데이터 없음" }),
        el("div", { class: "at", text: cur.length ? `${cur.length}개 노드 · ${fmtTime(at)}${stale ? " · stale" : ""}` : "이 지표가 수집되지 않았습니다" }), spark));
    }
  }

  // ------------------------------------------------------------------ query tables
  function queryRow(r) {
    const sql = el("details", { class: "sql" }, el("summary", { text: "SQL 보기 (최대 2,000자)" }), el("pre", { text: r.query }));
    const result = r.exception_code ? el("div", {}, el("span", { class: "check-fail", text: `code ${r.exception_code}` }), el("div", { class: "small", text: r.exception })) : el("span", { class: "small muted", text: `읽은 행 ${fmtNum(r.read_rows)}` });
    return el("tr", {},
      el("td", { text: r.source_display_name }), el("td", { class: "small", text: fmtTime(r.ts_ms, true) }),
      el("td", { class: "mono", text: r.query_id }), el("td", { class: "small", text: `${r.type}${r.is_initial_query ? "" : " (하위)"}` }),
      el("td", { class: "num", text: `${fmtNum(r.query_duration_ms)} ms` }), el("td", {}, result), el("td", {}, sql));
  }

  function renderTable(dash) {
    const rows = dash.queries.tables[state.tab] || [];
    const box = clear($("table"));
    if (!anyCoverage(dash)) { box.append(el("p", { class: "empty", text: "미수집 — 아직 수집된 구간이 없습니다." })); return; }
    if (!rows.length) { box.append(el("p", { class: "empty", text: "선택 기간·노드에 해당 쿼리가 없습니다 (수집 완료 구간 기준 실제 0건)." })); return; }
    const head = el("tr", {}, ["출처 노드", "이벤트 시각", "query_id", "이벤트 종류", "실행 시간", "결과 / 오류", "SQL"].map((t) => el("th", { text: t, class: t === "실행 시간" ? "num" : "" })));
    box.append(el("table", {}, el("thead", {}, head), el("tbody", {}, rows.map(queryRow))));
  }

  // ------------------------------------------------------------------ load loop
  async function load() {
    document.querySelectorAll(".chart-wrap").forEach((w) => w.classList.add("loading"));
    const q = new URLSearchParams({ range: state.range });
    if (state.node) q.set("node", state.node);
    const [detail, dash] = await Promise.all([api(`/api/clusters/${clusterId}`), api(`/api/clusters/${clusterId}/dashboard?${q}`)]);
    document.querySelectorAll(".chart-wrap").forEach((w) => w.classList.remove("loading"));
    if (!detail.ok) {
      clear($("banners")).append(el("div", { class: "banner error" }, el("strong", { text: "클러스터 정보를 불러오지 못했습니다." }), state.detail ? "마지막 화면을 유지합니다 (stale)." : `HTTP ${detail.status}`));
      return;
    }
    state.detail = detail.data;
    renderHead(detail.data);
    renderNodes(detail.data);
    if (dash.ok) {
      state.dash = dash.data;
      renderTiles(dash.data); renderCharts(dash.data); renderMetrics(dash.data); renderTable(dash.data);
    }
    renderBanners(detail.data, dash.ok ? null : { error: `HTTP ${dash.status} — 이전 화면 유지` });
    const lagNote = dash.ok ? `최근 ${dash.data.safety_lag_seconds}초는 flush 여유로 아직 수집 전` : "";
    $("freshness").textContent = `표시 시간대 ${tzLabel()} · 화면 갱신 ${fmtTime(Date.now())} · ${lagNote}`;
  }

  function syncControls() {
    document.querySelectorAll("#ranges button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === state.range)));
    const q = new URLSearchParams({ range: state.range });
    if (state.node) q.set("node", state.node);
    window.history.replaceState(null, "", `?${q}`);
  }

  function setAuto(on) {
    if (timer) clearInterval(timer);
    timer = on ? setInterval(load, 10000) : null;
  }

  document.querySelectorAll("#ranges button").forEach((b) => b.addEventListener("click", () => { state.range = b.dataset.range; syncControls(); load(); }));
  $("node").addEventListener("change", (e) => { state.node = e.target.value; syncControls(); load(); });
  $("refresh").addEventListener("click", load);
  $("auto").addEventListener("change", (e) => setAuto(e.target.checked));
  document.querySelectorAll("[role=tab]").forEach((b) => b.addEventListener("click", () => {
    state.tab = b.dataset.tab;
    document.querySelectorAll("[role=tab]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    if (state.dash) renderTable(state.dash);
  }));
  syncControls();
  load();
  setAuto(true);
})();
