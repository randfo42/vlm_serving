"""이미지 전처리. 서빙 쪽 규칙(docs/10-client-guide.md §2)을 코드로 못박은 곳.

서버에 나가는 바이트는 **반드시 여기를 통과한다.** 규칙을 어기면 조용히 실패하기
때문이다 — WEBP 를 보내면 HTTP 200 이 오고 서버 로그도 깨끗한데 모델은 이미지를
못 본다. 다른 경로로 data URI 를 만들지 말 것.

두 종류의 입력이 있다.

  (a) 렌더된 뷰포트  — Kakao/Naver SDK 를 headless 브라우저로 몰아 찍은 스크린샷.
                      이미 한 화각이므로 리사이즈만 한다. → `view_to_data_uri`
  (b) 원본 파노라마  — 등장방형(equirectangular) 360° 이미지.
                      진행 방향 기준 한 화각으로 잘라야 한다. → `equirect_to_view`

(b) 를 통째로 보내면 안 된다. 280 토큰에 360° 를 욱여넣으면 아무것도 안 보인다.
"""
import base64
import io

from PIL import Image

# 16:9 고정. 긴 변이 리사이즈 목표(1056)보다 커서 서버가 다운스케일한다.
# → 입력 크기와 무관하게 이미지 토큰이 264 로 상수가 된다 (10-client-guide.md §2.3).
TARGET_SIZE = (1280, 720)
EXPECTED_IMAGE_TOKENS = 264

# prompt_tokens 하한. 이미지가 무시되면(WEBP 사고) 텍스트 분량만 잡혀 수십 토큰이 된다.
# 이미지 토큰 264 의 3/4 만 넘겨도 "이미지가 들어갔다" 는 확실하다.
MIN_PROMPT_TOKENS = 200

JPEG_QUALITY = 90


class ImagePreprocessError(RuntimeError):
    pass


def _load(raw: bytes) -> tuple[Image.Image, str]:
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "?").upper()
        img.load()
    except Exception as e:
        raise ImagePreprocessError(f"이미지 디코드 실패: {type(e).__name__}: {e}") from e
    return img.convert("RGB"), fmt


def _encode(img: Image.Image) -> str:
    """항상 JPEG. 항상 TARGET_SIZE. 여기가 유일한 출구다.

    종횡비가 다르면 **가운데를 잘라** 맞춘다. 늘려 맞추면 토큰 수는 같지만 장면이
    일그러져 "길이 어느 쪽으로 이어지는가" 가 왜곡된다. 잘라내면 화각이 좁아질 뿐
    보이는 것은 정직하다.
    """
    if img.size != TARGET_SIZE:
        w, h = img.size
        want = TARGET_SIZE[0] / TARGET_SIZE[1]
        if abs(w / h - want) > 1e-3:
            cw, ch = (round(h * want), h) if w / h > want else (w, round(w / want))
            img = img.crop(((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch))
        img = img.resize(TARGET_SIZE, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def view_to_data_uri(raw: bytes) -> tuple[str, str]:
    """이미 한 화각인 이미지 → data URI. (uri, 원본포맷) 을 돌려준다.

    원본 포맷을 같이 돌려주는 이유: provider 가 WEBP 를 주기 시작하면 런로그에
    그 사실이 남아야 한다. 변환 자체는 여기서 항상 되므로 서버가 깨지진 않지만,
    provider 가 바뀌었다는 신호는 놓치면 안 된다.
    """
    img, fmt = _load(raw)
    return _encode(img), fmt


def equirect_to_view(raw: bytes, heading: float, fov_deg: float = 90.0,
                     pitch_deg: float = 0.0) -> tuple[str, str]:
    """등장방형 360° 파노라마에서 heading 방향 한 화각을 잘라 data URI 로.

    단순 사각 크롭이 아니라 gnomonic(직선 보존) 재투영이다. 사각 크롭은 화각이
    커질수록 수평선이 휘어 "길이 어디로 이어지는가" 판단을 망친다.

    numpy 가 필요하다. 렌더된 뷰포트만 쓰는 provider(Kakao/Naver SDK 경로)에서는
    호출되지 않으므로 numpy 는 선택 의존성으로 둔다.
    """
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover - 설치 안내가 목적
        raise ImagePreprocessError(
            "equirect_to_view 에는 numpy 가 필요하다: pip install -r app/requirements.txt") from e

    src, fmt = _load(raw)
    sw, sh = src.size
    W, H = TARGET_SIZE

    # 이미지 평면 좌표 → 카메라 좌표. f 는 수평 화각으로 정한 초점거리(픽셀).
    f = (W / 2) / np.tan(np.radians(fov_deg) / 2)
    u = (np.arange(W) - W / 2)[None, :]
    v = (np.arange(H) - H / 2)[:, None]
    x = np.broadcast_to(u, (H, W)).astype(np.float64)
    y = np.broadcast_to(v, (H, W)).astype(np.float64)
    z = np.full((H, W), f)

    # pitch(위아래) 회전. heading 은 아래에서 경도 오프셋으로 처리한다.
    p = np.radians(pitch_deg)
    y2 = y * np.cos(p) - z * np.sin(p)
    z2 = y * np.sin(p) + z * np.cos(p)

    r = np.sqrt(x * x + y2 * y2 + z2 * z2)
    lon = np.arctan2(x, z2) + np.radians(heading)   # 0 = 파노라마 중앙 = 정북 가정
    lat = np.arcsin(y2 / r)

    # 등장방형 샘플 좌표. 경도는 순환하므로 wrap, 위도는 clip.
    sx = ((lon / (2 * np.pi) + 0.5) % 1.0) * sw
    sy = (lat / np.pi + 0.5) * sh
    xi = np.clip(sx.astype(np.int64), 0, sw - 1)
    yi = np.clip(sy.astype(np.int64), 0, sh - 1)

    out = np.asarray(src)[yi, xi]
    return _encode(Image.fromarray(out)), fmt
