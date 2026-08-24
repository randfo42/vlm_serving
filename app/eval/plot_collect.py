#!/usr/bin/env python
"""수집 결과(run_collect 의 views.jsonl)를 지도 위 점으로. 무엇을 모았는지 눈으로.

    python app/eval/plot_collect.py app/runs/images/cheonggye-500m -o /tmp/c.svg
    python app/eval/plot_collect.py app/runs/images/{a,b} --nodes nodes.json -o /tmp/c.svg
    python app/eval/plot_collect.py app/runs/images/x --verdicts v.jsonl -o /tmp/c.svg

`--nodes` 는 `check_pano_census.py -o` 가 낸 반경 전수 조사다. 주면 **배경**으로
깔린다 — "반경 안에 있는 것" 위에 "실제로 모은 것" 이 겹쳐 그려진다.
이 둘의 차이가 이 그림의 요점이다: **로드뷰 pano 는 계열별로 끊긴 그래프라
시작점 하나가 어디까지 갈 수 있는지를 정한다** (→ docs/23-open-questions.md §7).
2026-08-23 GS25 반경 500m 에서 계열이 24개였고 하천 보행로는 4.7% 였다
(2,493개 중 118개). 그 census 도 시드에 의존한다 — 같은 §7 참고.

점 하나 = **pano 하나**다. 한 pano 에서 여러 화각을 찍어도 점은 하나다
(그 수는 툴팁에 있다).

`--verdicts` 는 판정 JSONL(`camera_surface` 를 가진 줄들)이다. 주면 **판정
1건 = 마커 1개**로 바뀐다 — pano 에서 그 화각 방향으로 뻗은 짧은 선에
범주 색을 칠한다. 같은 pano 라도 화각마다 범주가 다를 수 있고, 점 하나로
뭉개면 그 차이가 사라진다 (그게 판정의 단위이기도 하다 → docs/22 §5).

merc/타일 패턴은 plot_course.py · plot_explore.py 와 같다 (stdlib only).
"""
import argparse
import base64
import collections
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

# camera_surface 범주별 색 (→ prompts/system_v4.txt · trailwalk/prompt.py).
# 색 묶음이 뜻을 나른다: 주황 = 차량이 다니는 노면, 파랑·초록 = 보행,
# 회색 = 보도(경계), 보라·갈색 = 판정 불가·길 아님.
SURFACE_COLOR = {
    "roadway": "#c2410c",
    "shared_alley": "#ea9a3e",
    "sidewalk": "#6b7280",
    "pedestrian_way": "#2563eb",
    "park_path": "#16a34a",
    "waterside": "#0891b2",
    "open_ground": "#a16207",
    "unclear": "#9333ea",
}


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


def load_verdicts(path: Path) -> list[dict]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    # 오류 줄(범주가 없다)은 지도에 못 찍는다. 조용히 버리지 않고 세어서
    # 범례에 띄운다 — 안 보이면 "그 자리엔 아무것도 없었다" 로 읽힌다
    return rows


def render(sets: list[dict], nodes: dict | None, verdicts: list[dict] | None = None,
           with_map: bool = True) -> str:
    center = nodes["center"] if nodes else None
    if verdicts:
        # 판정에 맞춰 확대한다. 수집 세트 전체(수천 pano)에 맞추면 판정 구간이
        # 깨알이 되어 색을 구분할 수 없다 — 세트와 census 는 여기서 맥락이라
        # 잘려 나가도 된다
        pts = [(v["lat"], v["lng"]) for v in verdicts]
    else:
        pts = [(p["lat"], p["lng"]) for s in sets for p in s["panos"].values()]
        pts += [tuple(s["start"]) for s in sets]
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
    # 판정을 겹칠 때는 이 층이 맥락이라 작고 흐리게 간다
    r_set, op_set = (2.0, 0.35) if verdicts else (3.4, 1.0)
    for i, s in enumerate(sets):
        col = SETS[i % len(SETS)]
        for pid, p in s["panos"].items():
            a, b = sxy(p["lat"], p["lng"])
            o.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="{r_set}" fill="{col}" '
                     f'opacity="{op_set}" stroke="#ffffff" stroke-width="1.1">'
                     f'<title>{pid}  depth {p["depth"]}  {p["n"]}장</title></circle>')
        a, b = sxy(*s["start"])
        o.append(f'<path d="M {a:.1f} {b - 11:.1f} l 7 12 l -14 0 z" fill="{col}" '
                 f'stroke="#ffffff" stroke-width="1.6"/>')

    # ── 판정 (있으면 이 층이 주인공) ───────────────────────────────────
    if verdicts:
        for v in verdicts:
            surface = v.get("camera_surface")
            col = SURFACE_COLOR.get(surface, "#000000")
            a, b = sxy(v["lat"], v["lng"])
            if surface is None:              # 오류 — 자리를 비우지 않는다
                o.append(f'<path d="M {a - 4:.1f} {b - 4:.1f} l 8 8 M {a + 4:.1f} '
                         f'{b - 4:.1f} l -8 8" stroke="#d03b3b" stroke-width="2">'
                         f'<title>{v.get("file")}  {v.get("error", "판정 없음")}</title></path>')
                continue
            # 화각 방향으로 뻗은 짧은 선. 방위는 북=0 시계방향, 화면은 북이 -y
            rad = math.radians(v.get("heading", 0.0))
            dx, dy = math.sin(rad) * 9, -math.cos(rad) * 9
            o.append(f'<line x1="{a:.1f}" y1="{b:.1f}" x2="{a + dx:.1f}" '
                     f'y2="{b + dy:.1f}" stroke="{col}" stroke-width="2.2" '
                     f'stroke-linecap="round" opacity="0.9"/>')
            o.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.2" fill="{col}" '
                     f'stroke="#ffffff" stroke-width="1.2">'
                     f'<title>{v.get("file")}\n{surface}  heading '
                     f'{v.get("heading")}  {v.get("dist_m")}m</title></circle>')

    def label(x, y, t, size=12, bold=""):
        return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{INK}" {bold} '
                f'stroke="#ffffff" stroke-width="3.5" paint-order="stroke">{t}</text>')

    o.append(label(16, 26,
                   "판정한 화각 — 선이 카메라가 본 방향" if verdicts
                   else "수집한 pano — 점 하나가 pano 하나", 14,
                   'font-weight="bold"'))
    yy = 46
    if verdicts:
        cnt = collections.Counter(v.get("camera_surface") for v in verdicts)
        for surface, n in cnt.most_common():
            col = SURFACE_COLOR.get(surface, "#d03b3b")
            o.append(f'<line x1="17" y1="{yy - 4}" x2="29" y2="{yy - 4}" '
                     f'stroke="{col}" stroke-width="3" stroke-linecap="round"/>')
            o.append(label(34, yy, f"{surface or '판정 없음'} {n}", 11))
            yy += 16
        yy += 4
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
    ap.add_argument("--verdicts", default=None,
                    help="판정 JSONL (camera_surface). 주면 판정 1건 = 마커 1개")
    ap.add_argument("--no-map", action="store_true", help="타일 없이")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    sets = [load_manifest(Path(d)) for d in a.dirs]
    nodes = json.loads(Path(a.nodes).read_text(encoding="utf-8")) if a.nodes else None
    verdicts = load_verdicts(Path(a.verdicts)) if a.verdicts else None
    Path(a.out).write_text(render(sets, nodes, verdicts, not a.no_map), encoding="utf-8")
    for s in sets:
        print(f"  {s['name']}: pano {len(s['panos'])} · {s['views']}장")
    if verdicts:
        cnt = collections.Counter(v.get("camera_surface") for v in verdicts)
        print(f"  판정 {len(verdicts)}건: "
              + " · ".join(f"{k or '오류'} {n}" for k, n in cnt.most_common()))
    print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
