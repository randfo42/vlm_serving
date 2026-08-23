#!/usr/bin/env python
"""수집 결과(run_collect 의 views.jsonl)를 지도 위 점으로. 무엇을 모았는지 눈으로.

    python app/eval/plot_collect.py app/runs/images/cheonggye-500m -o /tmp/c.svg
    python app/eval/plot_collect.py app/runs/images/{a,b} --nodes nodes.json -o /tmp/c.svg

`--nodes` 는 `check_pano_census.py -o` 가 낸 반경 전수 조사다. 주면 **배경**으로
깔린다 — "반경 안에 있는 것" 위에 "실제로 모은 것" 이 겹쳐 그려진다.
이 둘의 차이가 이 그림의 요점이다: **로드뷰 pano 는 계열별로 끊긴 그래프라
시작점 하나가 어디까지 갈 수 있는지를 정한다** (→ docs/23-open-questions.md §7).
2026-08-23 GS25 반경 500m 에서 계열이 24개였고 하천 보행로는 4.7% 였다
(2,493개 중 118개). 그 census 도 시드에 의존한다 — 같은 §7 참고.

점 하나 = **pano 하나**다. 한 pano 에서 여러 화각을 찍어도 점은 하나다
(그 수는 툴팁에 있다).

merc/타일 패턴은 plot_course.py · plot_explore.py 와 같다 (stdlib only).
"""
import argparse
import base64
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.plot_course import TILE, fetch_tile, merc
from labels.pano_meta import is_walk

INK = "#1a1a1a"
WALK = "#0ca30c"      # 도보 촬영 (labels.pano_meta.is_walk)
CAR = "#c26a1c"       # 차량 촬영
SETS = ["#1668d6", "#c0339a", "#0e9c8f", "#8a4fd8"]   # 수집 세트별


def load_manifest(d: Path) -> dict:
    text = (d / "views.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(x) for x in text.splitlines() if x.strip()]
    hdr = rows[0]
    panos: dict[str, dict] = {}
    for v in rows[1:]:
        p = panos.setdefault(v["pano_id"], {"lat": v["lat"], "lng": v["lng"],
                                            "n": 0, "depth": v["depth"]})
        p["n"] += 1
        p["depth"] = min(p["depth"], v["depth"])
    return {"name": d.name, "start": hdr["start"], "panos": panos,
            "views": len(rows) - 1}


def render(sets: list[dict], nodes: dict | None, with_map: bool = True) -> str:
    pts = [(p["lat"], p["lng"]) for s in sets for p in s["panos"].values()]
    pts += [tuple(s["start"]) for s in sets]
    center = nodes["center"] if nodes else None
    if nodes:
        pts += [(n["lat"], n["lng"]) for n in nodes["nodes"].values()]
        pts.append(tuple(center))
    if not pts:
        raise SystemExit("그릴 점이 없다")

    for z in (18, 17, 16, 15, 14):
        xs, ys = zip(*(merc(la, ln, z) for la, ln in pts), strict=True)
        if max(max(xs) - min(xs), max(ys) - min(ys)) <= 1100:
            break
    pad = 60
    w = max(720, int(max(xs) - min(xs)) + 2 * pad)
    h = max(600, int(max(ys) - min(ys)) + 2 * pad + 60)
    ox = (max(xs) + min(xs)) / 2 - w / 2
    oy = (max(ys) + min(ys)) / 2 - h / 2

    def sxy(lat, lng):
        x, y = merc(lat, lng, z)
        return x - ox, y - oy

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="sans-serif">',
         f'<rect width="{w}" height="{h}" fill="#f2f0ec"/>']
    if with_map:
        for tx in range(int(ox // TILE), int((ox + w) // TILE) + 1):
            for ty in range(int(oy // TILE), int((oy + h) // TILE) + 1):
                png = fetch_tile(z, tx, ty)
                if png:
                    b64 = base64.b64encode(png).decode()
                    o.append(f'<image x="{tx * TILE - ox:.1f}" y="{ty * TILE - oy:.1f}" '
                             f'width="{TILE}" height="{TILE}" '
                             f'href="data:image/png;base64,{b64}"/>')
        o.append(f'<rect width="{w}" height="{h}" fill="#ffffff" opacity="0.45"/>')

    mpp = 40_075_016.686 * math.cos(math.radians(pts[0][0])) / (TILE * (1 << z))

    # ── 배경: 반경 안 전수 조사 ─────────────────────────────────────────
    if nodes:
        cx, cy = sxy(*center)
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{nodes["radius_m"] / mpp:.1f}" '
                 f'fill="none" stroke="{INK}" stroke-width="1.5" '
                 f'stroke-dasharray="7 5" opacity="0.55"/>')
        for n in nodes["nodes"].values():
            if n["d"] > nodes["radius_m"]:
                continue
            a, b = sxy(n["lat"], n["lng"])
            walk = is_walk(n["tool"])
            o.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="1.7" '
                     f'fill="{WALK if walk else CAR}" opacity="0.5"/>')

    # ── 실제로 모은 것 ──────────────────────────────────────────────────
    for i, s in enumerate(sets):
        col = SETS[i % len(SETS)]
        for pid, p in s["panos"].items():
            a, b = sxy(p["lat"], p["lng"])
            o.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.4" fill="{col}" '
                     f'stroke="#ffffff" stroke-width="1.1">'
                     f'<title>{pid}  depth {p["depth"]}  {p["n"]}장</title></circle>')
        a, b = sxy(*s["start"])
        o.append(f'<path d="M {a:.1f} {b - 11:.1f} l 7 12 l -14 0 z" fill="{col}" '
                 f'stroke="#ffffff" stroke-width="1.6"/>')

    def label(x, y, t, size=12, bold=""):
        return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{INK}" {bold} '
                f'stroke="#ffffff" stroke-width="3.5" paint-order="stroke">{t}</text>')

    o.append(label(16, 26, "수집한 pano — 점 하나가 pano 하나", 14,
                   'font-weight="bold"'))
    yy = 46
    if nodes:
        ins = [n for n in nodes["nodes"].values() if n["d"] <= nodes["radius_m"]]
        nw = sum(1 for n in ins if is_walk(n["tool"]))
        o.append(f'<circle cx="22" cy="{yy - 4}" r="3" fill="{WALK}" opacity="0.7"/>')
        o.append(label(32, yy, f"반경 안 도보 {nw}", 11)); yy += 16
        o.append(f'<circle cx="22" cy="{yy - 4}" r="3" fill="{CAR}" opacity="0.7"/>')
        o.append(label(32, yy, f"반경 안 차량 {len(ins) - nw}", 11)); yy += 16
    for i, s in enumerate(sets):
        col = SETS[i % len(SETS)]
        o.append(f'<circle cx="22" cy="{yy - 4}" r="4" fill="{col}" '
                 f'stroke="#ffffff" stroke-width="1"/>')
        o.append(label(32, yy, f'{s["name"]} — pano {len(s["panos"])} · {s["views"]}장', 11))
        yy += 16

    m = min((v for v in (50, 100, 200, 500, 1000) if v / mpp >= 60), default=1000)
    o.append(f'<line x1="16" y1="{h - 22}" x2="{16 + m / mpp:.1f}" y2="{h - 22}" '
             f'stroke="{INK}" stroke-width="2.5"/>')
    o.append(label(16, h - 30, f"{m} m", 11))
    if with_map:
        o.append(label(w - 200, h - 12, "지도: © OpenStreetMap contributors", 10))
    o.append("</svg>")
    return "\n".join(o)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+", help="run_collect 의 out_dir (views.jsonl 이 있는 곳)")
    ap.add_argument("--nodes", default=None, help="반경 전수조사 JSON (배경으로 깐다)")
    ap.add_argument("--no-map", action="store_true", help="타일 없이")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    sets = [load_manifest(Path(d)) for d in a.dirs]
    nodes = json.loads(Path(a.nodes).read_text(encoding="utf-8")) if a.nodes else None
    Path(a.out).write_text(render(sets, nodes, not a.no_map), encoding="utf-8")
    for s in sets:
        print(f"  {s['name']}: pano {len(s['panos'])} · {s['views']}장")
    print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
