"""테스트 공용 픽스처.

여기 테스트는 **브라우저도 VLM 서버도 없이** 돈다. 둘 다 있어야 하는 검증은
`app/check_kakao.py` 와 실제 walk 런의 몫이다. 유닛 테스트가 그것들을 대신할 수
없고, 대신하려 들면 느리고 잘 깨지는 테스트가 된다.

대신 여기서는 **불변식**을 건다. 이 레포의 사고는 거의 다 예외 없이 일어났기
때문에 "안 터졌다" 는 아무것도 보장하지 않는다.
"""
import io
import threading

import pytest
from PIL import Image

from trailwalk.providers.base import Neighbor, Pano


def make_image(size=(1280, 720), fmt="JPEG", color=(90, 140, 90)) -> bytes:
    """디코드 가능한 진짜 이미지 바이트. imaging 을 실제로 통과시켜야 의미가 있다."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


# 캡처 신분증을 색으로 싣는다. 32 단위라 JPEG 재인코딩의 오차(몇 단위)보다
# 훨씬 크고, 8×8 = 64개까지 구분한다 — 테스트 한 건의 판정 수로 충분하다.
_STEP, _BASE = 32, 16


def _ident_color(i: int) -> tuple[int, int, int]:
    assert i < 64, f"테스트가 캡처 64건을 넘었다 ({i}) — 인코딩을 넓혀야 한다"
    return (_BASE + (i % 8) * _STEP, _BASE + (i // 8) * _STEP, 128)


def _ident_of(uri: str) -> int:
    """data URI 를 되읽어 몇 번째 캡처였는지."""
    import base64
    raw = base64.b64decode(uri.split(",", 1)[1])
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    r, g, _ = im.getpixel((im.width // 2, im.height // 2))
    def near(v: int) -> int:
        return max(0, min(7, round((v - _BASE) / _STEP)))

    return near(r) + near(g) * 8


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
        self.camera_surface = None
        self.nature_level = None
        self.footway = None
        # 기록 계층(store.RunWriter)이 읽는 필드까지 갖춰야 runner 테스트가
        # 이 픽스처를 그대로 쓸 수 있다 — 진짜 Verdict(vlm.py)와 같은 모양
        self.prompt_tokens = 100
        self.cached_tokens = 90
        self.completion_tokens = 10
        self.latency_ms = 1.0


class Provider:
    """이웃 그래프를 흉내낸다. 빈 목록은 **로드 실패**로 읽힌다 (갈래 없음이 아니라)."""

    name = "fake"

    def __init__(self, graph=None, start=("S", 37.5, 127.0)):
        self.graph = graph or {}
        self.start = Pano(pano_id=start[0], lat=start[1], lng=start[2])
        self.probes = []          # (pano_id, heading) — 무엇을 물었는지
        self.nearest_calls = 0
        self._lock = threading.Lock()

    def nearest(self, lat, lng, radius_m):
        self.nearest_calls += 1
        if self.nearest_calls == 1:
            return self.start
        # 시작점 스냅은 한 번뿐이다. 그 뒤로 불리면 이동이 그래프를 안 쓴 것이다
        return Pano(pano_id=f"p{self.nearest_calls}", lat=lat, lng=lng)

    def neighbors(self, pano):
        return list(self.graph.get(pano.pano_id, []))

    def capture(self, pano, heading):
        """캡처마다 **다른 색**의 이미지를 준다.

        색이 곧 그 캡처의 신분증이다. 판정을 누가 요청했는지 알아내는 유일한
        수단이 이것뿐이라서다 — 캡처와 판정이 겹쳐 도는 순간 "몇 번째로
        불렸나" 로는 짝을 못 짓는다 (→ Client).

        32 단위로 띄엄띄엄 고른다. imaging 이 리사이즈 + JPEG 재인코딩을
        하므로 값이 몇 단위 흔들리는데, 그보다 훨씬 큰 간격이라 안 섞인다.
        """
        with self._lock:
            i = len(self.probes)
            self.probes.append((pano.pano_id, round(heading, 1)))
        return make_image(size=(320, 180), color=_ident_color(i))

    def close(self):
        pass


class Client:
    """**받은 이미지가 어느 캡처인지** 보고 판정을 돌려준다.

    verdicts 에 없는 조합은 False. "명시한 것만 산책로" 라서 테스트가
    실수로 통과하는 일이 없다.

    짝을 어떻게 짓느냐로 두 번 틀렸다. 처음엔 `probes[-1]`(가장 최근 캡처)을
    봤는데, 루프가 답을 기다리지 않고 다음을 캡처하게 되자 그 사이 끼어든
    캡처를 집었다. 다음엔 캡처 순서대로 하나씩 꺼냈는데, 워커가 여럿이 되자
    판정이 도착하는 순서가 보낸 순서와 달라져 또 어긋났다.

    둘 다 "몇 번째냐" 로 짝을 지으려 한 것이 문제였다. 진짜 `VlmClient` 는
    URI 를 인자로 받으므로 **이미지 자체가 신분증**이다 — 가짜도 그렇게 한다.
    """

    def __init__(self, provider, verdicts):
        self.provider = provider
        self.verdicts = verdicts

    def assess(self, uri, *, heading=None):
        return Verdict(self.verdicts.get(
            self.provider.probes[_ident_of(uri)], False))


def nb(pano_id, heading, lat=37.5, lng=127.0):
    return Neighbor(pano_id=pano_id, heading=heading, lat=lat, lng=lng)
