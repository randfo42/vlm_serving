#!/usr/bin/env python
"""코스 폴리라인 (+샘플 후보) → 지도 위 SVG. 게이트 2b/3a 의 눈검증 도구다.

    python app/eval/plot_course.py app/labels/jongno/courses_geom.json jongno-01
    python app/eval/plot_course.py app/labels/jongno/courses_geom.json jongno-01 \\
        --samples app/labels/jongno/samples.jsonl -o /tmp/c1.svg

확인할 것: **선이 실제 길 위를 따라가는가** (도보 라우터가 숲길 대신 옆
차도로 우회하지 않았는가 — app/docs/24-course-routes.md §6), 경유지 순서가
말이 되는가. --samples 를 주면 샘플 후보 점을 겹쳐 그려 간격·버퍼를 본다.

merc/타일 패턴은 plot_explore.py 와 같다 (stdlib only, 타일 base64 내장).
"""
import argparse
import base64
import json
import math
import sys
import urllib.request
from pathlib import Path

GOOD = "#0ca30c"      # 코스 폴리라인 / true 샘플
CRIT = "#d03b3b"      # false 샘플 (offroute)
WARN = "#b8860b"      # false 샘플 (같은 pano 이각: orth/rev)
MUTE = "#78787a"
INK = "#1a1a1a"

TILE = 256
UA = "trailwalk-plot/0.1 (local research; one-off dev rendering)"
# 타일 디스크 캐시. 150코스를 그리면 코스당 ~20타일 × 150 = 3,000 요청이고
# 인접 코스는 타일을 공유한다. OSM 타일 사용정책상 벌크 요청은 피해야 한다.
CACHE = Path(__file__).resolve().parent.parent / "labels" / ".tilecache"


def merc(lat: float, lng: float, z: int) -> tuple[float, float]:
    n = TILE * (1 << z)
    x = (lng + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def fetch_tile(z: int, tx: int, ty: int) -> bytes | None:
    cached = CACHE / str(z) / str(tx) / f"{ty}.png"
    if cached.exists():
        return cached.read_bytes()
    req = urllib.request.Request(
        f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except Exception:
        return None                # 타일 하나 빠져도 그림은 성립한다
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def load_samples(path: Path, course_id: str) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type", "sample") == "sample" and d.get("course_id") == course_id:
            rows.append(d)
    return rows


def render(course: dict, samples: list[dict], with_map: bool = True) -> str:
    pts: list[tuple[float, float]] = []
    polys = [([tuple(p) for p in s["polyline"]], s) for s in course["segments"]
             if s["status"] == "ok"]
    for poly, _ in polys:
        pts += poly
    pts += [(s["lat"], s["lng"]) for s in samples]
    if not pts:
        raise SystemExit(f"{course['course_id']}: 그릴 폴리라인이 없다 (전 구간 missing?)")

    for z in (17, 16, 15, 14):
        xs, ys = zip(*(merc(lat, lng, z) for lat, lng in pts), strict=True)
        if max(max(xs) - min(xs), max(ys) - min(ys)) <= 900:
            break
    pad = 70
    w = max(640, int(max(xs) - min(xs)) + 2 * pad)
    h = max(520, int(max(ys) - min(ys)) + 2 * pad + 40)
    ox = (max(xs) + min(xs)) / 2 - w / 2
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
        s.append(f'<rect width="{w}" height="{h}" fill="#ffffff" opacity="0.35"/>')

    # 구간 폴리라인
    for poly, seg in polys:
        d = "M " + " L ".join(f"{sxy(la, ln)[0]:.1f} {sxy(la, ln)[1]:.1f}"
                              for la, ln in poly)
        s.append(f'<path d="{d}" fill="none" stroke="{GOOD}" stroke-width="3.5" '
                 f'stroke-linecap="round" stroke-linejoin="round" opacity="0.85">'
                 f'<title>{seg["from"]} → {seg["to"]}  {seg["length_m"]}m</title></path>')
        # 구간 경계 (경유지)
        for la, ln in (poly[0], poly[-1]):
            a, b = sxy(la, ln)
            s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="5" fill="#ffffff" '
                     f'stroke="{INK}" stroke-width="2"/>')

    # 샘플 후보
    for sm in samples:
        a, b = sxy(sm["lat"], sm["lng"])
        src = sm.get("label_source", "route")
        if sm.get("label"):
            fill, r = GOOD, 3
        else:
            fill, r = (CRIT, 3.5) if src == "offroute" else (WARN, 3)
        s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="{r}" fill="{fill}" '
                 f'stroke="#ffffff" stroke-width="1">'
                 f'<title>{sm.get("sample_id", "?")} {src} h{sm.get("heading")}</title>'
                 f'</circle>')

    def label(x, y, t, size=12, bold=""):
        return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{INK}" {bold} '
                f'stroke="#ffffff" stroke-width="3.5" paint-order="stroke">{t}</text>')

    ok = sum(1 for sg in course["segments"] if sg["status"] == "ok")
    title = (f'{course["course_id"]} {course["name"]} — 구간 {ok}/'
             f'{len(course["segments"])} · {course["total_m"]}m')
    if samples:
        title += f' · 샘플 {len(samples)}'
    s.append(label(16, 26, title, 14, 'font-weight="bold"'))

    mpp = 40_075_016.686 * math.cos(math.radians(pts[0][0])) / (TILE * (1 << z))
    m = min((v for v in (50, 100, 200, 500, 1000) if v / mpp >= 60), default=1000)
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
    ap.add_argument("geom", help="courses_geom.json 경로")
    ap.add_argument("course_id", help="예: jongno-01, 또는 all")
    ap.add_argument("--samples", default=None, help="samples.jsonl 을 겹쳐 그린다")
    ap.add_argument("-o", "--out", default=None,
                    help="SVG 경로 (all 이면 디렉터리). 기본: geom 옆 svg/")
    ap.add_argument("--no-map", action="store_true", help="타일 배경 없이 (오프라인)")
    ap.add_argument("--dataset", default=None,
                    help="geom 대신 데이터셋 이름으로 경로를 잡는다")
    a = ap.parse_args()

    data = json.loads(Path(a.geom).read_text(encoding="utf-8"))
    courses = {c["course_id"]: c for c in data["courses"]}
    targets = list(courses) if a.course_id == "all" else [a.course_id]
    outdir = Path(a.out) if (a.out and a.course_id == "all") \
        else Path(a.geom).parent / "svg"

    for cid in targets:
        if cid not in courses:
            print(f"모르는 코스 {cid}. 있는 것: {', '.join(courses)}", file=sys.stderr)
            return 1
        samples = load_samples(Path(a.samples), cid) if a.samples else []
        svg = render(courses[cid], samples, with_map=not a.no_map)
        if a.out and a.course_id != "all":
            out = Path(a.out)
        else:
            outdir.mkdir(parents=True, exist_ok=True)
            out = outdir / f"{cid}.svg"
        out.write_text(svg, encoding="utf-8")
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
