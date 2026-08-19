#!/usr/bin/env python
"""폴리라인 → 양성 라벨 샘플 + 로드뷰 이미지. courses_geom.json → samples.jsonl

    python app/labels/make_samples.py --dry-run            # 후보 좌표만 (네트워크 없음)
    python app/labels/make_samples.py                       # 캡처 (Playwright + 앱키)
    python app/labels/make_samples.py --dataset jongno --course jongno-01

라벨 파이프라인 3단계 (← fetch_walk_routes.py, → 사람 검수 → apply_review.py).

### 이 스크립트는 **양성만** 만든다

음성 자동 생성 3종(같은 pano ±90°/180°, 코스 밖 격자)은 2026-08-19 에 폐기됐다.
라벨 단위가 `(pano, heading)` 이 아니라 **pano** 이기 때문이다 — 산책로 위
pano 에서 옆을 봐도 카메라는 여전히 산책로 위다. 전체 논거는
`docs/22-labels.md` §5(폐기 기록). 음성은 두 곳에서만 온다:

  (a) 검수자가 pos/ → neg/ 로 옮긴 것 (사람이 사진을 보고 만든 유일한 라벨)
  (b) "아예 산책로가 아닌 곳" 별도 수집 — TODO, 이 스크립트의 일이 아니다

### 한 샘플이 만들어지는 방식

1. 코스의 **선두부터 연속된 `ok` 세그먼트**를 이어 붙인다. 중간에 실패 구간이
   나오면 거기서 끊는다 — 건너뛰고 이으면 없는 경로를 지어내는 것이다.
2. `interval_m` 간격으로 리샘플하고 **`head_m` 까지만** 쓴다 (코스 앞머리).
3. 각 점을 `nearest()` 로 실측 pano 에 스냅한다. 스냅이 폴리라인에서
   `snap_radius_m × 1.5` 를 넘으면 평행한 옆길에 붙은 것이라 버린다.
4. `neighbors()` 의 화살표 방위 중 **코스 진행 방위에 가장 가까운 것**을
   heading 으로 쓴다. 화살표가 실측이고 코스 방위는 선택자다 —
   진행 방위를 그대로 쓰면 경유지가 길 건너 POI 인 지점에서 카메라가 옆
   건물을 본다(실측 사고). 화살표가 없는 pano 는 버린다(방위를 지어내지 않는다).
   walk 루프가 `Neighbor.heading` 으로 겨냥하는 것과 같은 기준이다.

suspect 코스(라우팅이 공식 거리의 1.6배 초과 — 도보 라우터가 산책로 대신
도로로 우회)는 **기본 제외**다. `--include-suspect` 로 강제할 수 있다.

### 재개

캡처는 즉시 `samples.jsonl` 에 append 된다. 다시 실행하면 기존 파일의
pano_id 를 읽어 건너뛴다 — 수 시간짜리 런이 중간에 죽어도 이어서 돈다.
처음부터 다시 하려면 samples.jsonl 과 images/ 를 지운다.
"""
import argparse
import contextlib
import hashlib
import json
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds

from trailwalk import settings as settings_mod
from trailwalk.geo import angle_diff, norm_deg, point_to_polyline_m, resample_polyline


def head_polyline(course: dict) -> list[tuple[float, float]]:
    """선두부터 **연속된** ok 세그먼트를 이어 붙인다.

    첫 세그먼트가 실패면 빈 리스트다 — 코스의 시작이 어디인지 알 수 없으므로
    그 코스는 쓰지 않는다. 중간에 끊기면 거기까지만 쓴다.
    """
    pts: list[tuple[float, float]] = []
    for seg in course["segments"]:
        if seg["status"] != "ok":
            break
        poly = [tuple(p) for p in seg["polyline"]]
        if pts and poly and pts[-1] == poly[0]:
            poly = poly[1:]                 # 구간 경계의 중복점
        pts += poly
    return pts


def course_candidates(course: dict, cfg) -> list[tuple[float, float, float]]:
    """코스 하나의 후보점 [(lat, lng, 코스진행방위), ...]. head_m 까지만."""
    pts = head_polyline(course)
    if len(pts) < 2:
        return []
    cands = resample_polyline(pts, cfg.interval_m)
    # 리샘플 점은 0, interval, 2×interval, … 이므로 앞에서 자르면 head_m 컷이다
    n_head = int(cfg.head_m // cfg.interval_m) + 1
    cands = cands[:n_head]
    if cfg.max_panos_per_course > 0:
        cands = cands[:cfg.max_panos_per_course]
    return cands


def pick_arrow_heading(course_bearing: float,
                       arrow_headings: list[float]) -> tuple[float, float]:
    """코스 진행 방위에 가장 가까운 화살표 방위를 고른다. 반환: (화살표, 차이).

    화살표(pano 그래프)가 실측이고 코스 방위는 선택자다. 빈 목록은 호출자가
    미리 걸러야 한다 — 화살표가 없으면 방위를 지어내지 않고 pano 를 버린다.
    """
    best = min(arrow_headings, key=lambda h: angle_diff(h, course_bearing))
    return best, angle_diff(best, course_bearing)


def load_courses(geom: dict, include_suspect: bool, only: set[str] | None,
                 coverage: dict[str, float] | None = None,
                 min_coverage: float = 0.0) -> tuple[list[dict], list[str]]:
    """쓸 코스 목록과 제외 사유 로그.

    coverage 는 {course_id: on_route_ratio}. 로드뷰가 코스 위에 없는 코스를
    캡처(장당 3초) 전에 걸러낸다 — 찍어봐야 옆 차도만 나온다.
    """
    out, skipped = [], []
    for c in geom["courses"]:
        cid = c["course_id"]
        if only and cid not in only:
            continue
        if c.get("suspect") and not include_suspect:
            skipped.append(f"{cid} {c['name']} (ratio {c.get('ratio')}x — 라우팅 우회 의심)")
            continue
        if coverage is not None and cid in coverage and coverage[cid] < min_coverage:
            skipped.append(f"{cid} {c['name']} "
                           f"(로드뷰 커버리지 {coverage[cid]:.0%} < {min_coverage:.0%})")
            continue
        if not head_polyline(c):
            skipped.append(f"{cid} {c['name']} (선두 구간 실패 — 시작점을 알 수 없다)")
            continue
        out.append(c)
    return out, skipped


def resume_state(path: Path) -> tuple[set[str], dict[str, int]]:
    """기존 samples.jsonl 에서 (이미 찍은 pano_id, 코스별 다음 seq)."""
    panos: set[str] = set()
    seq: dict[str, int] = {}
    if not path.exists():
        return panos, seq
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type") != "sample":
            continue
        panos.add(d["pano_id"])
        cid = d["course_id"]
        seq[cid] = max(seq.get(cid, 0), int(d["sample_id"].rsplit("-", 1)[-1][:-1]) + 1)
    return panos, seq


class Collector:
    """캡처 루프. provider 를 주기적으로 재시작하고 결과를 즉시 흘려보낸다."""

    def __init__(self, make_provider, paths: ds.DatasetPaths, cfg, log_err):
        self._make = make_provider
        self.paths = paths
        self.cfg = cfg
        self.log_err = log_err
        self.provider = None
        self._courses_done = 0
        self.stats = {"no_pano": 0, "off_polyline": 0, "dup_pano": 0,
                      "no_neighbor": 0, "capture_fail": 0}

    def _ensure_provider(self) -> None:
        every = self.cfg.provider_restart_every
        if self.provider is not None and every > 0 and self._courses_done >= every:
            self.close()
            self._courses_done = 0
        if self.provider is None:
            self.provider = self._make()

    def close(self) -> None:
        if self.provider is not None:
            with contextlib.suppress(Exception):
                self.provider.close()
            self.provider = None

    def course(self, course: dict, seen: set[str], start_seq: int) -> Iterator[dict]:
        """코스 하나를 캡처하며 샘플 행을 하나씩 내놓는다."""
        cid = course["course_id"]
        self._ensure_provider()
        polys = [[tuple(p) for p in s["polyline"]]
                 for s in course["segments"] if s["status"] == "ok"]
        cands = course_candidates(course, self.cfg)
        seq = start_seq
        for i, (lat, lng, bearing) in enumerate(cands, 1):
            try:
                pano = self.provider.nearest(lat, lng, radius_m=self.cfg.snap_radius_m)
            except Exception as e:
                self.stats["no_pano"] += 1
                self.log_err(f"{cid} nearest({lat:.5f},{lng:.5f}): {e}")
                continue
            if pano is None:
                self.stats["no_pano"] += 1
                continue
            if pano.pano_id in seen:
                self.stats["dup_pano"] += 1
                continue
            d = min(point_to_polyline_m((pano.lat, pano.lng), poly) for poly in polys)
            if d > self.cfg.snap_radius_m * 1.5:
                self.stats["off_polyline"] += 1      # 평행한 옆길에 붙었다
                continue
            try:
                nbrs = self.provider.neighbors(pano)
            except Exception as e:
                self.stats["no_neighbor"] += 1
                self.log_err(f"{cid} neighbors {pano.pano_id}: {e}")
                continue
            if not nbrs:
                self.stats["no_neighbor"] += 1       # 방위를 지어내지 않는다
                continue
            arrow, diff = pick_arrow_heading(bearing, [n.heading for n in nbrs])
            sample_id = f"{cid}-{seq:03d}p"
            try:
                png = self.provider.capture(pano, arrow)
            except Exception as e:
                self.stats["capture_fail"] += 1
                self.log_err(f"{cid} capture {pano.pano_id}: {e}")
                continue
            name = f"{sample_id}_{pano.pano_id}_{norm_deg(arrow):05.1f}_T.png"
            (self.paths.images / cid / "pos" / name).write_bytes(png)
            seen.add(pano.pano_id)
            seq += 1
            print(f"\r{cid} {seq}장 ({i}/{len(cands)})", end="", flush=True)
            yield {
                "type": "sample", "sample_id": sample_id, "course_id": cid,
                # 자동 라벨은 검수 전 **가설**이다. 확정은 apply_review 의 final_label.
                "label": True, "label_source": "route",
                "pano_id": pano.pano_id, "lat": pano.lat, "lng": pano.lng,
                "heading": round(norm_deg(arrow), 1),
                "course_bearing": round(norm_deg(bearing), 1),
                "arrow_diff_deg": round(diff, 1),
                "n_arrows": len(nbrs),
                "dist_to_route_m": round(d, 1),
                "image": f"{cid}/pos/{name}",
            }
        self._courses_done += 1
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=None, help="trailwalk.yaml 오버레이")
    ap.add_argument("--course", action="append", default=None,
                    help="이 코스만 (반복 가능). 기본: 전부")
    ap.add_argument("--include-suspect", action="store_true",
                    help="라우팅 우회 의심 코스도 포함 (라벨 오염 위험 — 검수 필수)")
    ap.add_argument("--dry-run", action="store_true",
                    help="캡처 없이 후보 좌표만 samples_dry.jsonl 로")
    ds.add_argument(ap)
    a = ap.parse_args()
    st = settings_mod.load(a.config)
    cfg = st.sampling
    paths = ds.resolve(a.dataset or st.labels.dataset)
    if not paths.geom.exists():
        print(f"✗ {paths.geom} 이 없다 — fetch_walk_routes.py 를 먼저 돌릴 것",
              file=sys.stderr)
        return 2

    geom = json.loads(paths.geom.read_text(encoding="utf-8"))
    coverage = None
    if paths.coverage.exists():
        cov = json.loads(paths.coverage.read_text(encoding="utf-8"))
        coverage = {r["course_id"]: r["on_route_ratio"] for r in cov["courses"]}
    courses, skipped = load_courses(
        geom, a.include_suspect, set(a.course) if a.course else None,
        coverage, cfg.coverage_min_ratio)
    for msg in skipped:
        print(f"제외: {msg}")
    if not courses:
        print("남은 코스가 없다", file=sys.stderr)
        return 1

    n_cand = sum(len(course_candidates(c, cfg)) for c in courses)
    print(f"코스 {len(courses)} · 후보점 {n_cand} "
          f"(간격 {cfg.interval_m:.0f}m · 시작 {cfg.head_m:.0f}m 까지)")

    if a.dry_run:
        out = paths.root / "samples_dry.jsonl"
        with out.open("w", encoding="utf-8") as f:
            for c in courses:
                for i, (lat, lng, brg) in enumerate(course_candidates(c, cfg)):
                    f.write(json.dumps({
                        "type": "sample", "sample_id": f"{c['course_id']}-{i:03d}p",
                        "course_id": c["course_id"], "label": True,
                        "label_source": "route", "lat": lat, "lng": lng,
                        "heading": round(brg, 1),
                    }, ensure_ascii=False) + "\n")
        print(f"wrote {out} — plot_course.py --samples 로 겹쳐 볼 것")
        return 0

    # ── 캡처 ───────────────────────────────────────────────────────────
    from trailwalk import providers
    from trailwalk.config import kakao_appkey

    seen, seq_by_course = resume_state(paths.samples)
    if seen:
        print(f"재개: 이미 찍은 pano {len(seen)}개는 건너뛴다")

    errors: list[str] = []

    def log_err(msg: str) -> None:
        errors.append(msg)
        print(f"\n  ! {msg}", file=sys.stderr)

    appkey = kakao_appkey()
    collector = Collector(
        lambda: providers.make("kakao", settings=st, appkey=appkey),
        paths, cfg, log_err)

    t0 = time.time()
    n_rows = 0
    fresh = not paths.samples.exists()
    paths.samples.parent.mkdir(parents=True, exist_ok=True)
    with paths.samples.open("a", encoding="utf-8") as f:
        if fresh:
            f.write(json.dumps({
                "type": "run_start",
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "dataset": paths.name,
                "geom_sha256": hashlib.sha256(paths.geom.read_bytes()).hexdigest(),
                "sampling": {k: getattr(cfg, k) for k in
                             ("interval_m", "head_m", "snap_radius_m",
                              "max_panos_per_course", "coverage_min_ratio")},
                "positives_only": True,
                "include_suspect": a.include_suspect,
                "skipped_courses": skipped,
            }, ensure_ascii=False) + "\n")
            f.flush()
        try:
            for c in courses:
                cid = c["course_id"]
                for sub in ("pos", "discard"):
                    # 검수자의 목적지를 미리 만들어 둔다. neg/ 는 만들지 않는다 —
                    # 음성은 별도 수집이고, pos 에서 옮기지 않는다 (§5 폐기 기록).
                    (paths.images / cid / sub).mkdir(parents=True, exist_ok=True)
                for row in collector.course(c, seen, seq_by_course.get(cid, 0)):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()          # 중간에 죽어도 거기까지는 남는다
                    n_rows += 1
        finally:
            collector.close()
            for msg in errors:
                f.write(json.dumps({"type": "event", "kind": "error", "msg": msg},
                                   ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "run_end", "stats": collector.stats,
                                "added": n_rows,
                                "wall_s": round(time.time() - t0, 1)},
                               ensure_ascii=False) + "\n")

    print(f"\nwrote {paths.samples} — 이번 런에서 {n_rows}장 · 스킵 {collector.stats}")
    print(f"이미지: {paths.images}/<코스>/pos/ — 검수는 파일 이동으로 "
          f"(discard/ = 폐기)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
