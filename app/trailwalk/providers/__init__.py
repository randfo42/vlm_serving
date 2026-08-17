"""provider 팩토리. 이름 하나로 갈아끼운다.

fixture 로 배선을 확인한 뒤 kakao 로 바꾸는 것이 기본 작업 순서다.
"""
from pathlib import Path

from .base import Pano, ProviderError, RoadviewProvider  # noqa: F401  (재수출)

NAMES = ("fixture", "kakao")


def make(name: str, **kw) -> RoadviewProvider:
    if name == "fixture":
        from .fixture import FixtureProvider
        root = Path(__file__).resolve().parents[3]
        return FixtureProvider(kw.get("image_dir") or root / "bench" / "images")
    if name == "kakao":
        from ..config import kakao_appkey
        from .kakao import KakaoProvider
        # 키는 여기서 한 번만 꺼내 바로 넘긴다. 어디에도 저장하거나 찍지 않는다.
        return KakaoProvider(appkey=kw.get("appkey") or kakao_appkey(),
                             headless=kw.get("headless", True))
    raise ProviderError(f"모르는 provider: {name!r} (가능: {', '.join(NAMES)})")
