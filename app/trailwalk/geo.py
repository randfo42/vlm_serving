"""측지 계산 — 거리와 각도.

**이동을 만들지 않는다.** 한때 "현재 좌표에서 heading 방향으로 N미터" 를 계산해
다음 지점을 지어내는 함수(`destination`)가 여기 있었고 그게 탐색의 이동 수단이었다.
지금은 이동이 이웃 그래프 하나뿐이라(→ docs/20-app-design.md §3) 그 함수를 지웠다.
지도가 알려준 지점으로만 걷고, 우리가 좌표를 만들어내지 않는다.

남은 것은 재는 함수들이다: 두 지점 사이 거리(haversine_m), 방위(bearing_deg),
각도 정규화·차이(norm_deg, angle_diff). 스냅 검증·후보 정렬·경로 길이 집계에 쓴다.

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


def norm_deg(d: float) -> float:
    """[0, 360) 으로 정규화."""
    return d % 360.0


def angle_diff(a: float, b: float) -> float:
    """두 방위각의 최소 차이. 항상 [0, 180]."""
    return abs((a - b + 180) % 360 - 180)
