#!/usr/bin/env python
"""종로구 걷기코스 수집 — jongno.go.kr → labels/jongno/courses.json

    python app/labels/fetch_jongno.py

평가 라벨 파이프라인의 0단계다 (→ 이후: geocode_waypoints.py).
서울 테마산책길 150개 대장(fetch_gil_seoul.py)과 달리 이쪽은 코스 9개뿐이지만
**경유지 텍스트가 목록에 바로 있다** — "택견수련터→수성동계곡→…" — 그래서
지오코딩 시드로 곧장 쓸 수 있다. 좌표·GPX 는 여기에도 없다.

### 페이지 구조 (2026-08 확인)

- 코스 페이지는 index_{1,2,3,5,7,9,10,11,12}.jsp 아홉 장. 빠진 번호(4,6,8)는
  코스가 아닌 안내 페이지다. 목록을 하드코딩하지 않고 **아무 페이지의 좌측
  내비게이션에서 코스 링크를 읽는다** — 코스가 늘거나 번호가 바뀌면 따라간다.
- 경유지는 `<li><span>코스경로</span><ul class="sub_font"><li>A→B→C</li>`.
  청계천길만 변형이 있다: "청계천길 [황학교 → 청계광장]" — 대괄호 안이 경유지다.
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://www.jongno.go.kr/fitness/sub04/"
ENTRY = BASE + "index_1.jsp"
OUT = Path(__file__).resolve().parent / "jongno" / "courses.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trailwalk-labels/1.0"}

# 내비게이션의 코스 링크. "추천1코스 : 인왕산숲길" 처럼 코스 번호가 붙은 것만 —
# 같은 내비에 있는 일반 안내 페이지를 거른다.
_NAV = re.compile(r'href="(index_\d+\.jsp)"[^>]*>\s*((?:추천|산책)\d+코스\s*:\s*[^<]+?)\s*<')

# 코스정보 블록의 <li><span>라벨</span><ul class="sub_font"><li>값</li>
_FIELD = re.compile(
    r'<li>\s*<span>([^<]+)</span>\s*<ul class="sub_font">\s*<li>(.*?)</li>', re.S)


def get(url: str, tries: int = 3) -> str:
    for i in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise AssertionError


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_nav(page_html: str) -> list[tuple[str, str]]:
    """내비게이션에서 (페이지 파일명, 코스 표기) 목록을 문서 순서로. 중복 제거."""
    seen, out = set(), []
    for href, label in _NAV.findall(page_html):
        if href not in seen:
            seen.add(href)
            out.append((href, _text(label)))
    return out


def parse_waypoints(route_text: str) -> list[str]:
    """'A→B→C' 또는 '이름 [A → B]' → 경유지 리스트."""
    m = re.search(r"\[(.+)\]", route_text)
    if m:
        route_text = m.group(1)
    parts = [p.strip() for p in re.split(r"→|➜|->", route_text)]
    return [p for p in parts if p]


def parse_course(page_html: str) -> dict:
    fields = {_text(k): _text(v) for k, v in _FIELD.findall(page_html)}
    route = fields.get("코스경로", "")
    km = re.search(r"([\d.]+)\s*km", fields.get("거리", ""))
    return {
        "route_text": route or None,
        "waypoints": parse_waypoints(route),
        "distance_km": float(km.group(1)) if km else None,
        "duration": fields.get("소요시간") or None,
        "course_type": fields.get("코스타입") or None,
        "difficulty": fields.get("난이도") or None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--delay", type=float, default=0.4, help="페이지 간 대기(초)")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()

    nav = parse_nav(get(ENTRY))
    if not nav:
        print("FATAL: 내비게이션에서 코스 링크를 하나도 못 찾았다 — 페이지 구조가 바뀌었다",
              file=sys.stderr)
        return 1
    print(f"코스 링크 {len(nav)}개")

    courses, missing = [], 0
    for i, (href, label) in enumerate(nav, 1):
        time.sleep(a.delay)
        c = parse_course(get(BASE + href))
        # "추천1코스 : 인왕산숲길" → 종별/이름 분리
        kind, _, name = (p.strip() for p in label.partition(":"))
        c = {"course_id": f"jongno-{i:02d}", "kind": kind, "name": name,
             "url": BASE + href, **c}
        for k in ("route_text", "distance_km", "duration"):
            if c[k] is None:
                missing += 1
                print(f"  누락 {c['course_id']} {name}: {k}", file=sys.stderr)
        if not c["waypoints"]:
            missing += 1
            print(f"  누락 {c['course_id']} {name}: waypoints", file=sys.stderr)
        print(f"  {c['course_id']} {name}: 경유지 {len(c['waypoints'])}개, "
              f"{c['distance_km']}km")
        courses.append(c)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({
        "source": ENTRY,
        "fetched_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "count": len(courses), "courses": courses,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {a.out}  (코스 {len(courses)}, 누락 필드 {missing})")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
