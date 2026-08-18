#!/usr/bin/env python
"""explore 결과 JSON → SVG 한 장. 웹 UI 전에 눈으로 확인하는 용도다.

    python app/run_explore.py --provider kakao --start 37.5695,127.0050 \\
        --max-calls 40 --dump /tmp/explore.json
    python app/eval/plot_explore.py /tmp/explore.json -o /tmp/explore.svg

의존성 없음(stdlib). 지도가 아니라 도형이다 — 좌표를 시작점 기준 미터로
펴서 그린다. 진짜 지도 위 마킹은 웹 UI(Kakao JS SDK)의 몫이다.

색은 판정 상태를 나타내며 선 스타일이 같이 간다 (색맹·인쇄 대비):
산책로 = 초록 실선 · 아님 = 빨강 점선 · 미탐색(frontier) = 회색 테두리 원.
"""
import argparse
import json
import math
from pathlib import Path

M_PER_DEG_LAT = 111_320.0

# 상태색 (dataviz status palette). 의미가 색에만 실리지 않게 선 스타일을 같이 쓴다
GOOD = "#0ca30c"       # 산책로
CRIT = "#d03b3b"       # 산책로 아님
MUTE = "#8a8a88"       # 미탐색 frontier / 보조선
INK = "#333333"


def to_xy(lat: float, lng: float, lat0: float, lng0: float) -> tuple[float, float]:
    """시작점 기준 미터 평면. 북쪽이 +y (SVG 로 갈 때 뒤집는다)."""
    x = (lng - lng0) * M_PER_DEG_LAT * math.cos(math.radians(lat0))
    y = (lat - lat0) * M_PER_DEG_LAT
    return x, y


def render(data: dict, width: int = 900, height: int = 700) -> str:
    lat0, lng0 = data["start"]
    nodes = {n["pano_id"]: n for n in data["nodes"]}

    # ── 간선 좌표 수집 ────────────────────────────────────────────────
    # to 좌표가 없는 판정(폴백 후보)은 heading 방향으로 12m 민 근사 위치에 그린다
    edges = []                      # (x1, y1, x2, y2, is_trail, label)
    pts = [(0.0, 0.0)]
    for p in data["probes"]:
        src = nodes.get(p["from_pano"])
        if src is None:
            continue
        x1, y1 = to_xy(src["lat"], src["lng"], lat0, lng0)
        if p.get("to_lat") is not None:
            x2, y2 = to_xy(p["to_lat"], p["to_lng"], lat0, lng0)
        else:
            rad = math.radians(p["heading"])
            x2, y2 = x1 + 12 * math.sin(rad), y1 + 12 * math.cos(rad)
        edges.append((x1, y1, x2, y2, p["is_trail"],
                      f"{p['from_pano']} → {p['to_pano'] or '(좌표)'}  "
                      f"h{p['heading']}  {'산책로' if p['is_trail'] else '아님'}"))
        pts += [(x1, y1), (x2, y2)]

    fronts = []                     # (x, y, reason)
    for f in data["frontier"]:
        if f.get("lat") is not None:
            x, y = to_xy(f["lat"], f["lng"], lat0, lng0)
            fronts.append((x, y, f["reason"]))
            pts.append((x, y))

    # ── 뷰포트 맞춤 (종횡비 유지) ─────────────────────────────────────
    pad = 60
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 20.0)
    scale = min(width - 2 * pad, height - 2 * pad) / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def sxy(x: float, y: float) -> tuple[float, float]:
        return (width / 2 + (x - cx) * scale,
                height / 2 - (y - cy) * scale)     # 북쪽이 위

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
         f'<rect width="{width}" height="{height}" fill="#ffffff"/>']

    for x1, y1, x2, y2, ok, label in edges:
        (a, b), (c, d) = sxy(x1, y1), sxy(x2, y2)
        style = (f'stroke="{GOOD}" stroke-width="3"' if ok
                 else f'stroke="{CRIT}" stroke-width="1.5" stroke-dasharray="5 4"')
        s.append(f'<line x1="{a:.1f}" y1="{b:.1f}" x2="{c:.1f}" y2="{d:.1f}" {style} '
                 f'stroke-linecap="round"><title>{label}</title></line>')

    for n in data["nodes"]:
        a, b = sxy(*to_xy(n["lat"], n["lng"], lat0, lng0))
        s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="4" fill="{INK}">'
                 f'<title>{n["pano_id"]}  depth {n["depth"]}</title></circle>')

    for x, y, reason in fronts:
        a, b = sxy(x, y)
        s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="6" fill="none" '
                 f'stroke="{MUTE}" stroke-width="2" stroke-dasharray="2 2">'
                 f'<title>frontier: {reason}</title></circle>')

    a, b = sxy(0, 0)
    s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="7" fill="none" stroke="{INK}" '
             f'stroke-width="2"/>')
    s.append(f'<text x="{a + 11:.1f}" y="{b + 4:.1f}" font-size="12" fill="{INK}">시작</text>')

    # ── 범례 · 축척 · 요약 ────────────────────────────────────────────
    trails = sum(1 for e in edges if e[4])
    s.append(f'<text x="{pad}" y="28" font-size="14" fill="{INK}" font-weight="bold">'
             f'explore — 판정 {len(edges)} (산책로 {trails}) · 노드 {len(data["nodes"])} · '
             f'호출 {data["calls"]} · {data["stop_reason"]}</text>')
    ly = 52
    s.append(f'<line x1="{pad}" y1="{ly}" x2="{pad + 26}" y2="{ly}" stroke="{GOOD}" '
             f'stroke-width="3"/><text x="{pad + 32}" y="{ly + 4}" font-size="12" '
             f'fill="{INK}">산책로</text>')
    s.append(f'<line x1="{pad + 96}" y1="{ly}" x2="{pad + 122}" y2="{ly}" stroke="{CRIT}" '
             f'stroke-width="1.5" stroke-dasharray="5 4"/><text x="{pad + 128}" y="{ly + 4}" '
             f'font-size="12" fill="{INK}">아님</text>')
    s.append(f'<circle cx="{pad + 182}" cy="{ly}" r="6" fill="none" stroke="{MUTE}" '
             f'stroke-width="2" stroke-dasharray="2 2"/><text x="{pad + 194}" y="{ly + 4}" '
             f'font-size="12" fill="{INK}">미탐색 frontier</text>')
    # 축척: 화면 폭의 1/5 안팎이 되는 "예쁜" 값을 고른다
    m = min((v for v in (5, 10, 20, 50, 100, 200, 500) if v * scale >= 60), default=500)
    s.append(f'<line x1="{pad}" y1="{height - 30}" x2="{pad + m * scale:.1f}" '
             f'y2="{height - 30}" stroke="{INK}" stroke-width="2"/>')
    s.append(f'<text x="{pad}" y="{height - 38}" font-size="11" fill="{INK}">{m} m</text>')
    s.append("</svg>")
    return "\n".join(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", help="run_explore.py --dump 이 만든 JSON")
    ap.add_argument("-o", "--out", default=None, help="SVG 출력 경로 (기본: <dump>.svg)")
    a = ap.parse_args()

    data = json.loads(Path(a.dump).read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else Path(a.dump).with_suffix(".svg")
    out.write_text(render(data), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
