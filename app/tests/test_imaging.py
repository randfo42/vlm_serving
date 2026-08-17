"""이미지 전처리 — 서버로 나가는 모든 바이트가 지나는 단일 출구.

### 왜 이 파일이 있나

**WEBP 를 보내면 서버가 HTTP 200 을 주고, 로그도 깨끗하고, 모델은 이미지를 못 본다.**
그러면 모델은 그럴듯한 JSON 을 지어낸다. 파싱도 성공한다. 값만 순전한 환각이다.

Kakao/Naver 가 WEBP 를 주기 시작하는 것은 우리가 통제할 수 없다. 통제할 수 있는
것은 "여기를 통과한 바이트는 무조건 JPEG 이다" 뿐이다.
"""
import base64
import io

import pytest
from PIL import Image

from conftest import make_image
from trailwalk.imaging import (
    TARGET_SIZE,
    ImagePreprocessError,
    equirect_to_view,
    view_to_data_uri,
)


def _decode(uri: str) -> Image.Image:
    head, b64 = uri.split(",", 1)
    assert head == "data:image/jpeg;base64"
    return Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "BMP", "TIFF"])
def test_무엇을_넣어도_JPEG가_나온다(fmt):
    """⚠️ 핵심 회귀 테스트. WEBP 가 그대로 나가면 조용히 실패한다."""
    uri, src = view_to_data_uri(make_image(fmt=fmt))
    assert uri.startswith("data:image/jpeg;base64,")
    assert _decode(uri).format == "JPEG"
    assert src == fmt


def test_원본_포맷을_그대로_보고한다():
    """변환은 항상 되지만, provider 가 포맷을 바꾼 사실은 런로그에 남아야 한다."""
    _, src = view_to_data_uri(make_image(fmt="WEBP"))
    assert src == "WEBP"


@pytest.mark.parametrize("size", [
    (1280, 720),      # 이미 목표
    (640, 360),       # 16:9 인데 작다
    (1000, 1000),     # 정사각
    (2000, 500),      # 아주 납작
    (300, 900),       # 세로로 김
])
def test_항상_같은_크기로_나온다(size):
    """크기가 흔들리면 이미지 토큰 수가 흔들리고, 그러면 지연도 비교 불가가 된다."""
    uri, _ = view_to_data_uri(make_image(size=size))
    assert _decode(uri).size == TARGET_SIZE


def test_늘리지_않고_잘라낸다():
    """늘려 맞추면 장면이 일그러져 "길이 어느 쪽으로 이어지는가" 가 왜곡된다.
    잘라내면 화각이 좁아질 뿐 보이는 것은 정직하다.

    확인 방법: 가로로 아주 긴 이미지의 **가운데**에만 표식을 둔다. 가운데를
    잘랐다면 표식이 살아 있고, 늘렸어도 살아 있다 — 그래서 가장자리를 본다.
    가장자리 색이 결과에 남아 있으면 잘라내지 않고 눌러 담은 것이다.
    """
    img = Image.new("RGB", (2000, 500), (0, 0, 0))
    for x in range(200):                       # 좌우 끝 200px 만 빨갛게
        for y in range(0, 500, 50):
            img.putpixel((x, y), (255, 0, 0))
            img.putpixel((1999 - x, y), (255, 0, 0))
    buf = io.BytesIO(); img.save(buf, format="PNG")

    out = _decode(view_to_data_uri(buf.getvalue())[0]).convert("RGB")
    reds = sum(1 for x in range(out.width) for y in range(0, out.height, 40)
               if out.getpixel((x, y))[0] > 120)
    # 16:9 로 가운데를 자르면 2000×500 → 888×500 이라 빨간 끝은 통째로 잘려나간다.
    assert reds == 0, "가장자리가 남아 있다 — 자르지 않고 늘린 것이다"


def test_깨진_바이트는_조용히_넘어가지_않는다():
    with pytest.raises(ImagePreprocessError):
        view_to_data_uri(b"not an image at all")


def test_같은_입력은_같은_바이트를_낸다():
    """⚠️ 판정 재현성의 전제. 인코딩이 흔들리면 temperature 0 도 소용없다."""
    raw = make_image()
    assert view_to_data_uri(raw)[0] == view_to_data_uri(raw)[0]


def test_equirect도_같은_출구를_쓴다():
    """파노라마 경로도 JPEG·고정크기 규칙을 우회하면 안 된다."""
    uri, _ = equirect_to_view(make_image(size=(2048, 1024), fmt="PNG"), heading=90.0)
    assert uri.startswith("data:image/jpeg;base64,")
    assert _decode(uri).size == TARGET_SIZE


def test_equirect는_방위에_따라_다른_그림을_낸다():
    """항상 같은 그림이 나오면 재투영이 죽은 것이다 — 그래도 예외는 안 난다."""
    src = Image.new("RGB", (1024, 512), (0, 0, 0))
    for x in range(0, 256):                    # 파노라마의 한 사분면만 밝게
        for y in range(512):
            src.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO(); src.save(buf, format="PNG")
    raw = buf.getvalue()
    assert equirect_to_view(raw, heading=0.0)[0] != equirect_to_view(raw, heading=180.0)[0]
