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

# `geo.destination` 을 왕복으로 검증하던 테스트들이 여기 있었다. 함수를 지웠다 —
# 좌표를 밀어 다음 지점을 지어내는 이동이 없어졌기 때문이다 (→ 20-app-design.md §3).
# 그 자리를 대신하는 것이 아래 fixture 격자 그래프의 왕복 테스트다.

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


def test_날짜변경선을_넘어도_방위가_유효하다():
    """-180/180 근처에서 방위가 튀면 후보 정렬이 뒤집힌다."""
    b = geo.bearing_deg((0.0, 179.999), (0.0, -179.999))
    assert geo.angle_diff(b, 90.0) < 1e-6


# ── fixture provider 의 격자 스냅 ───────────────────────────────────────────
#
# ⚠️ 실제 버그: 경도 격자 폭을 **입력** 위도로 계산했다. 위도가 조금만 달라져도
# 경도 격자 자체가 미끄러져서, 같은 자리인데 pano_id 가 매번 달라졌다.
# 그러면 탐색 루프의 재방문 감지가 영원히 동작하지 않는다 — 예외는 안 나고
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


def test_충분히_멀면_다른_pano다(fx):
    far = (SEOUL[0], SEOUL[1] + GRID_M * 10 / (111_320.0 * math.cos(math.radians(SEOUL[0]))))
    assert fx.nearest(*far, 25.0).pano_id != fx.nearest(*SEOUL, 25.0).pano_id


def test_스냅된_좌표가_원점에서_격자_안에_있다(fx):
    pano = fx.nearest(*SEOUL, 25.0)
    assert geo.haversine_m(SEOUL, (pano.lat, pano.lng)) < GRID_M


def test_같은_지점_같은_방향은_같은_그림이다(fx):
    """결정적이어야 회귀 비교가 의미를 갖는다."""
    pano = fx.nearest(*SEOUL, 25.0)
    assert fx.capture(pano, 90.0) == fx.capture(pano, 90.0)


# ── fixture 의 격자 그래프 ──────────────────────────────────────────────────
#
# 이동 수단이 이웃 그래프 하나뿐이라(→ 20-app-design.md §3) fixture 도 그래프를
# 줘야 한다. 예전에는 빈 목록을 주고 walk 가 좌표 밀기로 되돌아갔다.

def test_격자_이웃은_네_방향이다(fx):
    nbrs = fx.neighbors(fx.nearest(*SEOUL, 25.0))
    assert sorted(n.heading for n in nbrs) == [0.0, 90.0, 180.0, 270.0]


def test_이웃의_pano_id는_그_좌표로_스냅한_것과_같다(fx):
    """다르면 재방문 감지가 영원히 안 걸린다 — 같은 자리를 매번 새 pano 로 본다."""
    for n in fx.neighbors(fx.nearest(*SEOUL, 25.0)):
        assert fx.nearest(n.lat, n.lng, 25.0).pano_id == n.pano_id


def test_한_바퀴_돌아오면_같은_pano다(fx):
    """재방문 감지가 실제로 걸리는지. 이게 안 되면 루프를 영원히 못 잡는다.

    격자를 그래프로 북→동→남→서 한 바퀴 돈다. 좌표를 미는 게 아니라
    이웃을 따라간다 — 실제 탐색이 하는 것과 같은 이동이다.
    """
    start = fx.nearest(*SEOUL, 25.0)
    p = start
    for heading in (0.0, 90.0, 180.0, 270.0):
        n = next(x for x in fx.neighbors(p) if x.heading == heading)
        p = fx.nearest(n.lat, n.lng, 25.0)
    assert p.pano_id == start.pano_id


def test_이웃의_이웃에는_원래_자리가_들어_있다(fx):
    """되짚어 오는 길이 있어야 walk 의 `came_from` 제외가 의미를 갖는다."""
    start = fx.nearest(*SEOUL, 25.0)
    north = fx.neighbors(start)[0]
    back = fx.nearest(north.lat, north.lng, 25.0)
    assert start.pano_id in {n.pano_id for n in fx.neighbors(back)}


def test_이미지가_없으면_바로_실패한다(tmp_path):
    from trailwalk.providers.base import ProviderError
    with pytest.raises(ProviderError):
        FixtureProvider(tmp_path)


def test_극지방에서도_경도_격자가_폭발하지_않는다(fx):
    """cos(위도) 가 0 에 수렴하면 격자 폭이 무한대가 된다. 하한이 걸려 있어야 한다."""
    pano = fx.nearest(89.999, 127.0, 25.0)
    assert math.isfinite(pano.lng)
