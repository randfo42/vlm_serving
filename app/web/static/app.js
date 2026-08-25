// 앱 배선 — SDK 로드 → 지도 → 조회 → 호버/클릭 패널.
// 빌드 스텝 없음: 순수 ES 모듈. npm 을 들이면 "무엇이 서빙되는가" 의 정본이
// 소스/번들 둘이 된다.
import { PanoLayer, colorOf } from "/map.js";

const $ = (id) => document.getElementById(id);
const HEADING_LEVEL = 4;      // 이 줌 레벨 이하(확대)에서만 방위 눈금을 받는다

let map, layer, cfg;
let currentVersion = null;
let fetched = null;           // {bbox:{s,w,n,e}, headings:bool, version} — 재조회 생략용
let aborter = null;
let detailCache = new Map();  // pano_id → /api/pano 응답

main();

async function main() {
  cfg = await (await fetch("/api/config")).json();
  if (!cfg.kakao_js_key) {
    // 키가 없어도 백엔드 안내문(해결 방법 포함)을 그대로 보여준다 —
    // 빈 화면보다 traceback 보다 낫다
    fatal("Kakao JS 키가 없다.\n\n" + (cfg.kakao_key_error ?? ""));
    return;
  }
  currentVersion = cfg.prompt_version;
  try {
    await loadSdk(cfg.kakao_js_key);
  } catch (e) {
    fatal(e.message);
    return;
  }
  kakao.maps.load(init);
}

function loadSdk(key) {
  return new Promise((ok, bad) => {
    const s = document.createElement("script");
    // autoload=false: kakao.maps.load 콜백으로 초기화 시점을 우리가 잡는다
    s.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${key}&autoload=false&libraries=services`;
    s.onload = ok;
    // 도메인 미등록이면 Kakao 가 JSON 에러를 주고 브라우저(ORB)가 스크립트
    // 로드를 막는다 — 즉 여기 onerror 로 온다 (실측 2026-08-25:
    // "domain mismatched! caller=<origin>"). 네트워크 실패도 같은 경로다
    s.onerror = () => bad(new Error(
      "Kakao 지도 SDK 로드 실패.\n\n" +
      "가장 흔한 원인: 이 origin 이 Kakao 콘솔에 등록돼 있지 않다.\n" +
      "해결: Kakao developers 콘솔 > 내 애플리케이션 > 앱 설정 > 플랫폼 > " +
      `Web 에\n\n    ${location.origin}\n\n을 추가 등록할 것.\n` +
      "등록은 프로토콜·호스트·포트가 전부 정확히 일치해야 한다 — " +
      "localhost 와 127.0.0.1 은 서로 다른 도메인이다.\n\n" +
      "그 밖의 원인: 네트워크 차단, JS 키 오류 (app/.env 의 KAKAO_MAP_JS_API_KEY)."));
    document.head.appendChild(s);
  });
}

function init() {
  const el = $("map");
  map = new kakao.maps.Map(el, {
    center: new kakao.maps.LatLng(cfg.center[0], cfg.center[1]),
    level: 5,
  });
  layer = new PanoLayer(map, el);
  // 디버그 핸들 — 헤드리스 검증(uicheck)과 콘솔 진단이 모듈 내부에 닿는
  // 유일한 통로. 앱 코드는 이걸 통해 아무것도 하지 않는다
  window._tw = { map, layer };
  fillVersions();
  // idle 은 팬·줌이 **멎은 뒤** 한 번 뜬다 — SDK 가 1차 디바운스를 해 준다.
  // bounds_changed 는 연속으로 뜨므로 그리기 갱신(layer 내부)에만 쓴다
  kakao.maps.event.addListener(map, "idle", () => schedule(250));
  wirePointer(el);
  $("version").addEventListener("change", (e) => {
    currentVersion = e.target.value;
    detailCache.clear();
    schedule(0);
  });
  $("panel-close").addEventListener("click", () => closePanel());
  wireJobs();
  schedule(0);
}

async function fillVersions() {
  const vs = (await (await fetch("/api/versions")).json()).versions;
  const sel = $("version");
  sel.innerHTML = "";
  for (const v of vs) {
    const o = document.createElement("option");
    o.value = v.prompt_version;
    o.textContent = `${v.prompt_version} (${v.verdicts})`;
    sel.appendChild(o);
  }
  // 현재 설정 버전에 판정이 아직 없으면 있는 버전 중 첫 번째로
  if (![...sel.options].some((o) => o.value === currentVersion)) {
    if (sel.options.length) currentVersion = sel.options[0].value;
  }
  sel.value = currentVersion;
}

let timer = null;
function schedule(ms) {
  clearTimeout(timer);
  timer = setTimeout(refresh, ms);
}

async function refresh() {
  const b = map.getBounds();
  const sw = b.getSouthWest(), ne = b.getNorthEast();
  const wantHeadings = map.getLevel() <= HEADING_LEVEL;
  // 이미 받은(20% 부풀린) bbox 안에서의 작은 팬이면 네트워크를 안 탄다
  if (fetched && fetched.version === currentVersion
      && fetched.headings === wantHeadings
      && sw.getLat() >= fetched.s && sw.getLng() >= fetched.w
      && ne.getLat() <= fetched.n && ne.getLng() <= fetched.e) return;

  const padLat = (ne.getLat() - sw.getLat()) * 0.2;
  const padLng = (ne.getLng() - sw.getLng()) * 0.2;
  const q = new URLSearchParams({
    s: sw.getLat() - padLat, w: sw.getLng() - padLng,
    n: ne.getLat() + padLat, e: ne.getLng() + padLng,
    prompt_version: currentVersion, headings: wantHeadings ? "1" : "0",
  });
  aborter?.abort();
  aborter = new AbortController();
  let r;
  try {
    r = await fetch(`/api/panos?${q}`, { signal: aborter.signal });
  } catch (e) {
    if (e.name === "AbortError") return;
    throw e;
  }
  if (!r.ok) {
    banner((await r.json()).detail ?? `조회 실패 (${r.status})`);
    return;
  }
  const body = await r.json();
  fetched = { s: +q.get("s"), w: +q.get("w"), n: +q.get("n"), e: +q.get("e"),
              version: currentVersion, headings: wantHeadings };
  layer.setData(body.panos, wantHeadings);
  $("stat").textContent = `pano ${body.panos.length}` +
    (wantHeadings ? " · 방위 표시" : " · 확대하면 방위 눈금");
  banner(body.truncated ? "표시 한도 초과 — 확대해서 볼 것" : null);
}

function banner(msg) {
  const el = $("banner");
  el.hidden = !msg;
  if (msg) el.textContent = msg;
}

function fatal(msg) {
  const el = $("fatal");
  el.hidden = false;
  el.textContent = msg;
}

// ── 호버 = 판정 이력 요약, 클릭 = 상세 패널 (이미지) ────────────────────────

function wirePointer(el) {
  let downAt = null;
  el.addEventListener("mousedown", (e) => { downAt = [e.clientX, e.clientY]; });
  el.addEventListener("mousemove", (e) => onMove(e), { passive: true });
  el.addEventListener("click", (e) => {
    // 드래그(팬)가 클릭으로 새지 않게 — 6px 이상 움직였으면 무시
    if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6)
      return;
    const p = hitAt(e);
    if (p) openPanel(p.pano_id);
  });
}

function hitAt(e) {
  const rect = $("map").getBoundingClientRect();
  return layer.hit(e.clientX - rect.left, e.clientY - rect.top);
}

let hoverId = null;
async function onMove(e) {
  const p = hitAt(e);
  const tip = $("tooltip");
  if (!p) { tip.hidden = true; hoverId = null; return; }
  tip.style.left = e.clientX + 14 + "px";
  tip.style.top = e.clientY + 14 + "px";
  if (p.pano_id === hoverId) { tip.hidden = false; return; }
  hoverId = p.pano_id;
  const d = await detail(p.pano_id);
  if (hoverId !== p.pano_id) return;      // 그 사이 다른 점으로 옮겨갔다
  tip.innerHTML = tooltipHtml(p, d);
  tip.hidden = false;
}

async function detail(panoId) {
  if (!detailCache.has(panoId)) {
    const r = await fetch(`/api/pano/${encodeURIComponent(panoId)}`);
    detailCache.set(panoId, await r.json());
  }
  return detailCache.get(panoId);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function vrow(v) {
  const parts = [`h${v.heading}`];
  if (v.nature_level !== null) parts.push(`녹지 ${v.nature_level}`);
  if (v.footway !== null) parts.push(`인도 ${v.footway}`);
  if (v.camera_surface) parts.push(esc(v.camera_surface));
  parts.push(v.is_trail ? "🟢" : "🔴");
  return parts.join(" · ");
}

function tooltipHtml(p, d) {
  const rows = d.verdicts.map((v) =>
    `<tr><td>${esc(v.prompt_version ?? "?")}</td><td>${vrow(v)}</td></tr>`);
  const label = d.labels.length
    ? `<div>사람 라벨: ${d.labels[0].is_trail ? "산책로 ⭕" : "아님 ❌"}</div>` : "";
  return `<div><b>${esc(p.pano_id)}</b></div>${label}` +
    `<table>${rows.join("")}</table><div>클릭 → 이미지·상세</div>`;
}

async function openPanel(panoId) {
  layer.select(panoId);
  const d = await detail(panoId);
  const body = $("panel-body");
  // 현재 버전의 최신 판정을 기본 선택 — 없으면 마지막 판정
  const cur = d.verdicts.filter((v) => v.prompt_version === currentVersion);
  const sel = (cur.length ? cur : d.verdicts).at(-1);
  body.innerHTML = panelHtml(d);
  for (const tr of body.querySelectorAll("tr.clickable")) {
    tr.addEventListener("click", () => {
      showImage(body, d, Number(tr.dataset.vid));
      body.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
      tr.classList.add("sel");
    });
  }
  for (const btn of body.querySelectorAll("[data-label]")) {
    btn.addEventListener("click", () => saveLabel(panoId, btn.dataset.label));
  }
  $("panel").hidden = false;
  if (sel) {
    showImage(body, d, sel.verdict_id);
    body.querySelector(`tr[data-vid="${sel.verdict_id}"]`)?.classList.add("sel");
  }
}

function panelHtml(d) {
  const p = d.pano;
  const l = d.labels[0];
  // 라벨은 이 UI 가 존재하는 이유다 — 정확도를 못 재는 병목이 "라벨 7건"
  // 이었다. 지도 보면서 클릭 한 번으로 ⭕/❌ 를 단다
  const label = `
    <div class="labelbox">
      ${l ? `사람 라벨: <b>${l.is_trail ? "산책로 ⭕" : "아님 ❌"}</b>
             <span class="muted">(${esc(l.updated_at)})</span>`
          : `<span class="muted">사람 라벨 없음</span>`}
      <div class="labelbtns">
        <button data-label="1">산책로 ⭕</button>
        <button data-label="0">아님 ❌</button>
        ${l ? `<button data-label="del">지우기</button>` : ""}
      </div>
      <input id="labelnote" placeholder="메모 (근거)"
             value="${l?.note ? esc(l.note) : ""}">
    </div>`;
  const rows = d.verdicts.map((v) => `
    <tr class="clickable" data-vid="${v.verdict_id}">
      <td>${esc(v.prompt_version ?? "?")}</td>
      <td>${v.heading}°</td>
      <td style="color:${colorOf(v)}">●</td>
      <td>${v.nature_level ?? "–"}</td>
      <td>${v.footway ?? "–"}</td>
      <td>${v.has_image ? "🖼" : ""}</td>
    </tr>`).join("");
  return `
    <h3>${esc(p.pano_id)}</h3>
    <div class="muted">${p.lat.toFixed(6)}, ${p.lng.toFixed(6)}</div>
    <div>${label}</div>
    <table>
      <tr><th>프롬프트</th><th>방위</th><th></th><th>녹지</th><th>인도</th><th></th></tr>
      ${rows}
    </table>
    <div id="imgbox"></div>`;
}

async function showImage(body, d, verdictId) {
  const box = body.querySelector("#imgbox");
  const v = d.verdicts.find((x) => x.verdict_id === verdictId);
  if (!v?.has_image) {
    box.innerHTML = `<div class="noimg">이미지 없음 — save_images 가 꺼진
      런이다 (옛 런 전부). 앞으로의 런은 저장된다.</div>`;
    return;
  }
  box.innerHTML = `<img src="/api/image/${verdictId}"
    alt="VLM 이 본 장면 (${esc(v.prompt_version)} · h${v.heading})">`;
}

async function saveLabel(panoId, action) {
  const d = detailCache.get(panoId);
  let r;
  if (action === "del") {
    r = await fetch(`/api/labels/${d.labels[0].label_id}`, { method: "DELETE" });
    if (r.ok) d.labels = [];
  } else {
    const note = $("panel-body").querySelector("#labelnote")?.value || null;
    r = await fetch("/api/labels", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pano_id: panoId, is_trail: action === "1", note }),
    });
    if (r.ok) d.labels = [await r.json()];
  }
  if (!r.ok) {
    banner(`라벨 저장 실패 (${r.status})`);
    return;
  }
  // 지도 점의 테두리를 즉시 갱신 — 서버 재조회 없이 로컬 반영으로 충분하다
  // (같은 값을 서버가 방금 확인해 줬다)
  const p = layer.points.find((x) => x.pano_id === panoId);
  if (p) p.label = d.labels.length ? (d.labels[0].is_trail ? 1 : 0) : null;
  layer.draw();
  openPanel(panoId);      // 패널을 새 상태로 다시 그린다
}

function closePanel() {
  $("panel").hidden = true;
  layer.select(null);
}

// ── 탐색 잡 — 우클릭으로 넣고, 좌하단 위젯으로 지켜본다 ────────────────────

function wireJobs() {
  kakao.maps.event.addListener(map, "rightclick", (e) =>
    openJobForm(e.latLng.getLat(), e.latLng.getLng()));
  $("jobs").hidden = false;
  pollJobs();
}

function openJobForm(lat, lng) {
  const d = cfg.defaults ?? { radius_m: 500, max_seconds: 3600 };
  $("panel-body").innerHTML = `
    <h3>여기서 탐색</h3>
    <div class="muted">${lat.toFixed(6)}, ${lng.toFixed(6)}</div>
    <div class="jobform">
      <label>반경 (m) <input id="jf-radius" type="number" value="${d.radius_m}"></label>
      <label>시간 상한 (s) <input id="jf-secs" type="number" value="${d.max_seconds}"></label>
      <button class="go">큐에 넣기</button>
      <div class="muted">워커(run_worker.py)가 떠 있어야 실제로 돈다 —
        큐에 넣었는데 안 움직이면 그게 첫 번째 확인 대상이다.</div>
    </div>`;
  $("panel-body").querySelector(".go").addEventListener("click", async () => {
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lat, lng,
        radius_m: Number($("panel-body").querySelector("#jf-radius").value),
        max_seconds: Number($("panel-body").querySelector("#jf-secs").value),
      }),
    });
    if (!r.ok) {
      banner((await r.json()).detail ?? `잡 생성 실패 (${r.status})`);
      return;
    }
    closePanel();
    pollJobs(true);
  });
  $("panel").hidden = false;
}

const JOB_ICON = { queued: "⏳", claimed: "🔜", running: "🏃",
                   done: "✅", failed: "✗", canceled: "⛔" };
let jobTimer = null;
let jobsPrev = new Map();     // job_id → state (done 전환 감지용)

async function pollJobs(now = false) {
  clearTimeout(jobTimer);
  const r = await fetch("/api/jobs?limit=8");
  const jobs = (await r.json()).jobs;
  renderJobs(jobs);
  // 끝난 잡이 생겼으면 그 판정이 지도에 떠야 한다 — 뷰포트 캐시를 버린다
  for (const j of jobs) {
    const was = jobsPrev.get(j.job_id);
    if (was && was !== j.state && (j.state === "done" || j.state === "canceled")) {
      fetched = null;
      detailCache.clear();
      schedule(0);
    }
    jobsPrev.set(j.job_id, j.state);
  }
  const active = jobs.some((j) => ["queued", "claimed", "running"].includes(j.state));
  // 도는 잡이 있을 때만 폴링한다 — 유휴 페이지가 서버를 계속 두드릴 이유가 없다
  if (active || now) jobTimer = setTimeout(pollJobs, 2500);
}

function renderJobs(jobs) {
  const list = $("jobs-list");
  if (!jobs.length) {
    list.innerHTML = `<div class="muted">아직 없음</div>`;
    return;
  }
  list.innerHTML = jobs.map((j) => {
    const prog = j.progress_json ? JSON.parse(j.progress_json) : null;
    const info = j.state === "running" && prog
      ? `판정 ${prog.verdicts} · ${Math.round(prog.elapsed_s)}s`
      : j.state === "failed" ? esc((j.error ?? "").split("\n")[0])
      : j.stop_reason ?? "";
    const cancelBtn = ["queued", "claimed", "running"].includes(j.state)
      ? `<button data-cancel="${j.job_id}" title="취소">✕</button>` : "";
    return `<div class="job">${JOB_ICON[j.state] ?? "?"} #${j.job_id}
      <span>r${Math.round(j.radius_m)}m</span>
      <span class="muted">${info}</span>${cancelBtn}</div>`;
  }).join("");
  // 워커 없이 큐만 쌓이는 상태를 조용히 두지 않는다
  const stuck = jobs.some((j) => j.state === "queued" &&
    Date.now() - Date.parse(j.created_at) > 15000);
  if (stuck) list.innerHTML +=
    `<div class="hint">⚠ 큐가 안 빠진다 — 워커를 띄울 것: python app/run_worker.py</div>`;
  for (const b of list.querySelectorAll("[data-cancel]")) {
    b.addEventListener("click", async () => {
      await fetch(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" });
      pollJobs(true);
    });
  }
}
