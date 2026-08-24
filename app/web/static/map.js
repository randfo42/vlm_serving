// 판정 레이어 — 캔버스 하나에 pano 수천 개를 그린다.
//
// Marker/CustomOverlay 를 안 쓰는 이유: 점마다 DOM 노드가 생겨 2천 개부터
// 팬·줌이 끊긴다. MarkerClusterer 는 여러 pano 를 하나로 접는데 "지도 한 점
// = pano 하나" 가 이 UI 의 데이터 모델이라 정면 충돌한다 — 클러스터 위에서는
// 라벨 클릭의 뜻이 정의되지 않는다.
//
// AbstractOverlay 대신 지도 컨테이너 위 절대배치 캔버스를 쓴다: 오버레이
// 패널의 이동 좌표계를 따라가는 대신, bounds_changed(팬·줌 동안 연속 발생)
// 마다 containerPointFromCoords 로 전부 다시 그린다. 3천 arc 는 한 프레임
// 안이라 이쪽이 좌표계 실수의 여지가 없다.

// 색 — eval/plot_explore.py 의 GOOD/CRIT/MUTE 토큰과 맞춘다
const NATURE = { 0: "#d03b3b", 1: "#d1b93c", 2: "#7fbf3f", 3: "#0ca30c" };
const GOOD = "#0ca30c", CRIT = "#d03b3b", INK = "#1a1a1a";
const CELL = 32;          // 히트테스트 격자(px)
const HIT_R = 9;          // 클릭 판정 반경(px)

export function colorOf(p) {
  // v5/v6 는 녹지 등급이 축이고, v1~v4(nature 없음)는 is_trail 로 칠한다
  if (p.nature_level !== null && p.nature_level !== undefined)
    return NATURE[p.nature_level] ?? CRIT;
  return p.is_trail ? GOOD : CRIT;
}

export class PanoLayer {
  constructor(map, containerEl) {
    this.map = map;
    this.canvas = document.createElement("canvas");
    this.canvas.className = "pano-layer";
    containerEl.appendChild(this.canvas);
    this.points = [];
    this.showHeadings = false;
    this.selected = null;           // pano_id
    this.grid = new Map();
    kakao.maps.event.addListener(map, "bounds_changed", () => this.draw());
    new ResizeObserver(() => this.draw()).observe(containerEl);
  }

  setData(points, showHeadings) {
    this.points = points;
    this.showHeadings = showHeadings;
    this.draw();
  }

  select(panoId) {
    this.selected = panoId;
    this.draw();
  }

  draw() {
    const el = this.canvas.parentElement;
    const w = el.clientWidth, h = el.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    if (this.canvas.width !== w * dpr || this.canvas.height !== h * dpr) {
      this.canvas.width = w * dpr;
      this.canvas.height = h * dpr;
      this.canvas.style.width = w + "px";
      this.canvas.style.height = h + "px";
    }
    const ctx = this.canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    this.grid.clear();

    const proj = this.map.getProjection();
    for (const p of this.points) {
      const pt = proj.containerPointFromCoords(
        new kakao.maps.LatLng(p.lat, p.lng));
      const x = pt.x, y = pt.y;
      if (x < -20 || y < -20 || x > w + 20 || y > h + 20) continue;
      p._x = x; p._y = y;
      const key = ((x / CELL) | 0) + "_" + ((y / CELL) | 0);
      if (!this.grid.has(key)) this.grid.set(key, []);
      this.grid.get(key).push(p);

      // 방위 눈금 — 그 방위 판정의 색. 점(MAX)과 눈금이 다른 색이면
      // "어느 방향이 그렇게 보였나" 가 눈에 잡힌다
      if (this.showHeadings && p.headings) {
        for (const hd of p.headings) {
          const a = (hd.heading - 90) * Math.PI / 180;   // 방위각 → 캔버스각
          ctx.strokeStyle = colorOf(hd);
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(x + 5 * Math.cos(a), y + 5 * Math.sin(a));
          ctx.lineTo(x + 12 * Math.cos(a), y + 12 * Math.sin(a));
          ctx.stroke();
        }
      }

      ctx.fillStyle = colorOf(p);
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();

      // 사람 라벨 = 굵은 잉크 테두리. 모델의 판정(채움색)과 사람의 주장
      // (테두리)이 시각적으로 절대 안 섞이게 한다
      if (p.label !== null && p.label !== undefined) {
        ctx.strokeStyle = INK;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(x, y, 6.5, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (p.pano_id === this.selected) {
        ctx.strokeStyle = "#2563eb";
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(x, y, 9, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
  }

  hit(x, y) {
    // draw() 때 채운 격자에서 3×3 칸만 본다 — 점 전수 순회는 mousemove 마다
    // 하기엔 아깝다
    const cx = (x / CELL) | 0, cy = (y / CELL) | 0;
    let best = null, bestD = HIT_R * HIT_R;
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (const p of this.grid.get((cx + dx) + "_" + (cy + dy)) ?? []) {
          const d = (p._x - x) ** 2 + (p._y - y) ** 2;
          if (d < bestD) { bestD = d; best = p; }
        }
      }
    }
    return best;
  }
}
