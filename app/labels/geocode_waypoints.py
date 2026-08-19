#!/usr/bin/env python
"""경유지명 → 좌표. courses.json → waypoints.json

    python app/labels/geocode_waypoints.py

라벨 파이프라인 1단계 (← fetch_jongno.py, → fetch_walk_routes.py).
Kakao 로컬 키워드 검색으로 경유지 이름을 좌표로 바꾼다.

### 오매칭을 막는 두 장치

1. **직전 경유지 좌표로 바이어스** (`x,y` + `sort=distance`) — "청운공원" 같은
   흔한 이름이 딴 동네 동명 POI 로 튀는 것을 막는다. 첫 경유지는 종로구청
   좌표 + 반경 5km.
2. **연속 경유지 거리 새너티** — 이웃 경유지가 2km 넘게 떨어지면 경고.
   산책 코스 경유지가 2km 이상 벌어질 리 없다 (코스 전체가 2~4km 다).

### 실패는 사람이 메운다

검색이 안 되는 이름("이빨바위")은 status=missing 으로 남고 종료코드 1.
`overrides.json` 에 좌표를 넣고 재실행한다 (멱등):

    {"jongno-01/이빨바위": {"lat": 37.5, "lng": 126.9, "note": "카카오맵에서 수동"},
     "jongno-03/혜화문":   {"skip": true, "note": "경유지에서 제외"}}

결과의 모든 항목에 check_url(카카오맵 링크)이 붙는다 — **전 건을 눌러서
눈으로 확인하는 것**이 이 단계의 통과 조건이다.
"""
import argparse
import json
import re
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
from trailwalk.geo import haversine_m

SEARCH = "https://dapi.kakao.com/v2/local/search/keyword.json"
SEOUL_CENTER = (37.566535, 126.977969)    # 서울시청 — 구청 좌표를 모를 때의 시드
MAX_GAP_M = 2000.0


_GU_CACHE: dict[str, tuple[float, float]] = {}


def gu_center(key: str, gu: str | None) -> tuple[float, float]:
    """자치구 중심(구청) 좌표. 첫 경유지의 검색 바이어스로 쓴다.

    150개 코스가 서울 전역에 흩어져 있어 고정 중심(시청)으로는 외곽 자치구의
    첫 경유지가 반경 밖으로 밀린다. 구 이름은 대장에 이미 있다.
    """
    if not gu:
        return SEOUL_CENTER
    first = gu.split(",")[0].strip().split()[-1]   # "경기도 과천시" → "과천시"
    if first in _GU_CACHE:
        return _GU_CACHE[first]
    try:
        docs = _query(key, {"query": f"{first}청", "size": 1})
        if docs:
            _GU_CACHE[first] = (float(docs[0]["y"]), float(docs[0]["x"]))
        else:
            _GU_CACHE[first] = SEOUL_CENTER
    except Exception:
        _GU_CACHE[first] = SEOUL_CENTER
    time.sleep(0.15)
    return _GU_CACHE[first]


def gu_of(doc: dict) -> str:
    """검색 결과의 자치구명. address_name 은 "서울 강남구 …" 꼴이다."""
    addr = doc.get("address_name") or doc.get("road_address_name") or ""
    m = re.search(r"([가-힣]+[구시군])", addr)
    return m.group(1) if m else ""


def _query(key: str, params: dict) -> list[dict]:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{SEARCH}?{q}",
                                 headers={"Authorization": f"KakaoAK {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("documents", [])


# 변형 질의에서 떼어보는 꼬리말. "세검정터"→"세검정", "북악팔각정가는길"→"북악팔각정".
# 원문 질의가 항상 먼저이고, 변형은 원문이 실패하거나 매칭이 엉성할 때만 쓴다 —
# "청계광장"의 "광장"이 떨어져 나가는 일은 원문이 정확 매칭되는 한 없다.
_TAILS = ("방향", "가는길", "등산로입구", "입구", "정상", "광장", "터", "길")


def variants(name: str) -> list[str]:
    """검색해 볼 이름들. 덜 변형된 것이 앞이다."""
    out: list[str] = []

    def add(v: str) -> None:
        v = v.strip()
        if len(v) >= 2 and v not in out:
            out.append(v)

    add(name)
    add(re.sub(r"\s*\d+번\s*출구$", "", name))         # "경복궁역1번출구" → "경복궁역"
    add(re.sub(r"\([^)]*\)", "", name))              # 괄호 밖
    for inner in re.findall(r"\(([^)]*)\)", name):   # 괄호 안 (쉼표 분리)
        for part in inner.split(","):
            add(part)
    for base in list(out):                            # 각각에 꼬리말 제거를 얹는다
        v = base
        for _ in range(2):
            for t in _TAILS:
                if v.endswith(t) and len(v) - len(t) >= 2:
                    v = v[: -len(t)].strip()
                    add(v)
                    break
    for base in list(out):                            # 공백 분리 토막
        for part in base.split():
            if len(part) >= 3:
                add(part)
    return out


def _score(query: str, doc: dict) -> int:
    """이름 일치 품질. 클수록 좋다."""
    poi = doc["place_name"].replace(" ", "")
    q = query.replace(" ", "")
    if poi == q:
        return 3
    if poi.startswith(q) or q.startswith(poi):
        return 2
    if q in poi:
        return 1
    return 0


def search(key: str, name: str, near: tuple[float, float], cap_m: float,
           seed: tuple[float, float]) -> tuple[dict, str, bool] | None:
    """경유지명 하나를 푼다. 반환: (문서, 쓰인 질의, 약한 매칭 여부) 또는 None.

    변형 질의마다 거리 바이어스 검색과 정확도 검색을 다 모아, (덜 변형된 질의,
    이름 일치 품질, 바이어스 근접) 순으로 최선을 고른다. 거리 정렬만 쓰면
    "청계광장"이 근처 상호("○○ 청계광장시장점")에 잡힌다 — 실제로 그랬다.

    cap_m 밖의 후보는 이름이 정확히 맞아도 버린다. "구름다리"가 9km 밖
    동명 다리에 정확 일치로 잡혔고, 오염된 좌표가 다음 경유지의 바이어스로
    연쇄됐다 — 경유지는 코스 안에 있으므로 직전 경유지에서 코스 전장(cap_m)
    보다 멀 수 없다.

    이름이 하나도 안 맞으면(개명된 POI) 원문 질의의 거리 검색 1위를 **약한
    매칭**으로 받되 표시한다 — check_url 눈검증 게이트가 있어 허용한다.
    """
    best: tuple[tuple[int, int, float], dict, str] | None = None
    fallback: dict | None = None
    for vi, q in enumerate(variants(name)):
        docs = _query(key, {"query": q, "x": f"{near[1]:.7f}", "y": f"{near[0]:.7f}",
                            "radius": 5000, "sort": "distance", "size": 5})
        time.sleep(0.15)
        docs += _query(key, {"query": q, "x": f"{seed[1]:.7f}",
                             "y": f"{seed[0]:.7f}", "radius": 8000,
                             "sort": "accuracy", "size": 5})
        time.sleep(0.15)
        for doc in docs:
            d = haversine_m(near, (float(doc["y"]), float(doc["x"])))
            if d > cap_m:
                continue
            if vi == 0 and fallback is None:
                fallback = doc
            sc = _score(q, doc)
            if sc == 0:
                continue
            rank = (-vi, sc, -d)
            if best is None or rank > best[0]:
                best = (rank, doc, q)
        if best is not None and best[0][1] == 3:      # 이 변형에서 정확 일치 — 충분하다
            break
    if best is not None:
        return best[1], best[2], False
    if fallback is not None:
        return fallback, name, True
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ds.add_argument(ap)
    a = ap.parse_args()
    st = settings_mod.load()
    paths = ds.resolve(a.dataset or st.labels.dataset)

    key = kakao_rest_key()
    data = json.loads(paths.courses.read_text(encoding="utf-8"))
    overrides = json.loads(paths.overrides.read_text(encoding="utf-8")) \
        if paths.overrides.exists() else {}

    out_courses, n_missing, n_warn, n_incomplete = [], 0, 0, 0
    for course in data["courses"]:
        cid = course["course_id"]
        cap_m = max(2500.0, (course.get("distance_km") or 0) * 1000)
        gu = course.get("gu")
        seed = gu_center(key, gu)
        prev: tuple[float, float] | None = None
        rows = []
        for name in course["waypoints"]:
            ov = overrides.get(f"{cid}/{name}")
            if ov and ov.get("skip"):
                continue
            row: dict = {"name": name}
            if ov:
                row |= {"lat": ov["lat"], "lng": ov["lng"], "status": "override",
                        "matched_poi": ov.get("note", "수동")}
            else:
                # 첫 경유지의 바이어스는 구청이라 코스 전장 상한이 안 맞는다
                # (평창동 코스는 구청에서 5km). 구 반경으로 느슨하게 잡는다.
                hit = search(key, name, prev or seed,
                             cap_m if prev is not None else 10_000.0, seed)
                if hit is None:
                    row["status"] = "missing"
                    n_missing += 1
                    rows.append(row)
                    continue
                doc, used_q, weak = hit
                row |= {"lat": float(doc["y"]), "lng": float(doc["x"]),
                        "status": "geocoded", "matched_poi": doc["place_name"]}
                if used_q != name:
                    row["query_used"] = used_q
                if weak:
                    row["match"] = "weak"
                    n_warn += 1
                # 자치구 대조 — 종로에서 "구름다리"가 9km 밖 동명 다리에 정확
                # 일치로 잡혔던 사고를 거리 상한보다 직접적으로 잡는다.
                # 여러 구에 걸친 코스("마포구,용산구")와 서울 밖은 통과시킨다.
                found = gu_of(doc)
                # "경기도 과천시" 처럼 도 이름이 붙어 오고, 여러 구에 걸친
                # 코스("마포구,용산구")도 있다. 마지막 토큰끼리 비교한다.
                want = {g.strip().split()[-1] for g in gu.split(",")} if gu else set()
                if want and found and found not in want:
                    row["gu_found"] = found
                    n_warn += 1
            if prev is not None:
                gap = haversine_m(prev, (row["lat"], row["lng"]))
                if gap > MAX_GAP_M:
                    row["warn_gap_m"] = round(gap)
                    n_warn += 1
            row["check_url"] = ("https://map.kakao.com/link/map/"
                                + urllib.parse.quote(name)
                                + f",{row['lat']},{row['lng']}")
            prev = (row["lat"], row["lng"])
            rows.append(row)
        # 코스 단위 격리. **경유지 2개면 구간이 하나 나오므로 쓸 수 있다** —
        # 한 건 실패로 코스를 통째로 버리면 817경유지에서 대부분을 잃는다
        # (실측: 전건 성공 요구 시 54/150, 2개 이상 요구 시 128/150).
        n_ok = sum(1 for r in rows if r["status"] != "missing")
        n_gu = sum(1 for r in rows if r.get("gu_found"))
        if n_ok < 2:
            status = "incomplete"           # 구간을 만들 수 없다
        elif n_ok and n_gu / n_ok > 0.5:
            # 절반 넘게 딴 자치구에 잡혔다 = 경유지 이름이 검색 가능한 POI 가
            # 아니다 (예: 문화비축기지의 "T1"~"T6"). 좌표를 믿을 수 없다.
            status = "suspect_geocode"
        else:
            status = "ok"
        if status != "ok":
            n_incomplete += 1
        # 경유지 직선 연결 길이 / 공식 거리 — 코스당 스칼라 하나로 훑는다
        pts = [(r["lat"], r["lng"]) for r in rows if r["status"] != "missing"]
        chain_m = sum(haversine_m(x, y) for x, y in pairwise(pts))
        km = course.get("distance_km") or 0
        out_courses.append({"course_id": cid, "name": course["name"],
                            "gu": gu, "theme": course.get("theme"),
                            "status": status,
                            "n_geocoded": n_ok, "n_missing": len(rows) - n_ok,
                            "n_gu_mismatch": n_gu,
                            "chain_m": round(chain_m),
                            "chain_ratio": round(chain_m / (km * 1000), 2) if km else None,
                            "waypoints": rows})
        marks = " ".join(
            {"geocoded": "·", "override": "o", "missing": "X"}[r["status"]]
            + ("?" if r.get("match") == "weak" else "")
            + ("!" if "warn_gap_m" in r else "") for r in rows)
        print(f"{cid} {course['name']}: {marks}")

    paths.report.write_text(
        "course_id\tname\tgu\ttheme\tstatus\tn_wp\tmissing\tweak\tgu_mismatch\tchain_ratio\n"
        + "".join(
            f"{c['course_id']}\t{c['name']}\t{c.get('gu') or ''}\t{c.get('theme') or ''}\t"
            f"{c['status']}\t{len(c['waypoints'])}\t{c['n_missing']}\t"
            f"{sum(1 for r in c['waypoints'] if r.get('match') == 'weak')}\t"
            f"{c['n_gu_mismatch']}\t"
            f"{c['chain_ratio']}\n" for c in out_courses),
        encoding="utf-8")
    paths.waypoints.write_text(json.dumps({
        "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "bias": "직전 경유지 (첫 경유지는 자치구청 + 반경 5km)",
        "dataset": paths.name,
        "courses": out_courses,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {paths.waypoints}\nwrote {paths.report}  (코스별 점검표)")
    if n_warn:
        # 경고(약한 매칭 ?, 장구간 !)는 막지 않는다 — check_url 눈검증 게이트가
        # 잡을 대상 표시다. 개명된 POI(어린이도서관)와 경유지 2개짜리 장코스
        # (청계천길)처럼 정당한 경우가 실제로 있다.
        print(f"경고 {n_warn}건 — waypoints.json 의 ?/! 항목의 check_url 을 "
              f"먼저 확인할 것", file=sys.stderr)
    n_use = sum(1 for c in out_courses if c["status"] == "ok")
    print(f"쓸 수 있는 코스 {n_use}/{len(out_courses)} · 경유지 missing {n_missing}")
    if n_incomplete:
        print(f"제외 {n_incomplete}코스 (incomplete=경유지 2개 미만, "
              f"suspect_geocode=절반 넘게 딴 자치구). 살리려면 overrides.json 을 "
              f"채우고 재실행 (형식은 이 파일 독스트링)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
