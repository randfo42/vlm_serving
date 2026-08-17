"""비밀값 취급.

규칙 하나다: **이 모듈의 어떤 경로로도 키 값이 밖으로 나가지 않는다.**
예외 메시지는 스택트레이스에 남고 스택트레이스는 어디로든 간다.

실제 사고: Playwright 의 requestfailed 핸들러가 SDK URL 을 통째로 찍었고
거기에 `?appkey=...` 가 붙어 있었다. 진단 출력에도 같은 함정이 있다.
"""
import pytest

from trailwalk import config

FAKE = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """실제 app/.env 를 읽지 않는다. 테스트가 사람의 키에 의존하면 안 된다."""
    for name in (*config.KAKAO_KEY_NAMES, "KAKAO_MAP_REST_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")


def test_키가_없을_때_안내에_값이_없다():
    with pytest.raises(RuntimeError) as e:
        config.kakao_appkey()
    msg = str(e.value)
    assert FAKE not in msg
    assert "KAKAO_MAP_JS_API_KEY" in msg          # 이름은 알려준다


def test_REST_키만_있으면_종류가_틀렸다고_말한다(monkeypatch):
    """⚠️ 둘 다 32자 16진수라 값으로는 구별되지 않는다. 잘못 넣으면 SDK 가 조용히
    실패하고 증상은 '로드뷰가 안 뜬다' 뿐이라 커버리지 문제로 오인하기 딱 좋다."""
    monkeypatch.setenv("KAKAO_MAP_REST_API_KEY", FAKE)
    with pytest.raises(RuntimeError) as e:
        config.kakao_appkey()
    msg = str(e.value)
    assert "JavaScript" in msg
    assert FAKE not in msg, "안내에 키 값이 섞였다"


def test_JS_키가_있으면_돌려준다(monkeypatch):
    monkeypatch.setenv("KAKAO_MAP_JS_API_KEY", FAKE)
    assert config.kakao_appkey() == FAKE


def test_REST_키가_같이_있어도_JS_키를_고른다(monkeypatch):
    monkeypatch.setenv("KAKAO_MAP_REST_API_KEY", "rest" * 8)
    monkeypatch.setenv("KAKAO_MAP_JS_API_KEY", FAKE)
    assert config.kakao_appkey() == FAKE


def test_REST_키_이름은_후보에_없다():
    """이름으로 갈라놓는 것이 두 키의 혼동을 막는 유일한 수단이다."""
    assert "KAKAO_MAP_REST_API_KEY" not in config.KAKAO_KEY_NAMES


# ── .env 파싱 ──────────────────────────────────────────────────────────────

def test_쉘_환경변수가_파일보다_우선한다(monkeypatch, tmp_path):
    """일회성 실험(`KAKAO_JS_KEY=other python ...`)이 가능해야 한다."""
    env = tmp_path / ".env"
    env.write_text("KAKAO_MAP_JS_API_KEY=from_file\n")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setenv("KAKAO_MAP_JS_API_KEY", "from_shell")
    config.load_env()
    assert config.kakao_appkey() == "from_shell"


def test_따옴표와_export와_주석을_처리한다(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 주석\n"
        "\n"
        'export KAKAO_MAP_JS_API_KEY="quoted"\n'
        "OTHER='single'\n")
    monkeypatch.setattr(config, "ENV_FILE", env)
    config.load_env()
    assert config.kakao_appkey() == "quoted"
    import os
    assert os.environ["OTHER"] == "single"


def test_파일이_없어도_죽지_않는다(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "없음")
    assert config.load_env() == 0


def test_진단_출력에_키가_마스킹된다(monkeypatch):
    """diagnose_sdk 는 Kakao 응답 본문을 그대로 사람에게 보여준다.
    본문에 appkey 가 되비쳐 오는 경우가 있어서 마스킹이 필요하다."""
    from trailwalk.providers import kakao

    class Resp:
        status = 401

        def read(self, _n=None):
            return f'{{"message": "domain mismatched! caller={FAKE}"}}'.encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(kakao.urllib.request, "urlopen", lambda *a, **k: Resp())
    out = kakao.diagnose_sdk(FAKE, "http://127.0.0.1:8731")
    assert FAKE not in out
    assert "<KEY>" in out
