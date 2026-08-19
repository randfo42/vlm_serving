#!/usr/bin/env python
"""수집 퍼널 표. 대장 → 최종 샘플까지 테마별로 몇 개가 남았나.

    python app/labels/funnel.py --dataset seoul

`docs/22-labels.md` §9 의 표를 그대로 찍는다. 붙여넣기용이다.

### 왜 이 표가 필요한가

"왜 우리 평가 세트가 이 모양인가" 를 한 장으로 설명한다. 코스 150개로 시작해
최종 샘플까지 오는 동안 각 게이트가 무엇을 얼마나 걸렀는지 보이지 않으면,
나중에 나온 정확도 수치가 **어떤 모집단에 대한 것인지** 알 수 없다.

특히 보려는 것: **게이트들이 같은 테마를 무는가.** 도보 라우터가 산길을 못
타서 suspect 가 되는 코스와 로드뷰 커버리지가 없는 코스는 둘 다 "차가 못
들어가는 길" 이라 겹칠 것으로 예상된다. 겹치면 최종 세트가 하천·시가지 쪽으로
기울고, 그건 결과를 읽을 때 반드시 같이 읽어야 하는 사실이다.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds

from trailwalk import settings as settings_mod

STAGES = ("대장", "경유지있음", "지오코딩", "라우팅", "ratio통과", "커버리지", "최종샘플")


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def collect(paths: ds.DatasetPaths) -> tuple[dict[str, Counter], dict[str, int]]:
    """테마별 단계 카운트. 없는 단계는 그냥 0 으로 남는다 (중간 실행 지원)."""
    per: dict[str, Counter] = {}
    theme_of: dict[str, str] = {}

    def bump(cid: str, stage: str) -> None:
        t = theme_of.get(cid, "?")
        per.setdefault(t, Counter())[stage] += 1

    courses = _load(paths.courses)
    if not courses:
        raise SystemExit(f"{paths.courses} 가 없다")
    for c in courses["courses"]:
        theme_of[c["course_id"]] = c.get("theme") or "?"
        bump(c["course_id"], "대장")
        if c.get("status") == "ok":
            bump(c["course_id"], "경유지있음")

    wp = _load(paths.waypoints)
    if wp:
        for c in wp["courses"]:
            if c["status"] == "ok":
                bump(c["course_id"], "지오코딩")

    geom = _load(paths.geom)
    if geom:
        for c in geom["courses"]:
            if any(s["status"] == "ok" for s in c["segments"]):
                bump(c["course_id"], "라우팅")
            if not c.get("suspect"):
                bump(c["course_id"], "ratio통과")

    cov = _load(paths.coverage)
    if cov:
        for r in cov["courses"]:
            if r["on_route_ratio"] >= 0.5:
                bump(r["course_id"], "커버리지")

    # 최종 샘플은 코스 수가 아니라 **장 수**다. 따로 센다.
    shots: dict[str, int] = {}
    if paths.samples.exists():
        for line in paths.samples.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("type") == "sample":
                t = theme_of.get(d["course_id"], "?")
                shots[t] = shots.get(t, 0) + 1
    return per, shots


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ds.add_argument(ap)
    a = ap.parse_args()
    st = settings_mod.load()
    paths = ds.resolve(a.dataset or st.labels.dataset)
    per, shots = collect(paths)

    order = sorted(per, key=lambda t: -per[t]["대장"])
    head = "| 테마 | " + " | ".join(STAGES[:-1]) + " | 최종샘플(장) |"
    print(head)
    print("|---|" + "---:|" * len(STAGES))
    for t in order:
        cells = [str(per[t][s]) for s in STAGES[:-1]]
        print(f"| {t} | " + " | ".join(cells) + f" | {shots.get(t, 0)} |")
    tot = [sum(per[t][s] for t in per) for s in STAGES[:-1]]
    print("| **합계** | " + " | ".join(f"**{v}**" for v in tot)
          + f" | **{sum(shots.values())}** |")

    cov = _load(paths.coverage)
    geom = _load(paths.geom)
    if cov and geom:
        susp = {c["course_id"] for c in geom["courses"] if c.get("suspect")}
        low = {r["course_id"] for r in cov["courses"] if r["on_route_ratio"] < 0.5}
        if susp:
            print(f"\n두 게이트의 겹침: suspect {len(susp)}개 중 커버리지도 낮은 것 "
                  f"{len(susp & low)}개 ({len(susp & low) / len(susp):.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
