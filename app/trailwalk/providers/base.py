"""로드뷰 provider 인터페이스.

조사 결과(docs/21-roadview-providers.md) 때문에 인터페이스가 이 모양이다.
어느 제공자도 "이웃 파노라마 목록"을 주지 않는다. Google 만 JS SDK 에 links[] 가
있고 한국 커버리지가 없다. 그래서 **그래프 순회를 전제하지 않는다.**

    nearest(lat, lng, r) → 좌표를 pano 로 스냅
    capture(pano, heading) → 그 pano 를 heading 방향에서 본 한 화각

"다음 pano" 는 provider 가 아니라 walk.py 가 만든다: 현재 좌표에서 heading 방향으로
STEP_M 전진한 좌표를 계산하고 다시 nearest 를 부른다. 그래프가 없어도 걸을 수 있고,
서빙 쪽이 정한 경계("어디로 갈지는 클라이언트 몫")와도 맞는다.

capture 는 **인코딩된 이미지 바이트**를 돌려준다. data URI 로 바꾸는 일은
imaging.py 만 한다 — 규칙이 한 군데에만 있어야 조용히 깨지지 않는다.
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
