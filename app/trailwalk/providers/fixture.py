"""오프라인 provider. 로컬 이미지를 로드뷰인 척 돌려준다.

있는 이유가 두 가지다.

1. **API 키 없이 전체 루프를 돌릴 수 있다.** Kakao 는 JS 앱키가 필요하고 headless
   브라우저가 필요하다(docs/21-roadview-providers.md §4). 그게 준비되기 전에도
   imaging → vlm → walk → runlog 배선이 맞는지는 확인할 수 있어야 한다.

2. **회귀 테스트가 된다.** 같은 이미지에 같은 프롬프트면 판정도 같아야 한다
   (temperature 0). 프롬프트를 고친 뒤 무엇이 뒤집혔는지 바로 보인다.

좌표는 격자로 가짜 pano 를 만든다. GRID_M 격자에 스냅하므로 같은 자리를 두 번
밟으면 pano_id 도 같고, walk.py 의 재방문 감지가 실제로 동작하는지 확인된다.
"""
import hashlib
from pathlib import Path

from .base import Pano, ProviderError

GRID_M = 10.0          # 가짜 pano 격자 간격 (m)
_M_PER_DEG_LAT = 111_320.0


class FixtureProvider:
    name = "fixture"

    def __init__(self, image_dir: Path | str):
        self.dir = Path(image_dir)
        self.images = sorted(p for p in self.dir.iterdir()
                             if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
        if not self.images:
            raise ProviderError(f"이미지가 없다: {self.dir}")

    def nearest(self, lat: float, lng: float, radius_m: float) -> Pano | None:
        step_lat = GRID_M / _M_PER_DEG_LAT
        glat = round(lat / step_lat) * step_lat
        # 경도 격자 폭은 **스냅된** 위도로 정한다. 입력 위도를 그대로 쓰면 위도가
        # 조금만 달라져도 경도 격자 자체가 미끄러져, 같은 자리인데 pano_id 가
        # 달라진다. 그러면 재방문 감지가 영원히 동작하지 않는다.
        step_lng = GRID_M / (_M_PER_DEG_LAT * max(0.1, abs(_cos(glat))))
        glng = round(lng / step_lng) * step_lng
        return Pano(pano_id=f"fx_{glat:.6f}_{glng:.6f}", lat=glat, lng=glng)

    def capture(self, pano: Pano, heading: float, fov_deg: float) -> bytes:
        """pano_id + heading 을 해시해 이미지를 고른다.

        결정적이어야 한다 — 같은 자리를 같은 방향에서 보면 같은 그림이 나와야
        재방문 감지와 회귀 비교가 의미를 갖는다.
        """
        key = f"{pano.pano_id}|{int(heading) // 15}".encode()
        idx = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % len(self.images)
        return self.images[idx].read_bytes()

    def close(self) -> None:
        pass


def _cos(deg: float) -> float:
    import math
    return math.cos(math.radians(deg))
