"""설정 로더 — 정본이 하나라는 것, 그리고 오타가 조용히 안 넘어간다는 것.

이 레포의 사고는 전부 **에러 없이** 일어났다. 설정 파일에서 그 형태는
`max_candidate: 8` 이라고 적어 놓고 8이 먹었다고 믿는 것이다. 그래서
여기서 지키는 것은 값이 아니라 **틀렸을 때 터진다는 사실**이다.
"""
import textwrap

import pytest

from trailwalk import settings
from trailwalk.explore import ExploreConfig
from trailwalk.walk import WalkConfig


def write(tmp_path, body: str):
    p = tmp_path / "t.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ── 정본 ───────────────────────────────────────────────────────────────────

def test_정본_파일이_실제로_읽힌다():
    """app/config/trailwalk.yaml 이 없거나 깨지면 import 부터 죽어야 한다."""
    assert settings.DEFAULT_PATH.exists()
    s = settings.load()
    assert s.run.provider in ("fixture", "kakao")
    assert s.budget.max_vlm_calls > 0


def test_기본값을_코드에_다시_적지_않는다():
    """Config dataclass 에 기본값이 있으면 정본이 둘이 된다 — 어느 쪽이 먹는지
    코드를 읽어야 알게 되고, 그게 이 리팩터링이 없앤 문제다."""
    for cls in (WalkConfig, ExploreConfig):
        with pytest.raises(TypeError):
            cls()          # 인자 없이 만들어지면 코드에 기본값이 있다는 뜻


def test_두_루프가_같은_설정에서_같은_값을_받는다():
    """max_candidates 가 walk 3 / explore 4 로 갈라져 있던 자리다."""
    s = settings.load()
    w, e = WalkConfig.from_settings(s), ExploreConfig.from_settings(s)
    assert w.max_candidates == e.max_candidates
    assert w.max_seconds == e.max_seconds
    assert w.snap_radius_m == e.snap_radius_m
    assert w.max_vlm_calls == e.max_vlm_calls   # 예산 축이 같다


# ── 조용히 틀리지 않는다 ───────────────────────────────────────────────────

def test_모르는_키는_터뜨린다(tmp_path):
    p = write(tmp_path, """
        candidates:
          max_candidate: 8
    """)
    with pytest.raises(settings.SettingsError) as e:
        settings.load(p)
    assert "max_candidate" in str(e.value)


def test_모르는_구획도_터뜨린다(tmp_path):
    p = write(tmp_path, """
        candidate:
          max_candidates: 8
    """)
    with pytest.raises(settings.SettingsError):
        settings.load(p)


def test_없는_파일은_경로를_알려준다(tmp_path):
    with pytest.raises(settings.SettingsError) as e:
        settings.load(tmp_path / "없다.yaml")
    assert "없다.yaml" in str(e.value)


def test_깨진_YAML은_파싱_실패로_터진다(tmp_path):
    p = write(tmp_path, """
        run:
          provider: [닫히지 않은
    """)
    with pytest.raises(settings.SettingsError):
        settings.load(p)


# ── 부분 지정 ──────────────────────────────────────────────────────────────

def test_적은_것만_덮어쓰고_나머지는_정본에서_온다(tmp_path):
    """커스텀 설정에는 **바꿀 것만** 적는다. 나머지는 코드가 아니라 정본이 준다."""
    base = settings.load()
    p = write(tmp_path, """
        budget:
          max_vlm_calls: 7
    """)
    s = settings.load(p)
    assert s.budget.max_vlm_calls == 7
    # 같은 구획의 다른 키도, 아예 안 적은 구획도 정본 값 그대로다
    assert s.budget.explore_max_depth == base.budget.explore_max_depth
    assert s.run.provider == base.run.provider


def test_섹션을_통째로_갈아치우지_않는다(tmp_path):
    """`budget:` 아래 한 줄만 바꾸려던 사용자가 나머지 예산을 잃으면 안 된다."""
    base = settings.load()
    p = write(tmp_path, """
        budget:
          max_seconds: 30.0
    """)
    s = settings.load(p)
    assert s.budget.max_seconds == 30.0
    assert s.budget.max_vlm_calls == base.budget.max_vlm_calls


def test_코드에는_기본값이_없다():
    """섹션 dataclass 에 기본값이 있으면 정본이 둘이 된다 — 정본 YAML 의 값을
    고쳐도 그 키를 생략한 커스텀 설정은 조용히 옛 값으로 돈다."""
    for cls in (settings.RunSettings, settings.BudgetSettings,
                settings.CandidateSettings, settings.VlmSettings,
                settings.ImageSettings, settings.KakaoSettings):
        with pytest.raises(TypeError):
            cls()


def test_빈_파일은_정본_그대로다(tmp_path):
    p = write(tmp_path, "")
    assert settings.load(p) == settings.load()


def test_좌표는_리스트로_적어도_튜플로_들어온다(tmp_path):
    """YAML 에는 튜플이 없다. lat, lng = st.run.start 가 성립해야 한다."""
    p = write(tmp_path, """
        run:
          start: [37.5, 127.0]
    """)
    s = settings.load(p)
    assert s.run.start == (37.5, 127.0)
    lat, lng = s.run.start
    assert (lat, lng) == (37.5, 127.0)


# ── 파생 값 ────────────────────────────────────────────────────────────────

def test_이미지_토큰_하한이_따라_움직인다(tmp_path):
    """예전엔 MIN_PROMPT_TOKENS=200 이 따로 적혀 있었고 EXPECTED_IMAGE_TOKENS 는
    아무도 안 읽는 죽은 상수였다 — 토큰 수를 바꿔도 하한이 안 따라왔다."""
    p = write(tmp_path, """
        image:
          expected_image_tokens: 400
    """)
    assert settings.load(p).image.min_prompt_tokens == 300   # 400 * 3 // 4


def test_화각과_이미지_크기가_한_값에서_나온다():
    """kakao 뷰포트와 imaging 목표 크기는 **같은 값**이어야 한다.

    어긋나면 imaging 이 리사이즈/크롭을 하게 되고, 그게 곧 화각 변화다.
    그런데 이미지 토큰 수는 그대로라 로그에 아무 흔적이 안 남는다 —
    화면이 좁아진 줄 모르고 판정만 나빠진다. 두 상수를 따로 두던 시절의
    실패 방식이라 여기서 못박는다.
    """
    from trailwalk.imaging import TARGET_SIZE
    from trailwalk.providers import kakao

    assert tuple(TARGET_SIZE) == (kakao.VIEW_W, kakao.VIEW_H)
    assert tuple(TARGET_SIZE) == tuple(settings.SETTINGS.image.target_size)


# ── 타입 ───────────────────────────────────────────────────────────────────
#
# 오타보다 잡기 어려운 실패다. 키 이름은 맞는데 값의 타입이 틀리면 로더는
# 통과시키고, 틀린 값이 런 내내 조용히 먹는다.

def test_따옴표_친_no는_bool이_아니라_문자열이다(tmp_path):
    """YAML 의 `"no"` 는 문자열이고 `if not probe_all:` 은 그걸 참으로 읽는다 —
    끄려던 옵션이 켜진 채로 런이 끝난다. 에러도 안 난다."""
    p = write(tmp_path, """
        candidates:
          probe_all: "no"
    """)
    with pytest.raises(settings.SettingsError) as e:
        settings.load(p)
    assert "probe_all" in str(e.value)


def test_bool_자리에_숫자를_넣으면_터진다(tmp_path):
    """파이썬에서 bool 은 int 의 하위형이라 검사 순서가 뒤집히면 1이 통과한다."""
    p = write(tmp_path, """
        candidates:
          expand_non_trail: 1
    """)
    with pytest.raises(settings.SettingsError):
        settings.load(p)


def test_정수_자리에_소수를_넣으면_터진다(tmp_path):
    p = write(tmp_path, """
        candidates:
          max_candidates: 4.5
    """)
    with pytest.raises(settings.SettingsError):
        settings.load(p)


def test_실수_자리의_정수는_받아준다(tmp_path):
    """`max_seconds: 900` 을 쓰지 말라고 할 이유가 없다. 900.0 으로 받는다."""
    p = write(tmp_path, """
        budget:
          max_seconds: 900
    """)
    s = settings.load(p)
    assert s.budget.max_seconds == 900.0
    assert isinstance(s.budget.max_seconds, float)


def test_좌표_원소가_모자라면_터진다(tmp_path):
    """`lat, lng = st.run.start` 가 ValueError 로 죽는 것보다 여기서 잡는 게 낫다."""
    p = write(tmp_path, """
        run:
          start: [37.5]
    """)
    with pytest.raises(settings.SettingsError) as e:
        settings.load(p)
    assert "2개" in str(e.value)


def test_null_을_허용하는_필드는_통과한다(tmp_path):
    p = write(tmp_path, """
        run:
          out: null
          dump: null
    """)
    s = settings.load(p)
    assert s.run.out is None and s.run.dump is None


# ── --config 가 실제로 먹는가 ──────────────────────────────────────────────
#
# 모듈 상수를 import 시점에 고정해 두면 --config 가 조용히 무시된다. 그러면
# kakao 뷰포트는 새 크기로 찍는데 imaging 은 옛 크기로 되크롭해 **화각만
# 좁아지고** 토큰 수는 그대로라, 로그 어디에도 흔적이 안 남는다.

def test_커스텀_설정의_이미지_크기가_인코딩에_먹는다(tmp_path):
    from conftest import make_image
    from trailwalk.imaging import view_to_data_uri

    p = write(tmp_path, """
        image:
          target_size: [640, 360]
    """)
    s = settings.load(p)
    uri, _ = view_to_data_uri(make_image((1280, 720)), s.image)
    assert _decoded_size(uri) == (640, 360)

    # 설정을 안 넘기면 정본 크기다 — 그래서 루프는 반드시 cfg.image 를 넘긴다
    uri, _ = view_to_data_uri(make_image((1280, 720)))
    assert _decoded_size(uri) == tuple(settings.SETTINGS.image.target_size)


def test_커스텀_설정의_토큰_하한이_vlm에_먹는다(tmp_path):
    """이미지 무시(WEBP) 탐지의 유일한 신호다. 옛 하한을 쓰면 못 잡는다."""
    from trailwalk.vlm import VlmClient

    p = write(tmp_path, """
        image:
          expected_image_tokens: 800
    """)
    s = settings.load(p)
    assert VlmClient(settings=s).min_prompt_tokens == 600      # 800 * 3 // 4
    assert VlmClient().min_prompt_tokens == settings.SETTINGS.image.min_prompt_tokens


def test_루프_설정이_이미지_규칙을_들고_다닌다():
    """WalkConfig/ExploreConfig 가 image 를 안 들고 있으면 probe() 가 모듈
    상수로 인코딩하게 되고, 그 순간 --config 가 무시된다."""
    s = settings.load()
    for cfg in (WalkConfig.from_settings(s), ExploreConfig.from_settings(s)):
        assert cfg.image is s.image


def _decoded_size(data_uri: str):
    import base64
    import io

    from PIL import Image
    head, b64 = data_uri.split(",", 1)
    assert head == "data:image/jpeg;base64"
    return Image.open(io.BytesIO(base64.b64decode(b64))).size


def test_생성자가_settings만_받아도_그걸_따른다(tmp_path):
    """인자 기본값에 모듈 상수를 박아 두면 `VlmClient(settings=custom)` 이 옛
    정본 URL 로 요청을 보낸다. 지금은 호출부가 url 을 매번 같이 넘겨 가려지지만,
    안 넘기는 호출이 하나 생기는 순간 조용히 틀린다."""
    from trailwalk.vlm import VlmClient

    p = write(tmp_path, """
        vlm:
          url: "http://127.0.0.1:9999/v1/chat/completions"
          schema: eval
          prompt_version: system_v1
    """)
    s = settings.load(p)
    c = VlmClient(settings=s)
    assert c.url == "http://127.0.0.1:9999/v1/chat/completions"
    assert c.schema_name == "eval"
    assert c.system_version == "system_v1"
    # 명시 인자는 여전히 이긴다
    assert VlmClient(url="http://x/y", settings=s).url == "http://x/y"
