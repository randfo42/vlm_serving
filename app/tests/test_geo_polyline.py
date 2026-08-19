"""폴리라인 함수 불변식 — 라벨 수집(app/labels/)의 기하 기반.

리샘플 점이 원 폴리라인을 벗어나면 true 라벨이 길 밖 pano 로 스냅되고,
점-선 거리가 틀리면 버퍼 필터(경로 150m 밖 = negative)가 오염된다.
둘 다 라벨 오염이라 조용히 틀리면 안 되는 부분이다.
"""
from itertools import pairwise

import pytest

from trailwalk.geo import (
    angle_diff,
    bearing_deg,
    haversine_m,
    point_to_polyline_m,
    polyline_length_m,
    resample_polyline,
)

# 청계천 근처의 ㄱ자 폴리라인. 동쪽 ~177m + 북쪽 ~111m
L = [(37.5700, 127.0000), (37.5700, 127.0020), (37.5710, 127.0020)]


def test_length_is_sum_of_segments():
    assert polyline_length_m(L) == pytest.approx(
        haversine_m(L[0], L[1]) + haversine_m(L[1], L[2]))


def test_resample_points_lie_on_polyline():
    for lat, lng, _ in resample_polyline(L, 50):
        assert point_to_polyline_m((lat, lng), L) < 0.1


def test_resample_spacing_and_endpoints():
    rs = resample_polyline(L, 50)
    # 양 끝점 보존
    assert (rs[0][0], rs[0][1]) == L[0]
    assert (rs[-1][0], rs[-1][1]) == L[-1]
    # 간격: 연속 점 사이가 interval 이하 (세그먼트 경계·끝점에서만 짧아진다)
    for (a_lat, a_lng, _), (b_lat, b_lng, _) in pairwise(rs):
        d = haversine_m((a_lat, a_lng), (b_lat, b_lng))
        assert d <= 50 + 0.01


def test_resample_heading_follows_segment():
    rs = resample_polyline(L, 50)
    east = bearing_deg(L[0], L[1])
    north = bearing_deg(L[1], L[2])
    # 첫 점은 동쪽 세그먼트, 끝점은 북쪽 세그먼트의 방위
    assert angle_diff(rs[0][2], east) < 0.1
    assert angle_diff(rs[-1][2], north) < 0.1
    # 방위는 두 세그먼트 값 중 하나여야 한다 (지어낸 중간값 금지)
    for _, _, h in rs:
        assert min(angle_diff(h, east), angle_diff(h, north)) < 0.1


def test_resample_crosses_segment_boundary_continuously():
    # 간격 100m: 동쪽 세그먼트(177m)에 1개, 경계를 넘겨 이어 재므로
    # 북쪽 세그먼트의 샘플은 경계에서 100-77=23m 지점이 아니라 누적 200m 지점
    rs = resample_polyline(L, 100)
    total = polyline_length_m(L)
    assert len(rs) == 2 + int(total // 100)  # 시작 + 중간들 + 끝


def test_resample_rejects_degenerate():
    with pytest.raises(ValueError):
        resample_polyline([L[0]], 50)
    with pytest.raises(ValueError):
        resample_polyline(L, 0)
    with pytest.raises(ValueError):
        resample_polyline([L[0], L[0]], 50)     # 전부 같은 점


def test_resample_skips_zero_segments():
    rs = resample_polyline([L[0], L[0], L[1], L[2]], 50)   # 중복점 섞임
    assert (rs[0][0], rs[0][1]) == L[0]
    assert (rs[-1][0], rs[-1][1]) == L[-1]


def test_point_on_line_is_zero():
    assert point_to_polyline_m(L[0], L) < 1e-9
    assert point_to_polyline_m(L[1], L) < 1e-9


def test_point_perpendicular_distance():
    # 동쪽 세그먼트에서 북쪽으로 ~100m. 경도는 서쪽에 둬서 북쪽 세그먼트
    # (lng 127.0020)까지의 거리(~132m)가 정답을 가리지 않게 한다
    assert point_to_polyline_m((37.5709, 127.0005), L) == pytest.approx(100, abs=1)


def test_point_takes_minimum_over_segments():
    # 두 세그먼트 모두에 수선이 서는 점 — 더 가까운 북쪽 세그먼트(~88m)가 답
    d = point_to_polyline_m((37.5709, 127.0010), L)
    assert d == pytest.approx(88, abs=1)


def test_point_beyond_endpoint_clamps_to_endpoint():
    # 수선의 발이 세그먼트 밖 — 끝점 거리로 클램프
    far_west = (37.5700, 126.9990)
    assert point_to_polyline_m(far_west, L) == pytest.approx(
        haversine_m(far_west, L[0]), rel=0.01)


def test_point_to_single_point_polyline_is_haversine():
    assert point_to_polyline_m(L[1], [L[0]]) == pytest.approx(haversine_m(L[1], L[0]))


def test_point_to_empty_polyline_raises():
    with pytest.raises(ValueError):
        point_to_polyline_m(L[0], [])
