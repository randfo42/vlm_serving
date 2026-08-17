#!/usr/bin/env python
"""서울 테마산책길 목록 수집 — gil.seoul.go.kr → trails.json

    python app/labels/fetch_gil_seoul.py             # 목록만 (15페이지, ~15초)
    python app/labels/fetch_gil_seoul.py --detail    # 경유지까지 (150페이지, ~3분)

평가의 정답이 되는 "어디가 산책로인가" 의 출처다. 서울시가 2015~2018년에 선정한
5개 테마 150개소, 총 428.3km.

### 얻는 것과 못 얻는 것

목록 페이지 하나에 이름·테마·난이도·거리·소요시간·자치구·스마트서울맵 링크가
전부 들어 있다. 상세 페이지를 더 봐야 나오는 것은 경유지 이름과 진입 방법뿐이다.

**좌표(폴리라인)는 어디에도 없다.** 상세 페이지의 다운로드는 GPX 가 아니라
코스 지도 PDF 이고, 공공데이터포털의 전국길관광정보표준데이터에도 경로 좌표가
없다. 스마트서울맵(map.seoul.go.kr)이 코스 선을 실제로 그리므로 거기서 꺼내는
것이 남은 길인데, 아직 풀지 못했다. → app/docs/22-labels.md §3

그래서 이 스크립트가 만드는 것은 **좌표 없는 산책로 대장**이다. 여기서 바로
평가 세트가 나오지는 않는다. 시작점 좌표를 붙이는 방법은 22-labels.md 참조.
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://gil.seoul.go.kr"
LIST = BASE + "/trail/list.do?key=2406040004&pageIndex={}"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trailwalk-labels/1.0"}

THEMES = {"SE001": "숲이 좋은 길", "SE002": "역사문화길", "SE003": "전망이 좋은 길",
          "SE004": "한강·하천이 좋은 길", "SE005": "계곡이 좋은 길"}

_ITEM = re.compile(r'<li class="\w+">(.*?)</li>', re.S)


def get(url: str, tries: int = 3) -> str:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise AssertionError


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_list(page_html: str) -> list[dict]:
    out = []
    for blk in _ITEM.findall(page_html):
        m = re.search(r'href="(/trail/view\.do\?key=(\d+)&sc_trailSn=(\d+)&sc_trailSeCd=(\w+))"', blk)
        if not m:
            continue
        name = re.search(r'<span class="ellipsis1">(.*?)</span>', blk, re.S)
        diff = re.search(r'<span class="img">\s*<span class="\w+">(.*?)</span>', blk, re.S)
        km = re.search(r'class="km">.*?</img>?\s*([\d.]+)\s*km', blk, re.S) \
            or re.search(r'class="km">.*?>\s*([\d.]+)\s*km', blk, re.S)
        tm = re.search(r'class="time">(.*?)</span>', blk, re.S)
        # 자치구 칸. 대개 "동대문구" 한 덩어리지만 여러 구에 걸친 길("마포구,용산구")과
        # 서울 밖("과천시")이 있어 구/시로 제한하지 않고 통째로 받는다.
        gu = re.search(r'</a>\s*([^<]{1,40}?)\s*</span>', blk[blk.find('class="blank"'):]) \
            if 'class="blank"' in blk else None
        smgis = re.search(r'href="(https://map\.seoul\.go\.kr/[^"]+)"', blk)
        out.append({
            "trail_sn": int(m.group(3)),
            "name": _text(name.group(1)) if name else None,
            "theme_code": m.group(4),
            "theme": THEMES.get(m.group(4), m.group(4)),
            "difficulty": _text(diff.group(1)) if diff else None,
            "distance_km": float(km.group(1)) if km else None,
            "duration": _text(tm.group(1)) if tm else None,
            "gu": gu.group(1) if gu else None,
            "detail_url": BASE + html.unescape(m.group(1)),
            # 스마트서울맵 단축 링크. 지금은 사람이 눌러 보는 용도이지만, 코스
            # 폴리라인을 꺼낼 때 이게 유일한 손잡이가 된다 (22-labels.md §3).
            "smgis_url": smgis.group(1) if smgis else None,
        })
    return out


def parse_detail(page_html: str) -> dict:
    """경유지 이름과 진입 방법. 좌표는 여기에도 없다."""
    body = re.sub(r"<(script|style).*?</\1>", "", page_html, flags=re.S)
    wp = re.search(r'<ul class="course_list">(.*?)</ul>', body, re.S) \
        or re.search(r'class="way_list">(.*?)</ul>', body, re.S)
    waypoints = []
    if wp:
        waypoints = [_text(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", wp.group(1), re.S)]
    pdf = re.search(r'href="(/common/file/download\.do\?enc=[^"]+)"', body)
    tel = re.search(r"(\d{2,4}-\d{3,4}-\d{4})", _text(body))
    return {"waypoints": [w for w in waypoints if w],
            "pdf_url": BASE + html.unescape(pdf.group(1)) if pdf else None,
            "tel": tel.group(1) if tel else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detail", action="store_true", help="상세 페이지 150건까지 훑는다 (느림)")
    ap.add_argument("--pages", type=int, default=15)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "trails.json"))
    ap.add_argument("--delay", type=float, default=0.4, help="요청 간 대기 (초)")
    a = ap.parse_args()

    trails: list[dict] = []
    for page in range(1, a.pages + 1):
        items = parse_list(get(LIST.format(page)))
        if not items:
            print(f"  page {page}: 항목 없음 — 마지막 페이지로 보고 중단", file=sys.stderr)
            break
        trails.extend(items)
        print(f"  page {page:2d}: {len(items):2d}건 (누적 {len(trails)})", file=sys.stderr)
        time.sleep(a.delay)

    if a.detail:
        for i, t in enumerate(trails, 1):
            try:
                t.update(parse_detail(get(t["detail_url"])))
            except Exception as e:
                print(f"  ! {t['name']}: {type(e).__name__}", file=sys.stderr)
            if i % 20 == 0:
                print(f"  상세 {i}/{len(trails)}", file=sys.stderr)
            time.sleep(a.delay)

    out = Path(a.out)
    out.write_text(json.dumps(
        {"source": BASE + "/trail/list.do?key=2406040004",
         "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
         "count": len(trails), "trails": trails},
        ensure_ascii=False, indent=2), encoding="utf-8")

    by_theme: dict[str, int] = {}
    for t in trails:
        by_theme[t["theme"]] = by_theme.get(t["theme"], 0) + 1
    total_km = sum(t["distance_km"] or 0 for t in trails)
    print(f"\n{len(trails)}건 · {total_km:.1f}km → {out}")
    for k, v in sorted(by_theme.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v:3d}")
    missing = [k for k in ("name", "distance_km", "gu", "smgis_url")
               if sum(1 for t in trails if not t.get(k))]
    if missing:
        for k in missing:
            n = sum(1 for t in trails if not t.get(k))
            print(f"  ⚠ {k} 누락 {n}건 — 파서가 페이지 변경을 놓쳤을 수 있다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
