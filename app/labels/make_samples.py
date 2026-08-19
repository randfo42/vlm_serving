#!/usr/bin/env python
"""폴리라인 → 라벨 샘플 + 로드뷰 이미지. courses_geom.json → samples.jsonl + images/

    python app/labels/make_samples.py --dry-run      # 후보 좌표만 (네트워크 없음)
    python app/labels/make_samples.py                 # 캡처까지 (Playwright + 앱키)
    python app/labels/make_samples.py --course jongno-01 --course jongno-08

라벨 파이프라인 3단계 (← fetch_walk_routes.py, → 사람 검수 → apply_review.py).

### 라벨 설계 (자동 라벨 = 검수 전 가설이다)

- p / true  — 폴리라인 50m 리샘플 → pano 스냅, heading = 진행 방위
- o / false — positive pano 에서 ±90° 화각. 22-labels.md §5 —
  좌표가 같아 가장 값싸고 가장 어려운 음성
- r / false — positive pano 에서 180° 화각. **검수에서 가장 많이 뒤집힐
  부류다** — 산책로는 뒤로도 이어진다. 태그로 추적한다
- x / false — 코스 버퍼(150m) 밖 ~1km 내 도로 pano, heading 은 이웃
  그래프의 방위. 주변 시가지 하드 네거티브

suspect 코스(라우팅이 공식 거리의 1.6배 초과 — 도보 라우터가 산책로 대신
도로로 우회)는 **기본 제외**다. 그 폴리라인 위 점은 산책로가 아니라 인도라서
true 라벨이 오염된다. --include-suspect 로 강제할 수 있다 (검수 부담 증가).

offroute 의 heading 은 이웃 그래프(spot)의 방위를 쓴다 — 좌표도 방위도
지어내지 않는다 (geo.py 원칙). 이웃이 없는 pano 는 버린다.
"""
import argparse
import hashlib
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trailwalk import settings as settings_mod
from trailwalk.geo import norm_deg, point_to_polyline_m, resample_polyline

HERE = Path(__file__).resolve().parent / "jongno"
GEOM = HERE / "courses_geom.json"
SAMPLES = HERE / "samples.jsonl"
IMAGES = HERE / "images"


def load_polylines(geom: dict, include_suspect: bool, only: set[str] | None,
                   ) -> tuple[dict[str, list[list[tuple[float, float]]]], list[str]]:
    """코스별 ok 세그먼트 폴리라인. 반환: ({cid: [polyline,...]}, 제외 로그)."""
    out: dict[str, list[list[tuple[float, float]]]] = {}
    skipped: list[str] = []
    for c in geom["courses"]:
        cid = c["course_id"]
        if only and cid not in only:
            continue
        if c.get("suspect") and not include_suspect:
            skipped.append(f"{cid} {c['name']} (ratio {c['ratio']}x — 라우팅 우회 의심)")
            continue
        polys = [[tuple(p) for p in s["polyline"]]
                 for s in c["segments"] if s["status"] == "ok"]
        if polys:
            out[cid] = polys
    return out, skipped


def positive_candidates(polys: list[list[tuple[float, float]]],
                        interval_m: float) -> list[tuple[float, float, float]]:
    cands = []
    for poly in polys:
        cands += resample_polyline(poly, interval_m)
    return cands


def offroute_candidates(all_polys: list[list[tuple[float, float]]],
                        cfg) -> list[tuple[float, float]]:
    """코스 bbox 를 격자로 훑어 버퍼 밖·상한 안의 좌표를 모은다 (pano 스냅 전)."""
    lats = [p[0] for poly in all_polys for p in poly]
    lngs = [p[1] for poly in all_polys for p in poly]
    # 격자 간격을 도 단위로 (위도 1도 ≈ 111.32km, 경도는 cos 보정)
    dlat = cfg.grid_m / 111_320.0
    dlng = dlat / 0.793                    # cos(37.57°) ≈ 0.793 — 종로 위도 고정
    margin = cfg.offroute_max_m / 111_320.0
    out = []
    lat = min(lats) - margin
    while lat <= max(lats) + margin:
        lng = min(lngs) - margin / 0.793
        while lng <= max(lngs) + margin / 0.793:
            d = min(point_to_polyline_m((lat, lng), poly) for poly in all_polys)
            if cfg.buffer_m < d <= cfg.offroute_max_m:
                out.append((round(lat, 7), round(lng, 7)))
            lng += dlng
        lat += dlat
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=None, help="trailwalk.yaml 오버레이")
    ap.add_argument("--course", action="append", default=None,
                    help="이 코스만 (반복 가능). 기본: 전부")
    ap.add_argument("--include-suspect", action="store_true",
                    help="라우팅 우회 의심 코스도 포함 (라벨 오염 위험 — 검수 필수)")
    ap.add_argument("--dry-run", action="store_true",
                    help="캡처 없이 후보 좌표만 samples_dry.jsonl 로")
    a = ap.parse_args()

    st = settings_mod.load(a.config)
    cfg = st.sampling
    geom = json.loads(GEOM.read_text(encoding="utf-8"))
    polys_by_course, skipped = load_polylines(
        geom, a.include_suspect, set(a.course) if a.course else None)
    for msg in skipped:
        print(f"제외: {msg}")
    if not polys_by_course:
        print("남은 코스가 없다", file=sys.stderr)
        return 1

    # 버퍼 판정은 suspect 포함 **전체** 코스 폴리라인 기준이다 — suspect 코스
    # 주변이 offroute negative 로 뽑히면 그 점이 실제로는 코스 위일 수 있다.
    all_polys = [[tuple(p) for p in s["polyline"]]
                 for c in geom["courses"] for s in c["segments"]
                 if s["status"] == "ok"]

    pos_cands = {cid: positive_candidates(polys, cfg.interval_m)
                 for cid, polys in polys_by_course.items()}
    n_pos = sum(len(v) for v in pos_cands.values())
    off_cands = offroute_candidates(all_polys, cfg)
    random.Random(42).shuffle(off_cands)   # 결정적 셔플 — 재실행이 같은 순서
    print(f"positive 후보 {n_pos} (코스 {len(pos_cands)}개) · "
          f"offroute 격자 후보 {len(off_cands)}")

    if a.dry_run:
        out = HERE / "samples_dry.jsonl"
        with out.open("w", encoding="utf-8") as f:
            for cid, cands in pos_cands.items():
                for i, (lat, lng, h) in enumerate(cands):
                    f.write(json.dumps({
                        "type": "sample", "sample_id": f"{cid}-{i:03d}p",
                        "course_id": cid, "label": True, "label_source": "route",
                        "lat": lat, "lng": lng, "heading": round(h, 1),
                    }, ensure_ascii=False) + "\n")
            for i, (lat, lng) in enumerate(off_cands):
                f.write(json.dumps({
                    "type": "sample", "sample_id": f"grid-{i:03d}x",
                    "course_id": "offroute", "label": False,
                    "label_source": "offroute", "lat": lat, "lng": lng,
                }, ensure_ascii=False) + "\n")
        print(f"wrote {out} — plot_course.py --samples 로 겹쳐 볼 것")
        return 0

    # ── 캡처 ───────────────────────────────────────────────────────────
    from trailwalk import providers
    from trailwalk.config import kakao_appkey

    provider = providers.make("kakao", settings=st, appkey=kakao_appkey())
    t0 = time.time()
    seen_panos: dict[str, str] = {}        # pano_id → sample_id (전역 dedupe)
    rows: list[dict] = []
    stats = {"no_pano": 0, "off_polyline": 0, "dup_pano": 0, "no_neighbor": 0}
    errors: list[str] = []

    def log_err(msg: str) -> None:
        errors.append(msg)
        print(f"\n  ! {msg}", file=sys.stderr)

    def save(sample_id: str, cid: str, label: bool, source: str, pano, heading: float,
             dist_m: float | None) -> None:
        sub = "pos" if label else "neg"
        heading = norm_deg(heading)     # 파일명·대장 둘 다 [0,360). -90 이 그대로
                                        # 파일명에 들어가 조인 규칙을 깬 적이 있다
        d = IMAGES / cid / sub
        d.mkdir(parents=True, exist_ok=True)
        png = provider.capture(pano, heading)
        name = f"{sample_id}_{pano.pano_id}_{heading:05.1f}_{'T' if label else 'F'}.png"
        (d / name).write_bytes(png)
        rows.append({
            "type": "sample", "sample_id": sample_id, "course_id": cid,
            "label": label, "label_source": source,
            "pano_id": pano.pano_id, "lat": pano.lat, "lng": pano.lng,
            "heading": round(norm_deg(heading), 1),
            **({"dist_to_route_m": round(dist_m, 1)} if dist_m is not None else {}),
            "image": f"{cid}/{sub}/{name}",
        })

    try:
        for cid, cands in pos_cands.items():
            course_polys = polys_by_course[cid]
            seq = 0
            for lat, lng, heading in cands:
                try:
                    pano = provider.nearest(lat, lng, radius_m=cfg.snap_radius_m)
                except Exception as e:
                    # 오스냅 검출(ProviderError) 등 — 이 후보만 버린다.
                    # 한 점 때문에 수백 장 캡처 런이 죽으면 안 된다.
                    stats["no_pano"] += 1
                    log_err(f"{cid} nearest({lat:.5f},{lng:.5f}): {e}")
                    continue
                if pano is None:
                    stats["no_pano"] += 1
                    continue
                if pano.pano_id in seen_panos:
                    stats["dup_pano"] += 1
                    continue
                d = min(point_to_polyline_m((pano.lat, pano.lng), poly)
                        for poly in course_polys)
                if d > cfg.snap_radius_m * 1.5:
                    # 스냅이 폴리라인에서 이만큼 벗어났다 = 평행한 옆길에 붙었다
                    stats["off_polyline"] += 1
                    continue
                seen_panos[pano.pano_id] = cid
                base = f"{cid}-{seq:03d}"
                try:
                    save(f"{base}p", cid, True, "route", pano, heading, d)
                    # 같은 pano 이각 negative: 절반 직교(o, 좌우 교대), 1/4 역방향(r)
                    if seq % 2 == 0:
                        save(f"{base}o", cid, False, "orth", pano,
                             heading + (90.0 if seq % 4 == 0 else -90.0), d)
                    if seq % 4 == 1:
                        save(f"{base}r", cid, False, "rev", pano, heading + 180.0, d)
                except Exception as e:
                    stats["capture_fail"] = stats.get("capture_fail", 0) + 1
                    log_err(f"{cid} capture {pano.pano_id}: {e}")
                seq += 1
                print(f"\r{cid} {seq}/{len(cands)}  경과 {time.time() - t0:.0f}s",
                      end="", flush=True)
            print()

        n_pos_done = sum(1 for r in rows if r["label"])
        n_neg_done = len(rows) - n_pos_done
        need_off = max(0, round(cfg.neg_ratio * n_pos_done) - n_neg_done)
        print(f"positive {n_pos_done} · pano 이각 negative {n_neg_done} · "
              f"offroute 필요 {need_off}")
        got = 0
        for lat, lng in off_cands:
            if got >= need_off:
                break
            try:
                pano = provider.nearest(lat, lng, radius_m=60.0)
            except Exception:
                continue
            if pano is None or pano.pano_id in seen_panos:
                continue
            d = min(point_to_polyline_m((pano.lat, pano.lng), poly)
                    for poly in all_polys)
            if not (cfg.buffer_m < d <= cfg.offroute_max_m):
                continue                    # 스냅으로 버퍼 안에 끌려들어왔다
            nbrs = provider.neighbors(pano)
            if not nbrs:
                stats["no_neighbor"] += 1
                continue                    # 방위를 지어내지 않는다 — 버린다
            seen_panos[pano.pano_id] = "offroute"
            save(f"off-{got:03d}x", "offroute", False, "offroute", pano,
                 nbrs[0].heading, d)
            got += 1
            print(f"\roffroute {got}/{need_off}  경과 {time.time() - t0:.0f}s",
                  end="", flush=True)
        print()
    finally:
        provider.close()

    hdr = {"type": "run_start",
           "ts": datetime.now(UTC).isoformat(timespec="seconds"),
           "geom_sha256": hashlib.sha256(GEOM.read_bytes()).hexdigest(),
           "sampling": {k: getattr(cfg, k) for k in
                        ("interval_m", "buffer_m", "offroute_max_m",
                         "snap_radius_m", "neg_ratio", "grid_m")},
           "include_suspect": a.include_suspect, "skipped_courses": skipped}
    with SAMPLES.open("w", encoding="utf-8") as f:
        f.write(json.dumps(hdr, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for msg in errors:
            f.write(json.dumps({"type": "event", "kind": "error", "msg": msg},
                               ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "run_end", "stats": stats,
                            "wall_s": round(time.time() - t0, 1)},
                           ensure_ascii=False) + "\n")
    n_pos_done = sum(1 for r in rows if r["label"])
    print(f"wrote {SAMPLES} — 샘플 {len(rows)} (T {n_pos_done} / "
          f"F {len(rows) - n_pos_done}) · 스킵 {stats}")
    print(f"이미지: {IMAGES}/<코스>/pos|neg/ — 검수는 파일 이동으로 "
          f"(pos↔neg 이동=라벨 뒤집기, discard/=폐기)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
