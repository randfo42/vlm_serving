#!/usr/bin/env python
"""서울 테마산책길 대장 → 코스 대장. trails.json(--detail) → seoul/courses.json

    python app/labels/fetch_gil_seoul.py --detail     # 먼저 (경유지가 붙는다)
    python app/labels/adapt_gil_seoul.py

라벨 파이프라인 0단계의 **두 번째 생산자**다. 첫 번째는 `fetch_jongno.py`
(종로구 걷기코스 9개). 파이프라인의 실제 계약은 `courses.json` 스키마이고
이후 단계(geocode → routes → samples)는 소스를 모른다 — 그래서 새 출처가
생기면 스크립트를 분기시키지 않고 **어댑터를 하나 더 만든다**.

### course_id 는 사이트가 주는 안정 id 를 쓴다

`seoul-{trail_sn:03d}`. 종로는 내비 순서 기반(`jongno-{i:02d}`)이라 페이지
구성이 바뀌면 id 가 통째로 밀리는 약점이 있다. 여기서는 반복하지 않는다 —
`trail_sn` 은 상세 URL 의 키라서 사이트가 유지한다.

### theme / gu 를 실어 보낸다

평가 리포트의 슬라이스 축이 여기서 나온다. "어떤 산책로에서 되는가"(테마별)와
"지역 편향이 있는가"(자치구별)가 이 프로젝트의 실제 질문이고, 그 답은
courses.json 에 테마·자치구가 실려야 나온다.
"""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds

from trailwalk import settings as settings_mod

TRAILS = Path(__file__).resolve().parent / "trails.json"


def merge_split_parens(wps: list[str]) -> list[str]:
    """괄호가 안 맞는 연속 경유지를 합친다.

    상세페이지가 "홍릉터(홍릉숲·산림과학원)" 을 두 `<li>` 로 쪼개 놓는 경우가
    있다 → ["홍릉터(홍릉숲", "산림과학원)"]. 그대로 두면 둘 다 검색이 안 되고
    (괄호가 질의를 망친다) 경유지 하나가 통째로 사라진다.
    """
    out: list[str] = []
    for w in wps:
        if out and out[-1].count("(") > out[-1].count(")"):
            out[-1] = f"{out[-1]}·{w}"
        else:
            out.append(w)
    return out


def adapt(trails: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """trails.json 항목들 → courses.json 코스들. 반환: (코스, 집계)."""
    out, stats = [], {"total": 0, "no_waypoints": 0, "ok": 0}
    for t in trails:
        stats["total"] += 1
        wps = merge_split_parens(
            [w.strip() for w in (t.get("waypoints") or []) if w and w.strip()])
        # 경유지가 없거나 1개면 구간을 만들 수 없다. **드롭하지 않고** 표시한다 —
        # 조용히 빠지면 파서 회귀가 개수 감소로만 보인다.
        status = "ok" if len(wps) >= 2 else "no_waypoints"
        stats[status] += 1
        out.append({
            "course_id": f"seoul-{t['trail_sn']:03d}",
            "name": t.get("name"),
            "kind": t.get("theme"),          # 종로 스키마의 kind 자리
            "theme": t.get("theme"),
            "theme_code": t.get("theme_code"),
            "gu": t.get("gu"),
            "difficulty": t.get("difficulty"),
            "url": t.get("detail_url"),
            "smgis_url": t.get("smgis_url"),
            "route_text": " → ".join(wps) or None,
            "waypoints": wps,
            "distance_km": t.get("distance_km"),
            "duration": t.get("duration"),
            "course_type": None,
            "status": status,
        })
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trails", type=Path, default=TRAILS)
    ds.add_argument(ap)
    a = ap.parse_args()
    st = settings_mod.load()
    paths = ds.resolve(a.dataset or st.labels.dataset)

    raw = json.loads(a.trails.read_text(encoding="utf-8"))
    courses, stats = adapt(raw["trails"])
    if stats["no_waypoints"] == stats["total"]:
        print(f"✗ 경유지가 하나도 없다 — `fetch_gil_seoul.py --detail` 을 먼저 "
              f"돌릴 것 (지금 {a.trails} 는 목록만 있다)", file=sys.stderr)
        return 2

    paths.root.mkdir(parents=True, exist_ok=True)
    paths.courses.write_text(json.dumps({
        "source": raw.get("source"),
        "adapted_from": str(a.trails.name),
        "fetched_at": raw.get("fetched_at"),
        "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "count": len(courses), "courses": courses,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    by_theme: dict[str, list[int]] = {}
    for c in courses:
        row = by_theme.setdefault(c["theme"] or "?", [0, 0])
        row[0] += 1
        row[1] += 1 if c["status"] == "ok" else 0
    print(f"{'테마':<16} {'대장':>4} {'경유지있음':>8}")
    for theme, (n, ok) in sorted(by_theme.items(), key=lambda x: -x[1][0]):
        print(f"{theme:<16} {n:>4} {ok:>8}")
    n_wp = sum(len(c["waypoints"]) for c in courses)
    print(f"\nwrote {paths.courses}  (코스 {stats['ok']}/{stats['total']} · "
          f"경유지 {n_wp})")
    if stats["no_waypoints"]:
        print(f"경유지 없음 {stats['no_waypoints']}건 — status=no_waypoints 로 "
              f"남아 있다 (지오코딩에서 자연히 빠진다)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
