#!/usr/bin/env python
"""explore 결과 JSON → 지도 위 SVG 한 장. 웹 UI 전에 눈으로 확인하는 용도다.

    # 설정에서 run.dump: /tmp/explore.json 을 켜고 돌린 뒤
    python app/run_explore.py --config app/config/my.yaml
    python app/eval/plot_explore.py /tmp/explore.json -o /tmp/explore.svg

의존성 없음(stdlib). 배경은 OSM 타일을 받아 SVG 에 base64 로 내장한다 —
파일 하나로 자족적이고, 오프라인이면 `--no-map` 으로 도형만 그린다.
진짜 지도 UI(카카오맵 위 마킹)는 웹 UI 의 몫이다.

좌표계는 웹 메르카토르 픽셀 하나로 통일한다. 타일과 데이터가 같은 변환을
쓰므로 어긋날 수가 없다.

색은 판정 상태이고 선 스타일이 같이 간다 (색맹·인쇄 대비):
산책로 = 초록 실선 · 아님 = 빨강 점선 · 미탐색(frontier) = 회색 점선 원.
"""
import argparse
import base64
import json
import math
import urllib.request
from pathlib import Path

# 상태색 (dataviz status palette). 의미가 색에만 실리지 않게 선 스타일을 같이 쓴다
GOOD = "#0ca30c"       # 산책로
CRIT = "#d03b3b"       # 산책로 아님
MUTE = "#78787a"       # 미탐색 frontier / 보조
INK = "#1a1a1a"

TILE = 256
# 타일 usage policy 가 UA 를 요구한다. 뷰포트 한 장에 타일 십수 개 수준의 소량이다
UA = "trailwalk-plot/0.1 (local research; one-off dev rendering)"


def merc(lat: float, lng: float, z: int) -> tuple[float, float]:
    """웹 메르카토르 전역 픽셀 좌표."""
    n = TILE * (1 << z)
    x = (lng + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def fetch_tile(z: int, tx: int, ty: int) -> bytes | None:
    req = urllib.request.Request(
        f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception:
        return None               # 타일 하나 빠져도 그림은 성립한다


def collect_latlngs(data: dict) -> tuple[list[dict], list[dict], list[tuple]]:
    """probes 를 (from·to 좌표가 있는) 간선으로, frontier 를 점으로 편다."""
    nodes = {n["pano_id"]: n for n in data["nodes"]}
    edges, fronts, pts = [], [], []
    for p in data["probes"]:
        src = nodes.get(p["from_pano"])
        if src is None or p.get("to_lat") is None:
            # 좌표 없는 probe 는 그리지 않는다. 여기서 heading 방향으로 좌표를
            # 밀어 지어내던 시절이 있었는데, 그러면 실측 pano 간선과 우리가
            # 만든 점이 그림에서 구분되지 않는다. 탐색에서 좌표 밀기를 없앤
            # 지금은 to_lat 없는 probe 자체가 옛 dump 이거나 상류 버그다.
            continue
        to = (p["to_lat"], p["to_lng"])
        edges.append({"a": (src["lat"], src["lng"]), "b": to, "ok": p["is_trail"],
                      "label": f"{p['from_pano']} → {p['to_pano']}  "
                               f"h{p['heading']}  {'산책로' if p['is_trail'] else '아님'}"})
        pts += [edges[-1]["a"], to]
    for f in data["frontier"]:
        if f.get("lat") is not None:
            fronts.append(f)
            pts.append((f["lat"], f["lng"]))
    pts.append(tuple(data["start"]))
    return edges, fronts, pts


def render(data: dict, with_map: bool = True) -> str:
    lat0, lng0 = data["start"]
    edges, fronts, pts = collect_latlngs(data)

    # 데이터 범위가 뷰포트 안쪽(~820px)에 들어오는 가장 큰 줌을 고른다
    for z in (18, 17, 16, 15):
        xs, ys = zip(*(merc(lat, lng, z) for lat, lng in pts), strict=True)
        if max(max(xs) - min(xs), max(ys) - min(ys)) <= 820:
            break
    pad = 70
    w = max(640, int(max(xs) - min(xs)) + 2 * pad)
    h = max(520, int(max(ys) - min(ys)) + 2 * pad + 40)
    ox = (max(xs) + min(xs)) / 2 - w / 2      # 전역 픽셀 → 뷰포트 원점
    oy = (max(ys) + min(ys)) / 2 - h / 2

    def sxy(lat: float, lng: float) -> tuple[float, float]:
        x, y = merc(lat, lng, z)
        return x - ox, y - oy

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="sans-serif">',
         f'<rect width="{w}" height="{h}" fill="#f2f0ec"/>']

    if with_map:
        for tx in range(int(ox // TILE), int((ox + w) // TILE) + 1):
            for ty in range(int(oy // TILE), int((oy + h) // TILE) + 1):
                png = fetch_tile(z, tx, ty)
                if png:
                    b64 = base64.b64encode(png).decode()
                    s.append(f'<image x="{tx * TILE - ox:.1f}" y="{ty * TILE - oy:.1f}" '
                             f'width="{TILE}" height="{TILE}" '
                             f'href="data:image/png;base64,{b64}"/>')
        # 타일을 살짝 눌러 데이터가 앞으로 나오게
        s.append(f'<rect width="{w}" height="{h}" fill="#ffffff" opacity="0.35"/>')

    for e in edges:
        (a, b), (c, d) = sxy(*e["a"]), sxy(*e["b"])
        style = (f'stroke="{GOOD}" stroke-width="3.5"' if e["ok"]
                 else f'stroke="{CRIT}" stroke-width="1.8" stroke-dasharray="6 4"')
        s.append(f'<line x1="{a:.1f}" y1="{b:.1f}" x2="{c:.1f}" y2="{d:.1f}" {style} '
                 f'stroke-linecap="round"><title>{e["label"]}</title></line>')

    for n in data["nodes"]:
        a, b = sxy(n["lat"], n["lng"])
        fill = INK if n.get("is_trail") is None else (GOOD if n["is_trail"] else CRIT)
        s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="4.5" fill="{fill}" '
                 f'stroke="#ffffff" stroke-width="1.5">'
                 f'<title>{n["pano_id"]}  depth {n["depth"]}</title></circle>')

    for f in fronts:
        a, b = sxy(f["lat"], f["lng"])
        s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="7" fill="none" '
                 f'stroke="{MUTE}" stroke-width="2.5" stroke-dasharray="2.5 2.5">'
                 f'<title>frontier: {f["reason"]}</title></circle>')

    a, b = sxy(lat0, lng0)
    s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="8" fill="none" stroke="{INK}" '
             f'stroke-width="2.5"/>')
    s.append(f'<text x="{a + 13:.1f}" y="{b + 5:.1f}" font-size="13" fill="{INK}" '
             f'stroke="#ffffff" stroke-width="3" paint-order="stroke">시작</text>')

    # ── 제목 · 범례 · 축척 · 출처 ─────────────────────────────────────
    trails = sum(1 for e in edges if e["ok"])
    def label(x, y, t, size=12, bold=""):
        return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{INK}" {bold} '
                f'stroke="#ffffff" stroke-width="3.5" paint-order="stroke">{t}</text>')
    s.append(label(16, 26, f'explore — 판정 {len(edges)} (산책로 {trails}) · '
                          f'노드 {len(data["nodes"])} · 호출 {data["calls"]} · '
                          f'{data["stop_reason"]}', 14, 'font-weight="bold"'))
    ly = 50
    s.append(f'<line x1="16" y1="{ly}" x2="42" y2="{ly}" stroke="{GOOD}" stroke-width="3.5"/>')
    s.append(label(48, ly + 4, "산책로"))
    s.append(f'<line x1="112" y1="{ly}" x2="138" y2="{ly}" stroke="{CRIT}" '
             f'stroke-width="1.8" stroke-dasharray="6 4"/>')
    s.append(label(144, ly + 4, "아님"))
    s.append(f'<circle cx="198" cy="{ly}" r="6" fill="none" stroke="{MUTE}" '
             f'stroke-width="2.5" stroke-dasharray="2.5 2.5"/>')
    s.append(label(210, ly + 4, "미탐색 frontier"))

    mpp = 40_075_016.686 * math.cos(math.radians(lat0)) / (TILE * (1 << z))
    m = min((v for v in (10, 20, 50, 100, 200, 500) if v / mpp >= 60), default=500)
    s.append(f'<line x1="16" y1="{h - 22}" x2="{16 + m / mpp:.1f}" y2="{h - 22}" '
             f'stroke="{INK}" stroke-width="2.5"/>')
    s.append(label(16, h - 30, f"{m} m", 11))
    if with_map:
        s.append(label(w - 190, h - 12, "지도: © OpenStreetMap contributors", 10))
    s.append("</svg>")
    return "\n".join(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", help="run.dump 로 run_explore.py 가 만든 JSON")
    ap.add_argument("-o", "--out", default=None, help="SVG 출력 경로 (기본: <dump>.svg)")
    ap.add_argument("--no-map", action="store_true",
                    help="OSM 타일 배경 없이 도형만 (오프라인)")
    a = ap.parse_args()

    data = json.loads(Path(a.dump).read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else Path(a.dump).with_suffix(".svg")
    out.write_text(render(data, with_map=not a.no_map), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
