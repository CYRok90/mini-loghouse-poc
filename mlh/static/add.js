"use strict";
(() => {
  const { api, el, clear, errorText } = MLH;
  const $ = (id) => document.getElementById(id);
  const fields = ["name", "host", "port", "tls", "username", "password"];
  const CHECK_LABEL = { metrics: "system.metrics (필수)", query_log: "system.query_log (필수)", clusters: "system.clusters (노드 범위 확인, 선택)" };
  let lastProbe = null;

  function formValues() {
    return {
      name: $("name").value.trim(), host: $("host").value.trim(), port: Number($("port").value),
      tls: $("tls").checked, username: $("username").value, password: $("password").value,
    };
  }

  function invalidate() {
    lastProbe = null;
    $("save").disabled = true;
  }
  fields.forEach((f) => $(f).addEventListener("input", invalidate));

  function setBusy(text) {
    $("busy").textContent = text;
    $("test").disabled = Boolean(text);
  }

  function checkLine(label, check) {
    if (!check) return el("li", { class: "check-skip", text: `${label}: 접속 실패로 검사하지 않음` });
    if (check.ok) {
      const rows = Array.isArray(check.rows) ? `${check.rows.length}행` : `${check.rows}행`;
      return el("li", { class: "check-ok", text: `${label}: 조회 가능 (${rows})` });
    }
    return el("li", { class: "check-fail", text: `${label}: ${errorText(check)}` });
  }

  function connectBlock(target) {
    const c = target.connect || {};
    const head = c.ok
      ? el("p", { class: "check-ok", text: `접속·인증 성공 — ClickHouse ${c.version}, 서버 표시 이름 ${c.display_name}, 서버 시간대 ${c.timezone}` })
      : el("p", { class: c.connected ? "check-ok" : "check-fail", text: c.connected ? `접속·인증은 되었으나 ${errorText(c)}` : `접속 실패: ${errorText(c)}` });
    const checks = target.checks || {};
    return el("div", {}, head, el("ul", {}, Object.keys(CHECK_LABEL).map((k) => checkLine(CHECK_LABEL[k], checks[k]))));
  }

  function nodesBlock(probe) {
    const v = formValues();
    const rows = [el("tr", {}, el("td", {}, el("input", { type: "checkbox", checked: true, disabled: true })),
      el("td", { class: "mono", text: `${v.host}:${v.port}` }), el("td", { text: "등록 endpoint" }),
      el("td", { class: probe.ok ? "check-ok" : "check-fail", text: probe.ok ? "필수 검사 통과" : "필수 검사 실패" }))];
    for (const cand of probe.candidates) {
      const box = el("input", { type: "checkbox", class: "cand", "data-host": cand.host, "data-port": cand.port, checked: cand.required_ok, disabled: !cand.required_ok });
      const status = cand.required_ok
        ? `필수 검사 통과 (${cand.connect.display_name}, ${cand.connect.version})`
        : `제외: ${errorText(cand.connect.ok ? Object.values(cand.checks || {}).find((x) => !x.ok) : cand.connect)}`;
      rows.push(el("tr", {}, el("td", {}, box), el("td", { class: "mono", text: `${cand.host}:${cand.port}` }),
        el("td", { text: cand.cluster || "" }), el("td", { class: cand.required_ok ? "check-ok" : "check-fail", text: status })));
    }
    const summary = probe.discovered_cluster
      ? `system.clusters에서 endpoint가 속한 클러스터 '${probe.discovered_cluster}'의 노드 ${probe.candidate_count}개를 발견했습니다. 같은 계정으로 각 노드를 직접 검사했습니다.`
      : "클러스터 구성을 확인하지 못했습니다. endpoint 하나만 수집하며 수집 범위는 '미확인'으로 표시됩니다.";
    return el("div", {}, el("h2", { text: "확인된 노드 범위" }), el("p", { class: "small", text: summary }),
      el("table", {}, el("thead", {}, el("tr", {}, ["수집", "주소", "출처", "결과"].map((t) => el("th", { text: t })))), el("tbody", {}, rows)));
  }

  function renderProbe(probe) {
    const box = clear($("result"));
    box.hidden = false;
    if (probe.fatal) {
      box.append(el("p", { class: "check-fail", text: probe.fatal.message }));
      return;
    }
    box.append(el("h2", { text: "연결 테스트 결과" }), connectBlock(probe.endpoint));
    if (!probe.ok) {
      box.append(el("div", { class: "banner warn" }, el("strong", { text: "등록할 수 없습니다." }),
        "필수 수집 권한(system.metrics, system.query_log)이 확인되어야 저장할 수 있습니다. 입력값이나 계정 권한을 수정한 뒤 다시 연결 테스트를 하세요."));
    }
    if (probe.endpoint.connect && probe.endpoint.connect.ok) box.append(nodesBlock(probe));
  }

  async function runTest() {
    const values = formValues();
    setBusy("연결 검사 중… (최대 수십 초)");
    $("save").disabled = true;
    const res = await api("/api/probe", { body: values });
    setBusy("");
    if (!res.ok) {
      renderProbe({ fatal: { message: res.data && res.data.error ? fieldErrors(res.data.error) : `요청 실패 (HTTP ${res.status})` } });
      return;
    }
    lastProbe = res.data;
    renderProbe(res.data);
    $("save").disabled = !res.data.ok;
  }

  function fieldErrors(error) {
    const parts = (error.fields || []).map((f) => `${f.field}: ${f.message}`);
    return [error.message, ...parts].join(" · ");
  }

  async function save() {
    if (!lastProbe || !lastProbe.ok) return;
    $("save").disabled = true;
    setBusy("저장 및 수집 구성 중…");
    const values = formValues();
    const nodes = [{ host: values.host, port: values.port }];
    document.querySelectorAll("input.cand:checked").forEach((b) => nodes.push({ host: b.dataset.host, port: Number(b.dataset.port) }));
    const res = await api("/api/clusters", { body: { ...values, nodes } });
    $("password").value = "";  // never keep the secret in the page after submission
    invalidate();
    setBusy("");
    if (res.ok) {
      window.location.assign(`/clusters/${res.data.id}`);
      return;
    }
    const err = res.data && res.data.error;
    const msg = err ? `${fieldErrors(err)}` : `저장 실패 (HTTP ${res.status})`;
    clear($("result")).append(el("div", { class: "banner error" }, el("strong", { text: "저장하지 못했습니다." }), msg, " 비밀번호를 다시 입력하고 연결 테스트부터 진행하세요."));
    $("result").hidden = false;
  }

  $("test").addEventListener("click", runTest);
  $("save").addEventListener("click", save);
})();
