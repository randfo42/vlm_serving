"""측지 계산. 탐색 루프가 "이 방향으로 N미터" 를 좌표로 바꿀 때 쓴다.

로드뷰 provider 가 이웃 pano 그래프를 주지 않으므로(→ docs/21-roadview-providers.md §4)
이동은 전부 여기서 만든다: 현재 좌표에서 heading 방향으로 STEP_M 만큼 전진한 좌표를
계산하고, provider 에게 "그 좌표에서 가장 가까운 pano" 를 달라고 한다.

구면 근사(반지름 6371008.8m)로 충분하다. 스텝이 10m 규모라 타원체 보정과의 차이는
센티미터 단위이고, 어차피 pano 스냅 반경이 수십 미터다.
"""
import math

R = 6_371_008.8  # IUGG 평균 지구 반지름 (m)


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 (lat, lng) 사이 대권 거리 (m)."""
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """a 에서 b 로 향하는 초기 방위각. 0=북, 시계방향, [0, 360)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlng = math.radians(b[1] - a[1])
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return math.degrees(math.atan2(y, x)) % 360.0


def destination(a: tuple[float, float], bearing: float, dist_m: float) -> tuple[float, float]:
    """a 에서 bearing 방향으로 dist_m 만큼 간 좌표."""
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    br, ad = math.radians(bearing), dist_m / R
    lat2 = math.asin(math.sin(lat1) * math.cos(ad) + math.cos(lat1) * math.sin(ad) * math.cos(br))
    lng2 = lng1 + math.atan2(math.sin(br) * math.sin(ad) * math.cos(lat1),
                             math.cos(ad) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180


def norm_deg(d: float) -> float:
    """[0, 360) 으로 정규화."""
    return d % 360.0


def angle_diff(a: float, b: float) -> float:
    """두 방위각의 최소 차이. 항상 [0, 180]."""
    return abs((a - b + 180) % 360 - 180)
