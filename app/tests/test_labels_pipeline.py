"""라벨 파이프라인(app/labels/) 불변식 — 전부 오프라인.

라벨은 평가의 정답이다. 여기가 조용히 틀리면 그 위의 모든 정확도 수치가
허구가 된다. 실제로 겪은 사고를 고정한다:
  - "청계광장"이 9km 밖 동명 POI 에 정확 일치로 잡힘 (거리 상한이 없어서)
  - 말바위 계열 경유지 3개가 같은 POI 로 지오코딩 → SAME_POINT 실패
  - 청계천길 변형 표기 "이름 [A → B]"
"""
import importlib.util
import json
from itertools import pairwise
from pathlib import Path

import pytest

from trailwalk.geo import haversine_m

APP = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, APP / "labels" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch_jongno = _load("fetch_jongno")
geocode = _load("geocode_waypoints")
routes = _load("fetch_walk_routes")


# ── fetch_jongno: 경유지 파싱 ────────────────────────────────────────────

def test_waypoints_arrow_chain():
    assert fetch_jongno.parse_waypoints("택견수련터→수성동계곡→해맞이동산") == \
        ["택견수련터", "수성동계곡", "해맞이동산"]


def test_waypoints_bracket_form():
    # 청계천길의 실제 표기 — 대괄호 안이 경유지, 화살표에 공백
    assert fetch_jongno.parse_waypoints("청계천길 [황학교 → 청계광장]") == \
        ["황학교", "청계광장"]


def test_waypoints_empty():
    assert fetch_jongno.parse_waypoints("") == []


def test_parse_course_from_html_fragment():
    html = """
    <li><span>코스경로</span><ul class="sub_font"><li>A→B</li></ul></li>
    <li><span>거리</span><ul class="sub_font"><li>2.5km</li></ul></li>
    <li><span>소요시간</span><ul class="sub_font"><li>1시간</li></ul></li>
    <li><span>코스타입</span><ul class="sub_font"><li>비순환형</li></ul></li>
    """
    c = fetch_jongno.parse_course(html)
    assert c["waypoints"] == ["A", "B"]
    assert c["distance_km"] == 2.5
    assert c["duration"] == "1시간"


def test_parse_nav_dedupes_and_keeps_order():
    html = ('<a href="index_1.jsp">추천1코스 : 인왕산숲길</a>'
            '<a href="index_9.jsp">산책1코스 : 인왕산자락길</a>'
            '<a href="index_1.jsp">추천1코스 : 인왕산숲길</a>'
            '<a href="index_4.jsp">일반 안내</a>')          # 코스 아님 — 제외
    nav = fetch_jongno.parse_nav(html)
    assert [h for h, _ in nav] == ["index_1.jsp", "index_9.jsp"]


# ── geocode: 변형 질의와 매칭 점수 ───────────────────────────────────────

def test_variants_original_first():
    v = geocode.variants("가온다리(구름다리)")
    assert v[0] == "가온다리(구름다리)"
    assert "가온다리" in v and "구름다리" in v


def test_variants_strip_direction_tails():
    # "(말바위안내소방향)" — 괄호 안 + 꼬리말 제거로 검색 가능한 이름이 나온다
    assert "말바위안내소" in geocode.variants("(말바위안내소방향)")
    # "북악팔각정가는길" 류
    assert "북악팔각정" in geocode.variants("평창길 우측방향(북악팔각정가는길)")


def test_variants_subway_exit():
    assert "경복궁역" in geocode.variants("경복궁역1번출구")


def test_variants_do_not_return_short_junk():
    assert all(len(v) >= 2 for v in geocode.variants("이빨바위"))


def test_score_exact_beats_contains():
    exact = {"place_name": "청계광장"}
    contains = {"place_name": "오프뷰티 청계광장시장점"}
    assert geocode._score("청계광장", exact) > geocode._score("청계광장", contains)
    assert geocode._score("청계광장", {"place_name": "엉뚱한곳"}) == 0


def test_clean_of_paren_only_name_not_empty():
    # 이름 전체가 괄호인 경우에도 변형이 나온다
    assert geocode.variants("(말바위,말바위조망명소)")


# ── fetch_walk_routes: 좌표 변환과 응답 파싱 ─────────────────────────────

def test_wcongnamul_roundtrip_known_point():
    # 사직단 — REST transcoord 로 확인한 대응쌍 (2026-08-19)
    lat, lng = routes.wcongnamul_to_wgs84(492858, 1132253)
    assert haversine_m((lat, lng), (37.5756533, 126.9676599)) < 1.0


def test_parse_walkset_fixture():
    raw = json.loads((FIXTURES / "walkset_ok.json").read_text(encoding="utf-8"))
    pts, length_m, time_s = routes.parse_walkset(raw)
    assert len(pts) >= 2
    assert length_m > 0 and time_s > 0
    # 연속 중복점이 없어야 한다 (링크 경계에서 생긴다)
    assert all(a != b for a, b in pairwise(pts))
    # 폴리라인 재계산 길이가 응답 length 와 크게 어긋나지 않는다
    wgs = [routes.wcongnamul_to_wgs84(x, y) for x, y in pts]
    from trailwalk.geo import polyline_length_m
    assert polyline_length_m(wgs) == pytest.approx(length_m, rel=0.35)


def test_parse_walkset_failure_raises():
    bad = {"directions": [{"success": False, "resultCode": "SAME_POINT",
                           "sections": []}]}
    with pytest.raises(ValueError):
        routes.parse_walkset(bad)


def test_parse_walkset_shape_change_raises():
    # 형식이 바뀌면 조용히 빈 폴리라인이 아니라 예외여야 한다
    with pytest.raises((KeyError, ValueError, IndexError)):
        routes.parse_walkset({"totally": "different"})


def test_parse_walkset_empty_polyline_raises():
    ok_but_empty = {"directions": [{"success": True, "resultCode": "SUCCESS",
                                    "length": 0, "time": 0,
                                    "sections": [{"guideList": [
                                        {"guideMent": "도착", "x": 1, "y": 2}]}]}]}
    with pytest.raises(ValueError):
        routes.parse_walkset(ok_but_empty)


def test_effective_waypoints_merges_close_nodes():
    # --keys 와 main() 이 같은 경유지 열을 봐야 한다 — 갈라지면 --keys 가
    # 병합 구간에서 존재하지 않는 캐시 파일명을 알려준다 (리뷰 지적)
    course = {"course_id": "c", "waypoints": [
        {"name": "A", "status": "geocoded", "lat": 37.5700, "lng": 127.0000},
        {"name": "B", "status": "geocoded", "lat": 37.5701, "lng": 127.0000},  # ~11m
        {"name": "C", "status": "missing"},
        {"name": "D", "status": "geocoded", "lat": 37.5720, "lng": 127.0000},
    ]}
    wps = routes.effective_waypoints(course)
    assert [w["name"] for w in wps] == ["A", "D"]


def test_pick_arrow_heading_selects_nearest_arrow():
    # heading 의 기준은 화살표(실측)이고 코스 방위는 선택자다 — 진행 방위를
    # 그대로 쓰면 경유지가 길 건너 POI 인 지점에서 옆 건물을 본다 (실측 사고)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_samples", APP / "labels" / "make_samples.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    # 도로가 70/250 축, 코스 진행 310° → 250° 화살표가 선택돼야 한다
    arrow, diff = ms.pick_arrow_heading(310.0, [70.0, 250.0])
    assert arrow == 250.0 and diff == pytest.approx(60.0)
    # 0/360 랩
    arrow, diff = ms.pick_arrow_heading(5.0, [350.0, 180.0])
    assert arrow == 350.0 and diff == pytest.approx(15.0)


# ── pano_meta: 도보 판정과 검수 폴더 ──────────────────────────────────────

def test_is_walk_is_complement_of_car_tools():
    """카카오 isWalk 와 같다 — 차량 3종의 여집합. 화이트리스트가 아니다."""
    from labels.pano_meta import CAR_TOOLS, is_walk
    assert {"102", "200", "202"} == CAR_TOOLS
    for car in CAR_TOOLS:
        assert not is_walk(car)
    for walk in ("100", "101", "103", "201", "205"):
        assert is_walk(walk)


def test_is_walk_treats_unknown_codes_as_walk():
    """새 코드가 생겨도 카카오와 같은 쪽으로 틀린다 — 조용히 반대로 가지 않는다."""
    from labels.pano_meta import is_walk
    assert is_walk("999")
    assert is_walk(None)


def test_is_walk_coerces_numeric_shot_tool():
    """응답이 숫자 102 로 와도 차량이다 — 안 그러면 차량 전체가 조용히 도보가 된다."""
    from labels.pano_meta import is_walk
    assert not is_walk(102)
    assert not is_walk("102")
    assert is_walk(100)


def _walk_sample(tmp_path, sid, cid, folder):
    """캡처 시점 경로는 pos/ 인데 검수가 다른 폴더로 옮긴 상황을 만든다."""
    d = tmp_path / "images" / cid / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}_999_090.0_T.png").write_bytes(b"png")
    return {"type": "sample", "sample_id": sid, "course_id": cid, "label": True,
            "label_source": "route", "pano_id": "999", "lat": 37.5, "lng": 127.0,
            "heading": 90.0, "image": f"{cid}/pos/{sid}_999_090.0_T.png"}


def test_find_image_follows_review_moves(tmp_path):
    """검수가 discard/ 로 옮겨도 찾는다 — samples.jsonl 의 경로를 믿지 않는다."""
    from labels import dataset
    from labels.pano_meta import find_image
    paths = dataset.at("t", tmp_path)
    row = _walk_sample(tmp_path, "s-000p", "c-01", "discard")
    found = find_image(paths, row)
    assert found is not None, "옮겨진 이미지를 못 찾았다"
    assert found.parent.name == "discard"


def test_find_image_returns_none_when_absent(tmp_path):
    from labels import dataset
    from labels.pano_meta import find_image
    paths = dataset.at("t", tmp_path)
    (tmp_path / "images" / "c-01").mkdir(parents=True)
    assert find_image(paths, {"course_id": "c-01", "sample_id": "s-000p"}) is None
