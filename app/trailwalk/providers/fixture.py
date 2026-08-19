"""오프라인 provider. 로컬 이미지를 로드뷰인 척 돌려준다.

있는 이유가 두 가지다.

1. **API 키 없이 전체 루프를 돌릴 수 있다.** Kakao 는 JS 앱키가 필요하고 headless
   브라우저가 필요하다(docs/21-roadview-providers.md §4). 그게 준비되기 전에도
   imaging → vlm → walk → runlog 배선이 맞는지는 확인할 수 있어야 한다.

2. **회귀 테스트가 된다.** 같은 이미지에 같은 프롬프트면 판정도 같아야 한다
   (temperature 0). 프롬프트를 고친 뒤 무엇이 뒤집혔는지 바로 보인다.

좌표는 격자로 가짜 pano 를 만든다. GRID_M 격자에 스냅하므로 같은 자리를 두 번
밟으면 pano_id 도 같고, 탐색 루프의 재방문 감지가 실제로 동작하는지 확인된다.

### 격자가 곧 이웃 그래프다

예전에는 `neighbors()` 가 빈 목록을 줘서 walk 가 좌표 밀기로 되돌아갔고,
그 폴백 경로를 테스트하는 것이 fixture 의 역할 중 하나였다. **그 경로를
없앴다** — 이동은 이제 이웃 그래프 하나뿐이다 (→ docs/20-app-design.md §3).
격자는 원래 그래프이므로 fixture 도 4방향 이웃을 정직하게 준다.
"""
import hashlib
from pathlib import Path

from ..settings import SETTINGS
from .base import Neighbor, Pano, ProviderError

# 값의 정본은 app/config/trailwalk.yaml (fixture.grid_m). 여기 것은 그 별칭이고,
# 생성자에서 덮어쓸 수 있다 — 격자 간격을 바꾼 회귀 테스트를 짤 수 있게.
GRID_M = SETTINGS.fixture.grid_m
_M_PER_DEG_LAT = 111_320.0


class FixtureProvider:
    name = "fixture"

    def __init__(self, image_dir: Path | str, *, grid_m: float = GRID_M):
        self.grid_m = grid_m
        self.dir = Path(image_dir)
        self.images = sorted(p for p in self.dir.iterdir()
                             if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
        if not self.images:
            raise ProviderError(f"이미지가 없다: {self.dir}")

    def nearest(self, lat: float, lng: float, radius_m: float) -> Pano | None:
        step_lat = self.grid_m / _M_PER_DEG_LAT
        glat = round(lat / step_lat) * step_lat
        # 경도 격자 폭은 **스냅된** 위도로 정한다. 입력 위도를 그대로 쓰면 위도가
        # 조금만 달라져도 경도 격자 자체가 미끄러져, 같은 자리인데 pano_id 가
        # 달라진다. 그러면 재방문 감지가 영원히 동작하지 않는다.
        step_lng = self.grid_m / (_M_PER_DEG_LAT * max(0.1, abs(_cos(glat))))
        glng = round(lng / step_lng) * step_lng
        return Pano(pano_id=f"fx_{glat:.6f}_{glng:.6f}", lat=glat, lng=glng)

    def capture(self, pano: Pano, heading: float) -> bytes:
        """pano_id + heading 을 해시해 이미지를 고른다.

        결정적이어야 한다 — 같은 자리를 같은 방향에서 보면 같은 그림이 나와야
        재방문 감지와 회귀 비교가 의미를 갖는다.

        ⚠️ **화각은 원본 사진이 갖고 있던 값이고 우리는 그게 뭔지 모른다.**
        사진마다 다르다. kakao(90.9° 고정)와 fixture 의 판정을 화각 축에서
        비교하면 안 되는 이유다 (→ docs/23-open-questions.md §3, §6).
        """
        key = f"{pano.pano_id}|{int(heading) // 15}".encode()
        idx = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % len(self.images)
        return self.images[idx].read_bytes()

    def neighbors(self, pano: Pano) -> list[Neighbor]:
        """격자 4방향(북·동·남·서).

        pano_id 는 `nearest()` 에 다시 태워서 만든다. 직접 계산하면 경도 격자
        폭이 위도에 따라 달라지는 문제(→ nearest 주석)를 여기서 또 틀리게 된다.
        같은 자리는 언제 도달하든 같은 id 여야 재방문 감지가 동작한다.
        """
        step_lat = self.grid_m / _M_PER_DEG_LAT
        step_lng = self.grid_m / (_M_PER_DEG_LAT * max(0.1, abs(_cos(pano.lat))))
        out = []
        for heading, dlat, dlng in ((0.0, step_lat, 0.0), (90.0, 0.0, step_lng),
                                    (180.0, -step_lat, 0.0), (270.0, 0.0, -step_lng)):
            n = self.nearest(pano.lat + dlat, pano.lng + dlng, 0.0)
            out.append(Neighbor(pano_id=n.pano_id, heading=heading, lat=n.lat, lng=n.lng))
        return out

    def close(self) -> None:
        pass


def _cos(deg: float) -> float:
    import math
    return math.cos(math.radians(deg))
