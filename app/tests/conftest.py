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
