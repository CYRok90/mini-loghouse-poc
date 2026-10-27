"use strict";
(() => {
  const { api, el, clear, fmtTime, fmtAge, stateBadge, errorText, tzLabel } = MLH;
  const listNode = document.getElementById("list");
  const banner = document.getElementById("banner");
  let lastOk = null;

  function coverageBadge(cov) {
    const cls = cov.label === "전체 검증됨" ? "coverage-full" : cov.label === "일부" ? "coverage-partial" : "";
    const detail = cov.discovered_cluster
      ? `수집 확인 ${cov.confirmed_nodes} / 발견 ${cov.candidate_nodes}노드 (${cov.discovered_cluster})`
      : `수집 확인 ${cov.confirmed_nodes}노드 · 클러스터 구성 미발견 (endpoint 범위)`;
    return el("div", {}, el("span", { class: `badge ${cls}`, text: cov.label }), el("div", { class: "small muted", text: detail }));
  }

  function checkCell(check) {
    const label = check.ok ? "연결 정상" : "연결 실패";
    const src = check.source === "collection" ? "수집 시도 기준" : "등록 시 검사";
    return el("div", {}, el("span", { class: check.ok ? "check-ok" : "check-fail", text: label }),
      el("div", { class: "small muted", text: `${fmtTime(check.at_ms)} · ${src}` }));
  }

  function row(c) {
    const success = c.last_success_ms;
    const stale = success && Date.now() - success > c.stale_after_seconds * 1000;
    return el("tr", {},
      el("td", {}, el("a", { href: `/clusters/${c.id}`, text: c.name }),
        el("div", { class: "small muted mono", text: `${c.host}:${c.port} ${c.tls ? "TLS" : "평문"}` })),
      el("td", {}, checkCell(c.last_check)),
      el("td", {}, stateBadge(c.state, c.state_label)),
      el("td", {}, el("div", { text: fmtTime(success) }),
        el("div", { class: `small ${stale ? "" : "muted"}`, text: success ? `${fmtAge(success)}${stale ? " · 지연 기준 초과" : ""}` : "아직 없음" })),
      el("td", {}, coverageBadge(c.coverage)),
      el("td", { class: "small", text: c.latest_error ? errorText(c.latest_error) : "—" }),
      el("td", {}, el("a", { href: `/clusters/${c.id}`, text: "Dashboard →" })));
  }

  function render(data, health) {
    clear(listNode);
    if (!data.clusters.length) {
      listNode.append(el("div", { class: "empty" }, el("p", { text: "등록된 클러스터가 없습니다." }),
        el("a", { class: "button primary", href: "/clusters/new", text: "클러스터 추가" })));
      return;
    }
    const head = el("tr", {}, ...["이름 / endpoint", "최근 연결 확인", "수집 상태", "마지막 성공 수집 (모든 노드·소스)", "노드 수집 범위", "최근 오류", ""].map((t) => el("th", { text: t })));
    listNode.append(el("table", {}, el("thead", {}, head), el("tbody", {}, data.clusters.map(row))));
    document.getElementById("policy").textContent =
      `수집 주기 ${health.refresh_seconds}초 · 마지막 성공 후 ${health.stale_after_seconds}초(주기×3 + flush 여유 ${health.safety_lag_seconds}초)가 지나면 '수집 지연'. 연결 성공·수집 성공·클러스터 상태는 서로 다릅니다.`;
  }

  async function load() {
    const [list, health] = await Promise.all([api("/api/clusters"), api("/api/health")]);
    clear(banner);
    if (!list.ok || !health.ok) {
      banner.append(el("div", { class: "banner error" }, el("strong", { text: "목록을 불러오지 못했습니다." }),
        lastOk ? `마지막으로 받은 목록(${fmtTime(lastOk)})을 표시 중입니다 — stale.` : "backend가 실행 중인지 확인하세요."));
      return;
    }
    if (health.data.mode === "recovery") {
      banner.append(el("div", { class: "banner error" }, el("strong", { text: "복구 모드" }), health.data.recovery_reason || ""));
    }
    lastOk = Date.now();
    render(list.data, health.data);
    document.getElementById("footer").textContent = `표시 시간대 ${tzLabel()} · 화면 갱신 ${fmtTime(lastOk)} (10초마다 자동 갱신, 화면 갱신은 수집기를 재생성하지 않음)`;
  }

  load();
  setInterval(load, 10000);
})();
