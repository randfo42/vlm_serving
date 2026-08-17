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


class ProviderError(RuntimeError):
    pass


@runtime_checkable
class RoadviewProvider(Protocol):
    name: str

    def nearest(self, lat: float, lng: float, radius_m: float) -> Pano | None:
        """좌표에서 radius_m 안의 가장 가까운 파노라마. 없으면 None."""
        ...

    def capture(self, pano: Pano, heading: float, fov_deg: float) -> bytes:
        """pano 를 heading 방향에서 본 한 화각. 인코딩된 이미지 바이트."""
        ...

    def close(self) -> None:
        ...
