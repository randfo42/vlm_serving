"""로드뷰 provider 인터페이스.

    nearest(lat, lng, r)   → 좌표를 pano 로 스냅. **시작점에서만** 쓴다
    neighbors(pano)        → 한 발 갈 수 있는 이웃들. **유일한 이동 수단**이다
    capture(pano, heading) → 그 pano 를 heading 방향에서 본 한 화각

이동은 이웃 그래프 하나뿐이다. 처음엔 이웃을 얻을 길이 없다고 보고 "heading
방향으로 N미터 민 좌표를 다시 스냅" 하는 방식으로 걸었는데, 로드뷰 화면의
흰 화살표가 바로 그 데이터였다 (→ docs/21-roadview-providers.md §1.3). 그
폴백은 없앴다 — 지도가 알려준 지점으로만 걷고, 좌표를 지어내지 않는다
(→ docs/20-app-design.md §3).

capture 는 **인코딩된 이미지 바이트**를 돌려준다. data URI 로 바꾸는 일은
imaging.py 만 한다 — 규칙이 한 군데에만 있어야 조용히 깨지지 않는다.
화각도 provider 가 정한다 (→ capture 독스트링).
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Pano:
    pano_id: str
    lat: float
    lng: float
    # provider 가 촬영 시점을 알려주면 담는다. 오래된 파노라마는 판정이 틀려도
    # 모델 탓이 아닐 수 있어서 런로그에 남길 가치가 있다.
    captured_at: str | None = None


@dataclass(frozen=True)
class Neighbor:
    """이 pano 에서 **한 발 갈 수 있는** 이웃.

    로드뷰 화면에 흰 화살표로 그려지는 바로 그것이다. 처음엔 이 정보를 얻을
    길이 없다고 보고 좌표를 직접 미는 방식으로 설계했는데(→ 21-…-providers.md
    §1.3), 화면에 그려지는 이상 어딘가에는 데이터가 있었다.

    heading 은 **정확한 방위각**이다. 화면 화살표의 8방위 라벨(북/북서/…)은
    표시용으로 버킷된 값이고, 실제 값은 91.36° 같은 소수다.
    """
    pano_id: str
    heading: float          # 이 pano 에서 이웃을 향하는 방위각 (0=북)
    lat: float
    lng: float
    name: str | None = None  # 도로/길 이름. 같은 길인지 갈라졌는지 보는 데 쓸모 있다


class ProviderError(RuntimeError):
    pass


@runtime_checkable
class RoadviewProvider(Protocol):
    # 프로토콜 본문에는 메서드만 둔다 (데이터 멤버는 runtime_checkable 과 안 맞는다).
    name: str

    def nearest(self, lat: float, lng: float, radius_m: float) -> Pano | None:
        """좌표에서 radius_m 안의 가장 가까운 파노라마. 없으면 None."""
        ...

    def capture(self, pano: Pano, heading: float) -> bytes:
        """pano 를 heading 방향에서 본 한 화각. 인코딩된 이미지 바이트.

        **화각은 provider 가 정한다.** 한때 `fov_deg` 를 인자로 받았는데,
        호출자가 화각을 지시하는 모양이면서 아무도 그 값을 쓰지 않았다 —
        Kakao 는 각도를 받을 수단 자체가 없고(zoom 은 −3~3 이산 배율),
        fixture 는 원본 사진의 화각을 그대로 돌려준다. 지시할 수 없는 것을
        지시하는 계약이라 지웠다 (→ docs/23-open-questions.md §3).
        """
        ...

    def neighbors(self, pano: Pano) -> list[Neighbor]:
        """이 pano 에서 한 발 갈 수 있는 이웃들.

        **필수 구현이다.** 이동 수단이 이것 하나뿐이다 — 좌표를 heading 방향으로
        밀어 스냅하던 폴백은 없앴다 (→ docs/20-app-design.md §3).

        빈 리스트는 "갈래가 없다" 가 아니라 **이웃 목록을 못 얻었다** 로 읽힌다.
        호출자는 거기서 멈추고 `neighbors_missing` 으로 기록한다. 진짜 막다른
        길은 "이웃은 있는데 전부 온 길/기방문" 이고, 그건 호출자가 판단한다.
        """
        ...

    def close(self) -> None:
        ...
