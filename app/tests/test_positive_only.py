"""양성 전용 설계의 불변식 — 오프라인.

2026-08-19 에 자동 음성 3종(같은 pano ±90°/180°, 코스 밖 격자)을 폐기했다
(→ docs/22-labels.md §5). 그 결정이 코드에서 유지되는지, 그리고 그때 같이
들어온 규칙들(시작 head_m 컷 · 화살표 방위 · 데이터셋 경로)을 고정한다.
"""
import json
from pathlib import Path

import pytest
from labels import adapt_gil_seoul as adapt
from labels import dataset
from labels import make_samples as ms

from trailwalk.geo import bearing_deg, polyline_length_m

APP = Path(__file__).resolve().parent.parent


class Cfg:
    interval_m = 50.0
    head_m = 200.0
    snap_radius_m = 30.0
    max_panos_per_course = 0
    provider_restart_every = 0


def _course(n_seg=3, seg_len=0.002, statuses=None):
    """남북으로 이어지는 세그먼트 n개. 세그먼트당 ~222m."""
    segs, lat = [], 37.5700
    for i in range(n_seg):
        st = (statuses or ["ok"] * n_seg)[i]
        seg = {"from": f"w{i}", "to": f"w{i + 1}", "status": st}
        if st == "ok":
            seg["polyline"] = [[lat, 127.0], [lat + seg_len, 127.0]]
            seg["length_m"] = 222
        lat += seg_len
        segs.append(seg)
    return {"course_id": "t-01", "name": "테스트", "segments": segs,
            "total_m": 222 * n_seg, "ratio": 1.0}


# ── 음성이 없다 ──────────────────────────────────────────────────────────

def test_negative_generation_symbols_are_gone():
    # 폐기한 코드가 되살아나면 여기서 걸린다
    assert not hasattr(ms, "offroute_candidates")


def test_sampling_settings_have_no_negative_keys():
    from trailwalk.settings import SamplingSettings
    names = {f for f in SamplingSettings.__dataclass_fields__}
    assert names.isdisjoint({"neg_ratio", "buffer_m", "offroute_max_m",
                             "offroute_snap_radius_m", "grid_m"})
    assert "head_m" in names


# ── 시작 head_m 컷 ───────────────────────────────────────────────────────

def test_head_cut_limits_distance():
    cands = ms.course_candidates(_course(), Cfg)
    pts = [(la, ln) for la, ln, _ in cands]
    # head_m 200m, 간격 50m → 0/50/100/150/200 = 5점
    assert len(cands) == 5
    assert polyline_length_m(pts) == pytest.approx(200, abs=1)


def test_head_polyline_stops_at_first_failure():
    c = _course(3, statuses=["ok", "missing", "ok"])
    pts = ms.head_polyline(c)
    # 첫 구간만 쓴다 — 건너뛰고 이으면 없는 경로를 지어내는 것이다
    assert len(pts) == 2
    assert polyline_length_m(pts) == pytest.approx(222, abs=2)


def test_first_segment_missing_yields_nothing():
    c = _course(2, statuses=["missing", "ok"])
    assert ms.head_polyline(c) == []
    assert ms.course_candidates(c, Cfg) == []
    courses, skipped = ms.load_courses({"courses": [c]}, False, None)
    assert courses == [] and "선두 구간 실패" in skipped[0]


def test_head_polyline_drops_duplicate_boundary_point():
    c = _course(2)
    pts = ms.head_polyline(c)
    assert len(pts) == len(set(pts))          # 구간 경계 중복점 제거


def test_max_panos_per_course_caps():
    class C(Cfg):
        max_panos_per_course = 2
    assert len(ms.course_candidates(_course(), C)) == 2


def test_suspect_course_excluded_by_default():
    c = {**_course(), "suspect": True}
    courses, skipped = ms.load_courses({"courses": [c]}, False, None)
    assert courses == [] and "ratio" in skipped[0]
    courses, _ = ms.load_courses({"courses": [c]}, True, None)
    assert len(courses) == 1                  # --include-suspect


# ── 화살표 방위 ─────────────────────────────────────────────────────────

def test_pick_arrow_heading_selects_nearest():
    arrow, diff = ms.pick_arrow_heading(310.0, [70.0, 250.0])
    assert arrow == 250.0 and diff == pytest.approx(60.0)
    arrow, diff = ms.pick_arrow_heading(5.0, [350.0, 180.0])
    assert arrow == 350.0 and diff == pytest.approx(15.0)


class FakePano:
    def __init__(self, pid, lat, lng):
        self.pano_id, self.lat, self.lng = pid, lat, lng


class FakeNeighbor:
    def __init__(self, h):
        self.heading = h


class FakeProvider:
    """폴리라인 위에 pano 를 주는 가짜. 이웃 방위는 생성자에서 정한다."""

    def __init__(self, arrows=(70.0, 250.0), pano_at=None):
        self.arrows = arrows
        self.captures = []
        self._pano_at = pano_at
        self._n = 0

    def nearest(self, lat, lng, radius_m):
        self._n += 1
        if self._pano_at is not None:
            return self._pano_at(lat, lng, self._n)
        return FakePano(f"p{self._n}", lat, lng)

    def neighbors(self, pano):
        return [FakeNeighbor(h) for h in self.arrows]

    def capture(self, pano, heading):
        self.captures.append((pano.pano_id, round(heading, 1)))
        return b"\x89PNG-fake"

    def close(self):
        pass


def _collect(tmp_path, provider, course=None, cfg=Cfg):
    dp = dataset.DatasetPaths(
        name="t", root=tmp_path, courses=tmp_path / "c.json",
        waypoints=tmp_path / "w.json", overrides=tmp_path / "o.json",
        routes_dir=tmp_path / "routes", geom=tmp_path / "g.json",
        coverage=tmp_path / "cov.json", samples=tmp_path / "s.jsonl",
        images=tmp_path / "images", labels=tmp_path / "l.jsonl",
        report=tmp_path / "r.tsv", svg=tmp_path / "svg")
    c = course or _course()
    (dp.images / c["course_id"] / "pos").mkdir(parents=True, exist_ok=True)
    col = ms.Collector(lambda: provider, dp, cfg, lambda m: None)
    rows = list(col.course(c, set(), 0))
    return rows, dp


def test_all_rows_are_positive_route(tmp_path):
    rows, _ = _collect(tmp_path, FakeProvider())
    assert rows, "샘플이 나와야 한다"
    assert all(r["label"] is True for r in rows)
    assert {r["label_source"] for r in rows} == {"route"}
    assert all(r["image"].endswith("_T.png") and "/pos/" in r["image"] for r in rows)


def test_saved_heading_is_arrow_not_course_bearing(tmp_path):
    # 코스는 정북(0°) 진행, 화살표는 70/250 → 70 이 채택돼야 한다
    prov = FakeProvider(arrows=(70.0, 250.0))
    rows, _ = _collect(tmp_path, prov)
    assert rows[0]["heading"] == 70.0
    assert rows[0]["course_bearing"] == pytest.approx(0.0, abs=0.1)
    assert rows[0]["arrow_diff_deg"] == pytest.approx(70.0, abs=0.1)
    assert prov.captures[0][1] == 70.0        # 실제로 그 방위로 찍었다


def test_pano_without_arrows_is_dropped(tmp_path):
    prov = FakeProvider(arrows=())
    rows, _ = _collect(tmp_path, prov)
    assert rows == [] and prov.captures == []   # 방위를 지어내지 않는다


def test_pano_far_from_polyline_is_dropped(tmp_path):
    # 항상 폴리라인에서 200m 동쪽인 pano 를 주는 provider
    prov = FakeProvider(pano_at=lambda lat, lng, n: FakePano(f"p{n}", lat, lng + 0.0023))
    rows, _ = _collect(tmp_path, prov)
    assert rows == []


def test_duplicate_pano_is_skipped(tmp_path):
    prov = FakeProvider(pano_at=lambda lat, lng, n: FakePano("same", lat, lng))
    rows, _ = _collect(tmp_path, prov)
    assert len(rows) == 1                      # 전역 dedupe


# ── 재개 ────────────────────────────────────────────────────────────────

def test_resume_state_restores_panos_and_seq(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "run_start"}),
        json.dumps({"type": "sample", "sample_id": "c-000p", "course_id": "c",
                    "pano_id": "a"}),
        json.dumps({"type": "sample", "sample_id": "c-003p", "course_id": "c",
                    "pano_id": "b"}),
        json.dumps({"type": "event", "kind": "error"}),
    ]) + "\n")
    panos, seq = ms.resume_state(p)
    assert panos == {"a", "b"} and seq == {"c": 4}


def test_resume_state_missing_file(tmp_path):
    assert ms.resume_state(tmp_path / "none.jsonl") == (set(), {})


# ── 데이터셋 경로 ────────────────────────────────────────────────────────

def test_dataset_paths_under_named_dir():
    p = dataset.resolve("seoul")
    assert p.name == "seoul"
    for path in (p.courses, p.waypoints, p.geom, p.samples, p.images, p.labels):
        assert "labels/seoul" in str(path)


def test_dataset_name_rejects_traversal():
    for bad in ("../etc", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            dataset.resolve(bad)


# ── 어댑터 ──────────────────────────────────────────────────────────────

def test_adapt_uses_stable_trail_sn_and_keeps_theme():
    courses, stats = adapt.adapt([
        {"trail_sn": 184, "name": "홍릉 두물길", "theme": "한강·하천이 좋은 길",
         "theme_code": "SE004", "gu": "동대문구", "distance_km": 6.2,
         "waypoints": ["신설동역", "안암2교(성북천)"]}])
    assert courses[0]["course_id"] == "seoul-184"     # 사이트가 주는 안정 id
    assert courses[0]["theme"] == "한강·하천이 좋은 길"
    assert courses[0]["gu"] == "동대문구"
    assert courses[0]["status"] == "ok" and stats["ok"] == 1


def test_adapt_keeps_courses_without_waypoints():
    courses, stats = adapt.adapt([
        {"trail_sn": 1, "name": "a", "theme": "숲", "waypoints": []},
        {"trail_sn": 2, "name": "b", "theme": "숲", "waypoints": ["한곳"]}])
    # 조용히 드롭하면 파서 회귀가 개수 감소로만 보인다 — 표시하고 남긴다
    assert len(courses) == 2
    assert {c["status"] for c in courses} == {"no_waypoints"}
    assert stats["no_waypoints"] == 2


def test_course_bearing_matches_segment_direction(tmp_path):
    rows, _ = _collect(tmp_path, FakeProvider(arrows=(0.0,)))
    north = bearing_deg((37.57, 127.0), (37.572, 127.0))
    assert rows[0]["course_bearing"] == pytest.approx(north, abs=0.1)


def test_dataset_arg_has_no_baked_default():
    """--dataset 기본값을 argparse 에 채우면 --config 오버레이가 무시된다.

    채워두면 a.dataset 이 절대 None 이 아니라 `a.dataset or st.labels.dataset`
    의 뒷항이 죽고, sampling 값은 오버레이를 쓰면서 경로만 정본을 쓰는
    상태가 된다 — 다른 데이터셋에 쓰거나 읽는다 (리뷰 지적).
    """
    import argparse
    ap = argparse.ArgumentParser()
    dataset.add_argument(ap)
    assert ap.parse_args([]).dataset is None
    assert ap.parse_args(["--dataset", "x"]).dataset == "x"


def test_merge_split_parens():
    # 상세페이지가 "홍릉터(홍릉숲·산림과학원)" 을 두 li 로 쪼갠다
    assert adapt.merge_split_parens(["홍릉터(홍릉숲", "산림과학원)", "영휘원"]) == \
        ["홍릉터(홍릉숲·산림과학원)", "영휘원"]
    # 정상 입력은 건드리지 않는다
    assert adapt.merge_split_parens(["안암2교(성북천)", "청계천"]) == \
        ["안암2교(성북천)", "청계천"]
