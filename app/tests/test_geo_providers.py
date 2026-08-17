"""측지 계산과 provider 의 좌표 취급.

여기 버그는 전부 **틀린 좌표로 조용히 잘 도는** 종류다. 예외가 안 나고
경로만 이상해진다. 그래서 왕복(round-trip) 불변식으로 건다.
"""
import math

import pytest

from trailwalk import geo
from trailwalk.providers.fixture import GRID_M, FixtureProvider

SEOUL = (37.5665, 126.9780)


# ── geo ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 315, 359.9])
@pytest.mark.parametrize("dist", [1.0, 12.0, 500.0])
def test_이동한_만큼_떨어져_있다(bearing, dist):
    dst = geo.destination(SEOUL, bearing, dist)
    assert geo.haversine_m(SEOUL, dst) == pytest.approx(dist, rel=1e-6)


@pytest.mark.parametrize("bearing", [0, 30, 90, 180, 270, 350])
def test_이동한_방향이_그_방위다(bearing):
    # 0 과 360 은 같은 방위다. 뺄셈으로 비교하면 북쪽에서만 실패한다 —
    # angle_diff 가 있는 이유가 바로 이것이므로 그걸로 잰다.
    dst = geo.destination(SEOUL, bearing, 100.0)
    assert geo.angle_diff(geo.bearing_deg(SEOUL, dst), bearing) < 1e-6


def test_방위각은_항상_0에서_360():
    for b in (-720.0, -1.0, 0.0, 359.9, 360.0, 1080.5):
        assert 0.0 <= geo.norm_deg(b) < 360.0


@pytest.mark.parametrize(("a", "b", "want"), [
    (0.0, 0.0, 0.0),
    (10.0, 350.0, 20.0),      # 0도를 넘어가는 경우 — 여기서 틀리기 쉽다
    (350.0, 10.0, 20.0),
    (0.0, 180.0, 180.0),
    (0.0, 181.0, 179.0),      # 180 을 넘으면 반대쪽으로 재야 한다
    (91.36, 89.3, 2.06),
])
def test_각도차는_짧은_쪽으로_잰다(a, b, want):
    assert geo.angle_diff(a, b) == pytest.approx(want, abs=1e-9)


def test_각도차는_대칭이다():
    assert geo.angle_diff(10.0, 350.0) == geo.angle_diff(350.0, 10.0)


def test_날짜변경선을_넘어도_경도가_유효하다():
    """-180/180 근처에서 경도가 튀면 스냅이 지구 반대편을 찍는다."""
    lat, lng = geo.destination((0.0, 179.999), 90.0, 1000.0)
    assert -180.0 <= lng <= 180.0
    assert geo.haversine_m((0.0, 179.999), (lat, lng)) == pytest.approx(1000.0, rel=1e-6)


# ── fixture provider 의 격자 스냅 ───────────────────────────────────────────
#
# ⚠️ 실제 버그: 경도 격자 폭을 **입력** 위도로 계산했다. 위도가 조금만 달라져도
# 경도 격자 자체가 미끄러져서, 같은 자리인데 pano_id 가 매번 달라졌다.
# 그러면 walk.py 의 재방문 감지가 영원히 동작하지 않는다 — 예외는 안 나고
# 그냥 루프를 못 잡는다.

@pytest.fixture
def fx(tmp_path):
    from conftest import make_image
    for i in range(3):
        (tmp_path / f"{i}.jpg").write_bytes(make_image(size=(64, 36)))
    return FixtureProvider(tmp_path)


def test_같은_지점은_같은_pano로_스냅된다(fx):
    a = fx.nearest(*SEOUL, 25.0)
    b = fx.nearest(SEOUL[0] + 1e-9, SEOUL[1] - 1e-9, 25.0)
    assert a.pano_id == b.pano_id


def test_한_격자_칸_안에서_경도가_미끄러지지_않는다(fx):
    """⚠️ 핵심 회귀. 경도 격자 폭을 **스냅된** 위도로 정하는지 확인한다.

    입력 위도를 그대로 쓰면 위도가 1e-6도만 달라져도 경도 격자 폭이 바뀌고,
    스냅된 경도가 소수 6자리에서 흔들린다. pano_id 가 그 6자리로 만들어지므로
    같은 자리인데 id 가 달라지고, 재방문 감지가 영원히 동작하지 않는다.

    격자 칸을 **넘어가면** 경도값이 달라지는 것은 정상이다 — 10m 밖은 다른
    자리이고 다른 pano 여야 한다. 그래서 한 칸 안에서만 흔든다.
    """
    cell = GRID_M / 111_320.0
    ids = {fx.nearest(37.5 + d, 127.0, 25.0).pano_id
           for d in (-cell / 3, -cell / 9, 0.0, cell / 9, cell / 3)}
    assert len(ids) == 1, f"한 칸 안에서 pano_id 가 갈렸다: {sorted(ids)}"


def test_한_바퀴_돌아오면_같은_pano다(fx):
    """재방문 감지가 실제로 걸리는지. 이게 안 되면 루프를 영원히 못 잡는다."""
    p = SEOUL
    for b in (0.0, 90.0, 180.0, 270.0):
        p = geo.destination(p, b, GRID_M * 3)
    assert fx.nearest(*p, 25.0).pano_id == fx.nearest(*SEOUL, 25.0).pano_id


def test_충분히_멀면_다른_pano다(fx):
    far = geo.destination(SEOUL, 90.0, GRID_M * 10)
    assert fx.nearest(*far, 25.0).pano_id != fx.nearest(*SEOUL, 25.0).pano_id


def test_스냅된_좌표가_원점에서_격자_안에_있다(fx):
    pano = fx.nearest(*SEOUL, 25.0)
    assert geo.haversine_m(SEOUL, (pano.lat, pano.lng)) < GRID_M


def test_같은_지점_같은_방향은_같은_그림이다(fx):
    """결정적이어야 회귀 비교가 의미를 갖는다."""
    pano = fx.nearest(*SEOUL, 25.0)
    assert fx.capture(pano, 90.0, 90.0) == fx.capture(pano, 90.0, 90.0)


def test_fixture는_이웃을_주지_않는다(fx):
    """일부러 비워 둔다 — 그래프 경로와 폴백 경로가 **둘 다** 테스트되도록."""
    assert fx.neighbors(fx.nearest(*SEOUL, 25.0)) == []


def test_이미지가_없으면_바로_실패한다(tmp_path):
    from trailwalk.providers.base import ProviderError
    with pytest.raises(ProviderError):
        FixtureProvider(tmp_path)


def test_극지방에서도_경도_격자가_폭발하지_않는다(fx):
    """cos(위도) 가 0 에 수렴하면 격자 폭이 무한대가 된다. 하한이 걸려 있어야 한다."""
    pano = fx.nearest(89.999, 127.0, 25.0)
    assert math.isfinite(pano.lng)
