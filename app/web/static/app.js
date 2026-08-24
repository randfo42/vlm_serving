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
  await loadSdk(cfg.kakao_js_key);
  kakao.maps.load(init);
}

function loadSdk(key) {
  return new Promise((ok, bad) => {
    const s = document.createElement("script");
    // autoload=false: kakao.maps.load 콜백으로 초기화 시점을 우리가 잡는다
    s.src = `//dapi.kakao.com/v2/maps/sdk.js?appkey=${key}&autoload=false&libraries=services`;
    s.onload = ok;
    // 도메인 미등록이면 스크립트는 로드되고 지도만 안 뜬다 — 그 경우는
    // 콘솔에 Kakao 쪽 메시지가 남는다. 여기 onerror 는 네트워크 실패용
    s.onerror = () => bad(new Error("SDK 로드 실패 — 네트워크나 키를 확인"));
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
