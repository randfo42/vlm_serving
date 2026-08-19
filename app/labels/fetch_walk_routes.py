#!/usr/bin/env python
"""경유지 쌍 → 도보 경로 폴리라인. waypoints.json → courses_geom.json

    python app/labels/fetch_walk_routes.py

라벨 파이프라인 2단계 (← geocode_waypoints.py, → make_samples.py).

### 엔드포인트 (2026-08-19 관찰로 확정 → app/docs/24-course-routes.md)

카카오맵 웹의 도보 길찾기는

    GET https://map.kakao.com/route/walkset.json
        ?sName=&eName=&sX=&sY=&eX=&eY=&ids=%2C     (좌표는 WCongnamul)

이고 Referer: https://map.kakao.com/ 만 있으면 브라우저 없이 응답한다.
공식 REST 에는 도보 길찾기가 없어서(카카오모빌리티는 자동차뿐) 이 비공식
엔드포인트를 쓴다 — 로드뷰 이웃 그래프(providers/kakao.py `_sniff_node`)와
같은 성격의 비계약 의존이다. 형식이 바뀌면 파서가 **터지게** 되어 있다
(조용히 빈 폴리라인을 내지 않는다).

### 좌표계

요청·응답 모두 WCongnamul = EPSG:5181(중부원점, GRS80) × 2.5.
로컬 역변환(TM 급수)을 쓰되, **실행마다 REST transcoord 로 검증점 3개를
대조해 1m 이내를 확인한 뒤에만** 진행한다. 변환이 틀리면 폴리라인 전체가
조용히 밀리고, 그 위의 true 라벨이 통째로 오염되기 때문이다.

### 예의와 재실행

요청은 경유지 쌍당 1회 + 3초 대기. 응답 원본은 routes/ 에 캐시되어
재실행 시 **walkset 요청은** 나가지 않는다 (강제 재수집은 캐시 파일 삭제).
단, 좌표 역변환 검증(transcoord 3회)은 캐시 히트와 무관하게 매 실행 나간다 —
완전 오프라인 실행은 아니다.
"""
import argparse
import hashlib
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds

from trailwalk import settings as settings_mod
from trailwalk.config import kakao_rest_key
from trailwalk.geo import haversine_m, polyline_length_m

WALKSET = "https://map.kakao.com/route/walkset.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trailwalk-labels/1.0",
           "Referer": "https://map.kakao.com/"}
DELAY_S = 3.0
MERGE_M = 50.0        # 연속 경유지가 이보다 가까우면 하나로 (동일 POI 로 지오코딩된
                      # "말바위전망대/말바위/말바위등산로입구" 가 SAME_POINT 를 냈다)
SUSPECT_RATIO = 1.6   # 라우팅 합계가 공식 거리의 이 배수를 넘으면 코스를 의심 표시.
                      # 도보 라우터는 숲길·성곽길을 못 타고 도로로 우회한다 — 그
                      # 폴리라인 위의 점을 true 로 쓰면 라벨이 오염되므로
                      # make_samples 가 suspect 코스를 기본 제외한다 (§6)

# ── WCongnamul → WGS84 ─────────────────────────────────────────────────────
# WCongnamul = EPSG:5181 (TM 중부원점: lat0 38, lon0 127, k0 1,
# FE 200000, FN 500000, GRS80) 좌표에 2.5 를 곱한 것.
_A = 6_378_137.0                      # GRS80 장반경
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_E4, _E6 = _E2 * _E2, _E2 * _E2 * _E2
_LAT0, _LON0 = math.radians(38.0), math.radians(127.0)
_K0, _FE, _FN = 1.0, 200_000.0, 500_000.0


def _merid(lat: float) -> float:
    return _A * ((1 - _E2 / 4 - 3 * _E4 / 64 - 5 * _E6 / 256) * lat
                 - (3 * _E2 / 8 + 3 * _E4 / 32 + 45 * _E6 / 1024) * math.sin(2 * lat)
                 + (15 * _E4 / 256 + 45 * _E6 / 1024) * math.sin(4 * lat)
                 - (35 * _E6 / 3072) * math.sin(6 * lat))


def wcongnamul_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """WCongnamul (x, y) → (lat, lng). 표준 TM 역변환 급수."""
    e, n = x / 2.5, y / 2.5
    m = _merid(_LAT0) + (n - _FN) / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E4 / 64 - 5 * _E6 / 256))
    s1 = math.sqrt(1 - _E2)
    e1 = (1 - s1) / (1 + s1)
    lat1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    ep2 = _E2 / (1 - _E2)
    c1 = ep2 * math.cos(lat1) ** 2
    t1 = math.tan(lat1) ** 2
    n1 = _A / math.sqrt(1 - _E2 * math.sin(lat1) ** 2)
    r1 = _A * (1 - _E2) / (1 - _E2 * math.sin(lat1) ** 2) ** 1.5
    d = (e - _FE) / (n1 * _K0)
    lat = lat1 - (n1 * math.tan(lat1) / r1) * (
        d * d / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * ep2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = _LON0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                   + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * ep2 + 24 * t1 * t1)
                   * d ** 5 / 120) / math.cos(lat1)
    return math.degrees(lat), math.degrees(lon)


def verify_transform(key: str, samples: list[tuple[float, float]]) -> None:
    """검증점들을 REST transcoord 와 대조. 1m 넘게 어긋나면 즉시 중단."""
    for x, y in samples:
        q = urllib.parse.urlencode({"x": x, "y": y, "input_coord": "WCONGNAMUL",
                                    "output_coord": "WGS84"})
        req = urllib.request.Request(
            f"https://dapi.kakao.com/v2/local/geo/transcoord.json?{q}",
            headers={"Authorization": f"KakaoAK {key}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.load(r)["documents"][0]
        ours = wcongnamul_to_wgs84(x, y)
        gap = haversine_m(ours, (float(doc["y"]), float(doc["x"])))
        if gap > 1.0:
            raise SystemExit(f"FATAL: 좌표 역변환이 transcoord 와 {gap:.2f}m 어긋난다 "
                             f"(WCongnamul {x},{y}) — 폴리라인 전체가 오염되므로 중단")
        time.sleep(0.15)


# ── walkset 호출·파싱 ──────────────────────────────────────────────────────

def fetch_pair(sx: float, sy: float, ex: float, ey: float) -> dict:
    q = urllib.parse.urlencode({"sName": "s", "eName": "e",
                                "sX": round(sx), "sY": round(sy),
                                "eX": round(ex), "eY": round(ey), "ids": ","})
    req = urllib.request.Request(f"{WALKSET}?{q}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_walkset(raw: dict) -> tuple[list[tuple[float, float]], int, int]:
    """응답 → (WCongnamul 점열, length_m, time_s). 형식이 다르면 KeyError 로 터진다."""
    d = raw["directions"][0]
    if not d.get("success") or d.get("resultCode") != "SUCCESS":
        raise ValueError(f"경로 실패: resultCode={d.get('resultCode')!r}")
    pts: list[tuple[float, float]] = []
    for sec in d["sections"]:
        for g in sec["guideList"]:
            link = g.get("link")
            if not link or not link.get("points"):
                continue                      # 도착 가이드는 link 가 없다
            for tok in link["points"].split("|"):
                xs, ys = tok.split(",")
                p = (float(xs), float(ys))
                if not pts or pts[-1] != p:   # 링크 경계의 중복점 제거
                    pts.append(p)
    if len(pts) < 2:
        raise ValueError("폴리라인이 비었다 — 응답 형식이 바뀌었는지 확인할 것")
    return pts, int(d["length"]), int(d["time"])


def wgs_to_wc(key: str, lat: float, lng: float) -> tuple[float, float]:
    q = urllib.parse.urlencode({"x": lng, "y": lat, "input_coord": "WGS84",
                                "output_coord": "WCONGNAMUL"})
    req = urllib.request.Request(
        f"https://dapi.kakao.com/v2/local/geo/transcoord.json?{q}",
        headers={"Authorization": f"KakaoAK {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.load(r)["documents"][0]
    return float(doc["x"]), float(doc["y"])


def cache_name(cid: str, a: dict, b: dict) -> str:
    """구간의 캐시 파일명. 좌표 해시라 경유지 병합/skip 에도 안정적이다."""
    sig = hashlib.sha1(
        f"{a['lat']:.6f},{a['lng']:.6f}-{b['lat']:.6f},{b['lng']:.6f}"
        .encode()).hexdigest()[:10]
    return f"{cid}_{sig}.json"


def effective_waypoints(course: dict, verbose: bool = False) -> list[dict]:
    """실제 라우팅에 쓰이는 경유지 열: missing 제외 + 50m 이내 연속 병합.

    main() 과 print_keys() 가 반드시 같은 열을 봐야 한다 — 갈라지면 --keys 가
    병합 구간에서 존재하지 않는 파일명을 알려주고, 그 이름으로 만든 수동
    트레이스는 조용히 무시된다 (리뷰 지적, jongno-05 서시정≈시인의언덕 실측).
    """
    wps = [w for w in course["waypoints"] if w["status"] != "missing"]
    merged = [wps[0]] if wps else []
    for w in wps[1:]:
        gap = haversine_m((merged[-1]["lat"], merged[-1]["lng"]),
                          (w["lat"], w["lng"]))
        if gap < MERGE_M:
            if verbose:
                print(f"  {course['course_id']}: {merged[-1]['name']} ≈ "
                      f"{w['name']} ({gap:.0f}m) — 병합")
            continue
        merged.append(w)
    return merged


def print_keys(paths: ds.DatasetPaths) -> int:
    """수동 트레이스를 넣을 때 필요한 캐시 파일명 목록 (→ 24-course-routes.md §5)."""
    data = json.loads(paths.waypoints.read_text(encoding="utf-8"))
    for course in data["courses"]:
        cid = course["course_id"]
        for wa, wb in pairwise(effective_waypoints(course)):
            print(f"{cid}  {wa['name']} → {wb['name']}: "
                  f"routes/{cache_name(cid, wa, wb)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keys", action="store_true",
                    help="구간별 캐시 파일명만 출력 (수동 트레이스용)")
    st = settings_mod.load()
    ds.add_argument(ap, st)
    a = ap.parse_args()
    paths = ds.resolve(a.dataset or st.labels.dataset)
    if a.keys:
        return print_keys(paths)

    key = kakao_rest_key()
    data = json.loads(paths.waypoints.read_text(encoding="utf-8"))
    official = {c["course_id"]: c.get("distance_km")
                for c in json.loads(paths.courses.read_text(encoding="utf-8"))["courses"]}
    paths.routes_dir.mkdir(parents=True, exist_ok=True)

    out_courses, n_missing, verified = [], 0, False
    for course in data["courses"]:
        cid = course["course_id"]
        if course.get("status") == "incomplete":
            # 지오코딩이 완결되지 않은 코스는 라우팅 예산을 쓰지 않는다
            print(f"{cid} {course['name']}: 건너뜀 (지오코딩 incomplete)")
            continue
        wps = effective_waypoints(course, verbose=True)
        segments = []
        for wa, wb in pairwise(wps):
            # 캐시 키는 구간 인덱스가 아니라 좌표다 — 경유지 병합/skip 으로
            # 인덱스가 밀리면 캐시가 엉뚱한 구간에 붙는다.
            cache = paths.routes_dir / cache_name(cid, wa, wb)
            if cache.exists():
                raw = json.loads(cache.read_text(encoding="utf-8"))
            else:
                sx, sy = wgs_to_wc(key, wa["lat"], wa["lng"])
                time.sleep(0.15)
                ex, ey = wgs_to_wc(key, wb["lat"], wb["lng"])
                try:
                    raw = fetch_pair(sx, sy, ex, ey)
                except Exception as e:
                    print(f"  {cid} {wa['name']}→{wb['name']}: 요청 실패 {e}",
                          file=sys.stderr)
                    raw = {"_error": str(e)}
                cache.write_text(json.dumps(raw, ensure_ascii=False),
                                 encoding="utf-8")
                time.sleep(DELAY_S)
            seg = {"from": wa["name"], "to": wb["name"], "cache": cache.name}
            try:
                wc_pts, length_m, time_s = parse_walkset(raw)
            except (KeyError, ValueError, IndexError) as e:
                seg |= {"status": "missing", "error": str(e)}
                n_missing += 1
                segments.append(seg)
                continue
            if not verified:
                verify_transform(key, [wc_pts[0], wc_pts[len(wc_pts) // 2], wc_pts[-1]])
                verified = True
                print("좌표 역변환 검증 통과 (transcoord 대조 3점 < 1m)")
            poly = [wcongnamul_to_wgs84(x, y) for x, y in wc_pts]
            seg |= {"status": "ok", "length_m": length_m, "time_s": time_s,
                    "polyline": [[round(la, 7), round(ln, 7)] for la, ln in poly]}
            # 길이 새너티는 파일에 남긴다 — stderr 경고만으로는 자동화 실행에서
            # 증발한다 (리뷰 지적). 15% 는 walkset length 가 계단 가중치를
            # 포함해 재계산과 어긋나는 정상 범위를 실측으로 감안한 값.
            calc = polyline_length_m(poly)
            if length_m > 0 and abs(calc - length_m) / length_m > 0.15:
                seg |= {"length_recalc_m": round(calc), "warn_length": True}
                print(f"  경고 {cid} {wa['name']}→{wb['name']}: 응답 length "
                      f"{length_m}m vs 재계산 {calc:.0f}m", file=sys.stderr)
            segments.append(seg)
        total = sum(s.get("length_m", 0) for s in segments)
        ok = sum(1 for s in segments if s["status"] == "ok")
        km = official.get(cid)
        ratio = round(total / 1000 / km, 2) if km else None
        suspect = ratio is not None and ratio > SUSPECT_RATIO
        mark = f" · 공식 {km}km 의 {ratio}x" + ("  ⚠ suspect" if suspect else "") \
            if ratio else ""
        print(f"{cid} {course['name']}: 구간 {ok}/{len(segments)} · {total}m{mark}")
        out_courses.append({"course_id": cid, "name": course["name"],
                            "segments": segments, "total_m": total,
                            "official_km": km, "ratio": ratio,
                            "suspect": suspect})

    paths.geom.write_text(json.dumps({
        "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "crs": "WGS84", "endpoint_doc": "app/docs/24-course-routes.md",
        "dataset": paths.name,
        "courses": out_courses,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {paths.geom}")
    if n_missing:
        print(f"실패 구간 {n_missing}개 — courses_geom.json 의 status=missing. "
              f"수동 트레이스는 routes/ 에 source=manual 로 넣는다", file=sys.stderr)
    return 0 if n_missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
