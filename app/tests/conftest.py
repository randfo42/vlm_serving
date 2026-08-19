"""테스트 공용 픽스처.

여기 테스트는 **브라우저도 VLM 서버도 없이** 돈다. 둘 다 있어야 하는 검증은
`app/check_kakao.py` 와 실제 walk 런의 몫이다. 유닛 테스트가 그것들을 대신할 수
없고, 대신하려 들면 느리고 잘 깨지는 테스트가 된다.

대신 여기서는 **불변식**을 건다. 이 레포의 사고는 거의 다 예외 없이 일어났기
때문에 "안 터졌다" 는 아무것도 보장하지 않는다.
"""
import io

import pytest
from PIL import Image

from trailwalk.providers.base import Neighbor, Pano


def make_image(size=(1280, 720), fmt="JPEG", color=(90, 140, 90)) -> bytes:
    """디코드 가능한 진짜 이미지 바이트. imaging 을 실제로 통과시켜야 의미가 있다."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return make_image()


@pytest.fixture
def webp_bytes() -> bytes:
    """WEBP. 서버가 HTTP 200 을 주면서 조용히 무시하는 포맷이다."""
    return make_image(fmt="WEBP")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """재시도 백오프를 실시간으로 기다리지 않는다.

    vlm.assess 는 503/500 에서 1→2→4초 백오프를 건다. 그대로 두면 재시도
    테스트 하나가 7초를 먹는다. 백오프 **로직**은 호출 횟수로 검증하고
    실제 대기는 없앤다.
    """
    monkeypatch.setattr("time.sleep", lambda *_: None)


# ── 탐색 루프용 가짜 provider/client ────────────────────────────────────────
#
# 두 루프가 같은 판정 배선을 공유한다는 사실 자체가 지킬 불변식이라, 픽스처도
# 하나만 둔다. (한때 test_walk.py 안에 있었고 test_explore.py 가 그것을 import
# 했다 — 테스트 파일끼리의 import 는 한쪽을 지우면 다른 쪽이 collection 에서
# 죽는다.)

class Verdict:
    def __init__(self, is_trail):
        self.is_trail = is_trail
        self.confidence = None


class Provider:
    """이웃 그래프를 흉내낸다. 빈 목록은 **로드 실패**로 읽힌다 (갈래 없음이 아니라)."""

    name = "fake"

    def __init__(self, graph=None, start=("S", 37.5, 127.0)):
        self.graph = graph or {}
        self.start = Pano(pano_id=start[0], lat=start[1], lng=start[2])
        self.probes = []          # (pano_id, heading) — 무엇을 물었는지
        self.nearest_calls = 0
        self._img = make_image(size=(320, 180))

    def nearest(self, lat, lng, radius_m):
        self.nearest_calls += 1
        if self.nearest_calls == 1:
            return self.start
        # 시작점 스냅은 한 번뿐이다. 그 뒤로 불리면 이동이 그래프를 안 쓴 것이다
        return Pano(pano_id=f"p{self.nearest_calls}", lat=lat, lng=lng)

    def neighbors(self, pano):
        return list(self.graph.get(pano.pano_id, []))

    def capture(self, pano, heading):
        self.probes.append((pano.pano_id, round(heading, 1)))
        return self._img

    def close(self):
        pass


class Client:
    """probes 의 마지막 항목을 보고 판정을 돌려준다.

    verdicts 에 없는 조합은 False. "명시한 것만 산책로" 라서 테스트가
    실수로 통과하는 일이 없다.
    """

    def __init__(self, provider, verdicts):
        self.provider = provider
        self.verdicts = verdicts

    def assess(self, uri, *, heading=None):
        return Verdict(self.verdicts.get(self.provider.probes[-1], False))


def nb(pano_id, heading, lat=37.5, lng=127.0):
    return Neighbor(pano_id=pano_id, heading=heading, lat=lat, lng=lng)
