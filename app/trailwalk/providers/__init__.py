"""provider 팩토리. 이름 하나로 갈아끼운다.

fixture 로 배선을 확인한 뒤 kakao 로 바꾸는 것이 기본 작업 순서다.
"""
import os
from pathlib import Path

from .base import Pano, ProviderError, RoadviewProvider  # noqa: F401  (재수출)

NAMES = ("fixture", "kakao")


def make(name: str, **kw) -> RoadviewProvider:
    if name == "fixture":
        from .fixture import FixtureProvider
        root = Path(__file__).resolve().parents[3]
        return FixtureProvider(kw.get("image_dir") or root / "bench" / "images")
    if name == "kakao":
        from .kakao import KakaoProvider
        return KakaoProvider(
            appkey=kw.get("appkey") or os.environ.get("KAKAO_JS_KEY", ""),
            headless=kw.get("headless", True))
    raise ProviderError(f"모르는 provider: {name!r} (가능: {', '.join(NAMES)})")
