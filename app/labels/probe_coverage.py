#!/usr/bin/env python
"""로드뷰 커버리지 프로브. courses_geom.json → coverage.json

    python app/labels/probe_coverage.py --dataset seoul
    python app/labels/probe_coverage.py --dataset seoul --points 10

라벨 파이프라인 3.5단계 (← fetch_walk_routes.py, → make_samples.py).

### 왜 캡처 전에 재는가

**로드뷰는 차량 촬영이다.** 숲길·계곡길에는 커버리지가 없을 가능성이 크다
(`docs/02-open-questions.md` §2b). 캡처는 장당 ~3초라 커버리지 없는 코스에
들어가면 `no_pano` 카운터만 올리며 시간을 태운다. `nearest()` 만 부르면
장당 ~1초로 미리 알 수 있다.

그리고 **이 결과 자체가 산출물이다.** "어떤 테마가 로드뷰로 평가 가능한가" 는
애플리케이션의 적용 범위를 정하는 답이다 (→ `docs/22-labels.md` §9).

### 두 수를 따로 낸다 — 합치면 거짓말이 된다

- `pano_hit`  : `nearest(snap_radius_m)` 이 pano 를 준 비율
- `on_route`  : 그 pano 가 폴리라인에서 `snap_radius_m × 1.5` 이내인 비율

숲길 옆 **차도의** pano 가 30m 안에 있으면 `pano_hit` 은 잡히지만 그건 산책로가
아니다. `on_route` 가 진짜 커버리지이고, make_samples 의 `off_polyline` 판정을
캡처 전으로 당겨 하는 것과 같다.
"""
import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds
from labels.make_samples import course_candidates, head_polyline

from trailwalk import settings as settings_mod
from trailwalk.geo import point_to_polyline_m


def probe_points(course: dict, cfg, k: int) -> list[tuple[float, float]]:
    """코스에서 균등하게 k 점. 샘플링과 **같은 구간**(시작 head_m)을 본다."""
    cands = course_candidates(course, cfg)
    if not cands:
        return []
    if len(cands) <= k:
        return [(la, ln) for la, ln, _ in cands]
    step = len(cands) / k
    return [(cands[int(i * step)][0], cands[int(i * step)][1]) for i in range(k)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--points", type=int, default=8,
                    help="코스당 프로브 점 수 (기본 8)")
    ap.add_argument("--config", default=None)
    st_pre = settings_mod.load()
    ds.add_argument(ap, st_pre)
    a = ap.parse_args()

    st = settings_mod.load(a.config)
    cfg = st.sampling
    paths = ds.resolve(a.dataset or st.labels.dataset)
    geom = json.loads(paths.geom.read_text(encoding="utf-8"))

    from trailwalk import providers
    from trailwalk.config import kakao_appkey

    provider = providers.make("kakao", settings=st, appkey=kakao_appkey())
    rows, t0 = [], time.time()
    try:
        for n, c in enumerate(geom["courses"], 1):
            cid = c["course_id"]
            pts = probe_points(c, cfg, a.points) if head_polyline(c) else []
            polys = [[tuple(p) for p in s["polyline"]]
                     for s in c["segments"] if s["status"] == "ok"]
            hit = on_route = 0
            for lat, lng in pts:
                try:
                    pano = provider.nearest(lat, lng, radius_m=cfg.snap_radius_m)
                except Exception:
                    continue          # 프로브는 추정치다. 한 점 실패로 코스를 죽이지 않는다
                if pano is None:
                    continue
                hit += 1
                d = min(point_to_polyline_m((pano.lat, pano.lng), p) for p in polys)
                if d <= cfg.snap_radius_m * 1.5:
                    on_route += 1
            row = {"course_id": cid, "name": c["name"],
                   "theme": c.get("theme"), "gu": c.get("gu"),
                   "suspect": bool(c.get("suspect")), "ratio": c.get("ratio"),
                   "n_probe": len(pts), "pano_hit": hit, "on_route": on_route,
                   "pano_hit_ratio": round(hit / len(pts), 2) if pts else 0.0,
                   "on_route_ratio": round(on_route / len(pts), 2) if pts else 0.0}
            rows.append(row)
            print(f"\r{n}/{len(geom['courses'])} {cid} "
                  f"hit {row['pano_hit_ratio']:.2f} / on_route "
                  f"{row['on_route_ratio']:.2f}  경과 {time.time() - t0:.0f}s",
                  end="", flush=True)
    finally:
        provider.close()
    print()

    paths.coverage.write_text(json.dumps({
        "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "dataset": paths.name, "points_per_course": a.points,
        "snap_radius_m": cfg.snap_radius_m, "head_m": cfg.head_m,
        "courses": rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # 테마별 요약 — docs/22-labels.md §9 퍼널 표에 그대로 옮긴다
    by_theme: dict[str, list[int]] = {}
    for r in rows:
        t = by_theme.setdefault(r["theme"] or "?", [0, 0, 0])
        t[0] += 1
        t[1] += 1 if r["on_route_ratio"] >= 0.5 else 0
        t[2] += 1 if r["suspect"] else 0
    print(f"\n{'테마':<18} {'코스':>4} {'커버리지통과':>10} {'suspect':>8}")
    for theme, (n, ok, susp) in sorted(by_theme.items(), key=lambda x: -x[1][0]):
        print(f"{theme:<18} {n:>4} {ok:>10} {susp:>8}")
    # 두 게이트의 상관 — 같은 코스를 무는가 (22-labels.md §9 의 예상)
    both = sum(1 for r in rows if r["suspect"] and r["on_route_ratio"] < 0.5)
    n_susp = sum(1 for r in rows if r["suspect"])
    if n_susp:
        print(f"\nsuspect 코스 {n_susp}개 중 커버리지도 낮은 것 {both}개 "
              f"({both / n_susp:.0%}) — 두 게이트가 같은 코스를 무는 정도")
    print(f"wrote {paths.coverage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
