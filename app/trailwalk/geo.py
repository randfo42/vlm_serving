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
from itertools import pairwise

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


# ── 폴리라인 ────────────────────────────────────────────────────────────────
#
# 아래 셋은 라벨 수집(app/labels/)용이다. resample_polyline 은 좌표를 보간해
# 만들지만, destination() 을 지운 원칙과 충돌하지 않는다 — destination 은
# **존재하지 않는 지점**을 heading 으로 지어냈고, 여기의 보간점은 지도가 준
# 실경로(길찾기 폴리라인) 위의 점이다. 게다가 보간점은 곧바로 provider 의
# nearest() 로 실측 pano 에 스냅되므로, 최종적으로 쓰이는 좌표는 언제나
# Kakao 가 준 좌표다.

def polyline_length_m(pts: list[tuple[float, float]]) -> float:
    """(lat, lng) 열의 누적 대권 거리 (m)."""
    return sum(haversine_m(a, b) for a, b in pairwise(pts))


def resample_polyline(pts: list[tuple[float, float]], interval_m: float,
                      ) -> list[tuple[float, float, float]]:
    """폴리라인을 등간격으로 다시 찍는다. 반환: [(lat, lng, heading), ...].

    heading 은 그 점이 놓인 세그먼트의 진행 방위각이다. 양 끝점은 보존된다
    (끝점의 heading 은 마지막 세그먼트의 방위각). 세그먼트 안 보간은 위경도
    선형이다 — 세그먼트가 수십~수백 m 라 대권과의 차이는 cm 단위다.
    """
    if len(pts) < 2:
        raise ValueError("점이 2개 이상이어야 한다")
    if interval_m <= 0:
        raise ValueError(f"interval_m > 0 이어야 한다 ({interval_m!r})")
    out: list[tuple[float, float, float]] = []
    carry = 0.0                     # 다음 샘플까지 남은 거리
    for a, b in pairwise(pts):
        seg = haversine_m(a, b)
        if seg == 0.0:
            continue
        h = bearing_deg(a, b)
        if not out:
            out.append((a[0], a[1], h))
        d = carry
        while d + interval_m <= seg:
            d += interval_m
            f = d / seg
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, h))
        carry = d - seg             # 세그먼트 경계를 넘겨 이어 잰다 (≤0)
    if not out:                     # 모든 세그먼트가 0m — 같은 점의 반복
        raise ValueError("폴리라인의 모든 점이 같은 위치다")
    last = pts[-1]
    if haversine_m((out[-1][0], out[-1][1]), last) > 1e-6:
        prev = pts[-2] if haversine_m(pts[-2], last) > 0 else (out[-1][0], out[-1][1])
        out.append((last[0], last[1], bearing_deg(prev, last)))
    return out


def point_to_polyline_m(pt: tuple[float, float], pts: list[tuple[float, float]]) -> float:
    """점에서 폴리라인까지 최소 거리 (m). 버퍼 필터("경로에서 150m 밖")용.

    세그먼트마다 점 주변 등장방형(equirectangular) 평면으로 투영해 점-선분
    거리를 잰다. 수백 m 규모에서 대권과의 차이는 cm 단위다.
    """
    if not pts:
        raise ValueError("빈 폴리라인")
    if len(pts) == 1:
        return haversine_m(pt, pts[0])
    coslat = math.cos(math.radians(pt[0]))

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return (math.radians(p[1] - pt[1]) * coslat * R,
                math.radians(p[0] - pt[0]) * R)

    best = math.inf
    for a, b in pairwise(pts):
        ax, ay = xy(a)
        bx, by = xy(b)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 == 0.0:
            d = math.hypot(ax, ay)
        else:
            # 원점(=pt)에서 선분 ab 로의 수선. 발이 선분 밖이면 끝점으로 클램프
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
            d = math.hypot(ax + t * dx, ay + t * dy)
        best = min(best, d)
    return best
