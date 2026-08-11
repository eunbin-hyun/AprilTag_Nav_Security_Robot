// 텔레메트리 화면 JS를 진짜로 돌려 보는 스텁 DOM 하네스.
//
// 왜 필요한가 — 화면 시험이 전부 `assert "..." in body` 문자열 검사라, 위임 핸들러가
// 실제로 도는지·409 갈래가 갈리는지·다른 화면이 적은 신원이 이쪽 버튼에 반영되는지가
// 증명이 안 됐다. `node --check`는 문법만 보고 넘어간다.
//
// 쓰는 법 — `node telemetry_harness.js <telemetry.html 경로>`. 결과를 JSON 한 줄로 찍는다.
// 실패는 exit 1 + stderr.
"use strict";

const fs = require("fs");
const path = process.argv[2];
const html = fs.readFileSync(path, "utf8");

// ── 스텁 DOM ────────────────────────────────────────────────────────────────

class El {
  constructor(tag, id) {
    this.tagName = (tag || "DIV").toUpperCase();
    this.id = id || "";
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.title = "";
    this._html = "";
    this.children = [];
    this.options = [];
    this.parentNode = null;
    this.listeners = Object.create(null);
    this.attrs = Object.create(null);
    this.focused = 0;
  }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  addEventListener(name, fn) {
    (this.listeners[name] = this.listeners[name] || []).push(fn);
  }
  fire(name, evt) {
    (this.listeners[name] || []).forEach((fn) => fn(evt || {}));
  }
  appendChild(node) {
    this.children.push(node);
    if (this.tagName === "SELECT") { this.options.push(node); }
    node.parentNode = this;
    return node;
  }
  focus() { this.focused += 1; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

const byId = Object.create(null);
global.document = {
  getElementById(id) {
    if (!byId[id]) { byId[id] = new El(id.startsWith("s-") ? "div" : "div", id); }
    return byId[id];
  },
  createElement(tag) { return new El(tag); },
  createTextNode(text) { const n = new El("#text"); n.textContent = text; return n; },
};
// select·button은 태그가 중요해서 미리 심어 둔다(위임 핸들러가 tagName을 본다).
byId["filter"] = new El("select", "filter");

const storage = Object.create(null);
global.window = {
  sessionStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
  },
};
global.location = { protocol: "http:", host: "127.0.0.1:8000" };

// ── 스텁 fetch ──────────────────────────────────────────────────────────────

const fetchCalls = [];
let nextResponses = [];   // {status, body} 큐. 시나리오 중간에 한 번만 쓰는 답을 넣는다.
// 부팅 때 나가는 조회는 **주소로** 답을 고른다. 순서 큐로 두면 화면이 호출을 하나 더하거나
// 순서를 바꾸는 순간 조용히 한 칸씩 밀린다.
//
// ⚠ 2026-08-04 실사고 — 화면이 `loadDispatchState()`로 `/api/dispatch/state`를 **먼저** 부르는데
//   하네스는 그 호출을 모르고 큐를 다섯 개만 준비했다. 그래서 스냅샷 답이 dispatch 로,
//   events 답이 스냅샷으로… 한 칸씩 밀렸고 `got.untagged`가 today 통계 **객체**를 받아
//   `got.untagged.slice is not a function`으로 죽었다. 큐가 비어 {}가 온 게 아니라 **엉뚱한
//   답이 제자리에 앉은 것**이라, 겉으로는 "재조회가 큐를 다 먹었다"처럼 보였다.
let routeResponses = [];  // [{match: (url)=>bool, status, body}]
global.fetch = (url, opts) => {
  fetchCalls.push({ url, opts: opts || {} });
  const canned =
    nextResponses.shift() ||
    routeResponses.find((r) => r.match(String(url))) ||
    { status: 200, body: {} };
  return Promise.resolve({
    ok: canned.status >= 200 && canned.status < 300,
    status: canned.status,
    json: () => Promise.resolve(canned.body),
  });
};

// ── 스텁 WebSocket ──────────────────────────────────────────────────────────

const sockets = [];
global.WebSocket = function (url) {
  this.url = url;
  this.onopen = null;
  this.onmessage = null;
  this.onclose = null;
  this.onerror = null;
  sockets.push(this);
};

global.setTimeout = () => 0;   // 재접속 타이머가 프로세스를 붙잡지 않게 한다.

// ── 부팅 때 나가는 조회의 답 — 주소로 고른다 ────────────────────────────────
// 화면이 부르는 순서(loadDispatchState → loadHistory 의 Promise.all 다섯)에 안 매달린다.
// 여기서 신원 보고 채널을 꺼진 상태로 준다 — 꺼진 배포에서 화면이 "카드가 나갑니다"로
// 얼버무리지 않는지가 검사 대상이라서다.
//
// ⚠ 순서가 아니라 주소로 고르는 이유는 위 fetch 스텁 주석에 있다(2026-08-04 실사고).
// ⚠ 더 좁은 주소를 먼저 둔다 — `find`가 먼저 맞는 것을 쓰므로 `/api/alerts?type=untagged`가
//    `/api/alerts` 보다 앞에 있어야 한다.
const NOW = new Date().toISOString();
routeResponses = [
  { match: (u) => u.startsWith("/api/dispatch/state"),
    status: 200, body: {} },
  { match: (u) => u.startsWith("/api/dashboard/snapshot"),
    status: 200, body: {
      server_time: NOW, ws_protocol_version: "1", robots: [], active_alerts: [],
      recent_events: [], counts: { robots: 0, active_alerts: 1, recent_events: 0 },
      identify_report_enabled: false } },
  { match: (u) => u.startsWith("/api/events"), status: 200, body: [] },
  { match: (u) => u.startsWith("/api/alerts?type=untagged"),
    status: 200, body: [
      { id: 7, type: "untagged", severity: "high", source_type: "gate_pass",
        source_id: 1, ack: false, created_at: NOW, identified: false }] },
  { match: (u) => u.startsWith("/api/alerts"), status: 200, body: [] },
  { match: (u) => u.startsWith("/api/stats/today"),
    status: 200, body: {
      date: NOW.slice(0, 10), timezone: "Asia/Seoul", tagging: 0,
      gate_pass: { total: 0, normal: 0, untagged: 0, exit: 0, beam_incomplete: 0, other: 0 },
      shuttle_arrival: 0, active_alerts: 1 } },
];

// ── 페이지 스크립트 실행 ────────────────────────────────────────────────────

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length !== 1) {
  console.error(`script 블록이 ${scripts.length}개다 — 하네스가 한 개를 전제한다`);
  process.exit(1);
}
// eslint-disable-next-line no-eval
(0, eval)(scripts[0]);

// ── 시나리오 ────────────────────────────────────────────────────────────────

const results = {};
const fail = (msg) => { console.error("HARNESS FAIL: " + msg); process.exit(1); };

const ws = sockets[0];
if (!ws) { fail("WebSocket을 안 열었다"); }
results.ws_url = ws.url;

function push(msg) { ws.onmessage({ data: JSON.stringify(msg) }); }

// 1) 미태깅 경보가 오면 신원 입력 대상 표에 줄이 생긴다.
push({
  type: "untagged_alert", version: "1", robot_id: null,
  timestamp: new Date().toISOString(),
  data: { alert_id: 42, gate_no: 7, source_type: "gate_pass", source_id: 1 },
});
const rowsHtml = byId["untaggedrows"].innerHTML;
results.panel_has_row = rowsHtml.includes('data-uident="42"');
results.panel_label_before = rowsHtml.includes("신원 입력");

// 2) 표의 버튼을 실제로 눌러 위임 핸들러를 태운다.
const btn = new El("button");
btn.setAttribute("data-uident", "42");
btn.parentNode = byId["untaggedrows"];
byId["untaggedrows"].fire("click", { target: btn });
results.form_opened = byId["identsec"].className === "";
results.form_target = byId["ident-target"].textContent;
results.overwrite_prechecked_new = byId["ident-overwrite"].checked;

// 3) 키가 없어도 **서버를 부른다.** 판정은 서버 몫이다.
//
// ⚠ 2026-08-04 수리 전에는 반대였다 — 화면이 fetch 앞에서 막고 "API 키를 넣어 주세요"를
//   띄웠다. 로그인(`AUTH_REQUIRE_LOGIN`)을 켜면 이 창구가 세션 쿠키로 도는데, 그 가드가
//   관리자로 로그인한 사람까지 막았다. 여기가 그 회귀 못이다.
const before = fetchCalls.length;
nextResponses = [{ status: 401, body: {} }];
byId["ident-person"].value = "가명-1";
byId["ident-send"].fire("click");
results.no_key_called_server = fetchCalls.length > before;
results.no_key_sent_url = fetchCalls[fetchCalls.length - 1].url;

// 4) 키를 넣고 보내면 X-API-Key 헤더가 실린다.
//
// ⚠ 2026-08-04 정정 — 칸 id 가 `ident-key`에서 `api-key`로 바뀌었다. 2026-08-03 커밋
//    f1e35cf 가 신원 입력 칸 옆에 있던 키 입력을 화면 맨 위 공용 자리로 옮기면서 갈렸는데,
//    그때 이 하네스를 같이 안 고쳤다. `byId["ident-key"]`가 undefined 라 여기서 TypeError 로
//    죽었고, 그 뒤로 이 파일이 지키던 화면 JS 계약 여섯이 통째로 안 돌고 있었다.
//    화면 시험은 문자열 검사라 안 깨졌고 `node --check`도 문법만 봐서 못 잡는다.
byId["api-key"].value = "test-key";
byId["ident-send"].fire("click");
const sent = fetchCalls[fetchCalls.length - 1];
results.sent_url = sent.url;
results.sent_method = sent.opts.method;
results.sent_key = (sent.opts.headers || {})["X-API-Key"];
results.sent_body_has_person = String(sent.opts.body || "").includes("가명-1");
// URL에 키를 실으면 안 된다(프록시 로그에 통째로 남는다).
results.key_not_in_url = !sent.url.includes("test-key");

// 5) 다른 화면이 신원을 적으면 이쪽 버튼이 바뀐다(대시보드 두 대 갈림).
push({
  type: "alert_identified", version: "1", robot_id: null,
  timestamp: new Date().toISOString(),
  data: { alert_id: 42, type: "untagged", severity: "high", identified: true,
          identified_at: new Date().toISOString(), corrected: false },
});
const afterHtml = byId["untaggedrows"].innerHTML;
results.panel_label_after = afterHtml.includes("✓ 신원 적힘");
results.panel_class_done = afterHtml.includes("identdone");

// 6) 이미 적힌 경고를 열면 고쳐 적기가 미리 켜지고 안내가 갈린다.
byId["untaggedrows"].fire("click", { target: btn });
results.overwrite_prechecked_done = byId["ident-overwrite"].checked;
results.done_note = byId["identnote"].textContent;

// 7) "목록 지우기"를 눌러도 신원 입력 대상 표는 안 사라진다.
byId["clear"].fire("click");
results.panel_survives_clear = byId["untaggedrows"].innerHTML.includes('data-uident="42"');

// 8) 409를 받으면 고쳐 적기 안내가 뜬다.
nextResponses = [{ status: 409, body: {} }];
byId["ident-person"].value = "가명-2";
byId["ident-send"].fire("click");

// 비동기 갈래(이력 조회 + 방금 쏜 409)가 다 돌고 나서 찍는다.
const tick = (n) => (n <= 0 ? Promise.resolve() : Promise.resolve().then(() => tick(n - 1)));
tick(20).then(() => {
  results.conflict_note = byId["identnote"].textContent;
  // 9) 이력으로 받은 미태깅 경고도 같은 표에 붙는다(이벤트 목록 상한과 무관).
  const finalHtml = byId["untaggedrows"].innerHTML;
  results.panel_has_history_row = finalHtml.includes('data-uident="7"');
  results.panel_still_has_live_row = finalHtml.includes('data-uident="42"');
  // 10) 보고 채널이 꺼져 있으면 화면이 그 사실을 못박는다.
  results.panel_note = byId["untaggednote"].textContent;
  results.history_note = byId["histnote"].textContent;
  // 11) 401 안내가 **두 원인을 다 짚는다.** 로그인을 켠 배포에서는 세션 문제이고 끈
  //     배포에서는 기기 키 문제인데, 화면은 어느 쪽인지 모른다. 한쪽만 적으면 켠 뒤에
  //     관리자가 있지도 않은 키를 찾아 헤맨다(2026-08-04 수리).
  //     ⚠ 폼을 **다시 열고** 보낸다. 위 4)의 성공 갈래가 `identAlertId`를 null로 되돌리는데
  //       그 대입이 마이크로태스크에서 늦게 돌아, 여기 올 때는 이미 닫힌 상태다. 안 열고
  //       누르면 `sendIdent`가 첫 줄에서 그냥 돌아가 요청이 안 나가고, 그러면 이 검사가
  //       "문구가 안 바뀌었다"를 자기 탓인 줄 모르고 통과시킨다.
  byId["untaggedrows"].fire("click", { target: btn });
  nextResponses = [{ status: 401, body: {} }];
  byId["ident-person"].value = "가명-3";
  const beforeUnauthorized = fetchCalls.length;
  byId["ident-send"].fire("click");
  if (fetchCalls.length === beforeUnauthorized) { fail("401 시나리오가 요청을 안 보냈다"); }
  return tick(10).then(() => {
    results.unauthorized_note = byId["identnote"].textContent;
    console.log(JSON.stringify(results));
  });
});
