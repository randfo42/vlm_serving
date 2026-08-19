"""provider 팩토리. 이름 하나로 갈아끼운다.

fixture 로 배선을 확인한 뒤 kakao 로 바꾸는 것이 기본 작업 순서다.
"""
from pathlib import Path

from .base import Pano, ProviderError, RoadviewProvider  # noqa: F401  (재수출)

NAMES = ("fixture", "kakao")


def make(name: str, settings=None, **kw) -> RoadviewProvider:
    """provider 하나를 만든다. 기본값은 전부 settings 에서 온다.

    settings 를 안 주면 정본(app/config/trailwalk.yaml)을 쓴다 — 테스트와
    진단 도구(check_kakao.py 등)가 설정 파일을 몰라도 돌 수 있게.
    kw 는 그 위의 일회성 덮어쓰기다 (check_fov.py 가 뷰포트를 훑을 때 쓴다).
    """
    if settings is None:
        from ..settings import SETTINGS as settings

    if name == "fixture":
        from .fixture import FixtureProvider
        root = Path(__file__).resolve().parents[3]
        image_dir = kw.get("image_dir") or settings.fixture.images_dir
        return FixtureProvider(Path(image_dir) if image_dir else root / "bench" / "images",
                               grid_m=settings.fixture.grid_m)
    if name == "kakao":
        from ..config import kakao_appkey
        from .kakao import KakaoProvider
        # 키는 여기서 한 번만 꺼내 바로 넘긴다. 어디에도 저장하거나 찍지 않는다.
        # host/port/뷰포트/화살표는 KakaoProvider 가 settings 에서 알아서 채운다 —
        # 여기서 다시 풀어 넘기면 그게 또 하나의 정본이 된다.
        return KakaoProvider(appkey=kw.get("appkey") or kakao_appkey(),
                             headless=kw.get("headless"),
                             view_w=kw.get("view_w"), view_h=kw.get("view_h"),
                             hide_arrows=kw.get("hide_arrows"),
                             settings=settings)
    raise ProviderError(f"모르는 provider: {name!r} (가능: {', '.join(NAMES)})")
