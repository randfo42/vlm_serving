"""VLM 호출 — 조용한 실패를 잡는 부분만 집중적으로.

이 클라이언트의 존재 이유는 요청을 보내는 게 아니라 **성공 응답을 의심하는 것**이다.
HTTP 200 + 정상 JSON 이면서 완전히 틀린 응답이 실제로 나온다:

  · 이미지가 통째로 무시됨 (prompt_tokens 만이 잡아낸다)
  · system turn 오염으로 프리픽스 캐시 사망 (cached_tokens 만이 잡아낸다)
  · Metal OOM 좀비 — /health 는 200 을 주지만 거짓이다
"""
import io
import json
import threading
import urllib.error

import pytest

from trailwalk import prompt as P
from trailwalk.vlm import (
    ImageIgnoredError,
    ServerDeadError,
    Stats,
    VlmClient,
    VlmError,
)

URI = "data:image/jpeg;base64,AAAA"


def ok_payload(*, prompt_tokens=276, cached=200, content=None, finish="stop"):
    return {
        "choices": [{"finish_reason": finish,
                     "message": {"content": content or json.dumps(
                         {"camera_surface": "park_path"})}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 17,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    }


def http_error(code: int, body: bytes = b"boom"):
    return urllib.error.HTTPError("u", code, "msg", {}, io.BytesIO(body))


class FakePost:
    """_post 를 대신한다. 호출 기록을 남겨 요청 **내용**까지 검사할 수 있게."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r, 100.0


def client(*responses, **kw) -> tuple[VlmClient, FakePost]:
    c = VlmClient(**kw)
    fake = FakePost(*responses)
    c._post = fake
    return c, fake


# ── 이미지가 무시되는 사고 ──────────────────────────────────────────────────

def test_prompt_tokens가_낮으면_ImageIgnored():
    """⚠️ WEBP 사고. 서버는 200 을 주고 로그도 안 남긴다."""
    c, _ = client(ok_payload(prompt_tokens=12))
    with pytest.raises(ImageIgnoredError) as e:
        c.assess(URI)
    assert "12" in str(e.value)


def test_이미지_검사가_JSON_파싱보다_먼저다():
    """이미지가 무시되면 모델은 **그럴듯한 JSON** 을 지어낸다. 파싱은 성공하고
    값은 환각이다. 파싱을 먼저 하면 이 사고가 정상 결과로 통과한다.

    여기서는 파싱조차 불가능한 본문을 준다. 그런데도 ImageIgnoredError 가
    나와야 순서가 맞는 것이다 (파싱이 먼저면 JSONDecodeError 쪽으로 샌다).
    """
    c, _ = client(ok_payload(prompt_tokens=12, content="이건 JSON 이 아니다"))
    with pytest.raises(ImageIgnoredError):
        c.assess(URI)


# ── 프리픽스 캐시 ───────────────────────────────────────────────────────────

def test_system_turn이_호출마다_바이트_동일하다():
    """⚠️ 1바이트만 달라도 캐시 적중률이 0 이 된다. 에러는 안 나고 느려질 뿐이다."""
    c, fake = client(ok_payload())
    c.assess(URI, heading=10.0)
    c.assess(URI, heading=280.0)
    systems = [b["messages"][0]["content"] for b in fake.bodies]
    assert systems[0] == systems[1]
    assert systems[0] == P.load(c.system_version)


def test_가변값은_이미지_뒤_user_turn에만_들어간다():
    c, fake = client(ok_payload())
    c.assess(URI, heading=91.36)
    body = fake.bodies[0]
    assert "91" not in body["messages"][0]["content"]      # system 은 깨끗
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "image_url"                  # 이미지가 먼저
    assert "91" in parts[1]["text"]                         # 가변값은 뒤


def test_첫_호출의_캐시미스는_세지_않는다():
    """런의 첫 호출은 캐시가 비어 있는 게 정상이다. 그걸 경고로 세면 매 런마다
    경고가 뜨고, 사람은 곧 경고 자체를 무시하게 된다."""
    c, _ = client(ok_payload(cached=0))
    c.assess(URI)
    assert c.stats.cache_misses == 0
    c.assess(URI)
    assert c.stats.cache_misses == 1


def test_캐시정보가_null이어도_판정이_나온다():
    """vLLM 은 `prompt_tokens_details` 를 **`null` 로 명시해서** 보낸다.
    `.get(key, {})` 의 기본값은 키가 없을 때만 먹으므로 None 이 그대로 흘러
    `None.get()` 으로 터졌다 — llama.cpp 는 키를 아예 빼서 안 드러났다.
    서버가 캐시 정보를 안 주는 것은 판정을 못 쓸 이유가 아니다."""
    payload = ok_payload()
    payload["usage"]["prompt_tokens_details"] = None
    c, _ = client(payload)
    v = c.assess(URI)
    assert v.is_trail is True
    assert v.cached_tokens == 0



class _SpyLock:
    """잡혀 있는지 밖에서 볼 수 있는 락."""

    def __init__(self):
        self._l = threading.Lock()
        self.held = False

    def __enter__(self):
        self._l.acquire()
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._l.release()


class _GuardedStats(Stats):
    """필드가 바뀌는 **순간마다** 락이 잡혀 있는지 확인한다."""

    def __init__(self, spy):
        object.__setattr__(self, "spy", spy)
        with spy:                      # 초기화는 경쟁 대상이 아니다
            super().__init__()

    def __setattr__(self, name, value):
        assert self.spy.held, f"Stats.{name} 을 락 밖에서 바꿨다"
        object.__setattr__(self, name, value)


def test_카운터는_락_안에서만_바뀐다():
    """겹치기(→ vlm.max_inflight)를 켜면 **클라이언트 하나를 여러 스레드가**
    부른다. Stats 는 이 레포가 조용한 실패를 잡으려고 둔 유일한 계측이라,
    경쟁으로 몇 건 새면 그게 곧 잘못된 안심이 된다.

    ⚠️ 스레드를 여럿 돌려 "샜나" 를 보는 테스트는 **부적이다.** 실제로 그렇게
    짰더니 락을 빼도 통과했다 — 새는지 여부가 스케줄링에 달려 있어서다.
    그래서 불변식을 직접 못박는다: 카운터가 바뀌는 순간 락이 잡혀 있어야 한다.
    새 카운터를 락 밖에서 올리면 스레드 없이도 여기서 걸린다.
    """
    spy = _SpyLock()
    # 파싱 실패 1회 → 재시도 → 성공. _bump 와 _parse 의 갱신을 한 번에 지난다
    c, _ = client(ok_payload(content="{ 안 닫힌"), ok_payload())
    c._lock, c.stats = spy, _GuardedStats(spy)

    assert c.assess(URI).is_trail is True
    assert (c.stats.parse_failures, c.stats.retries, c.stats.calls) == (1, 1, 1)
    assert not spy.held


def test_usage에_캐시_키가_아예_없어도_판정이_나온다():
    payload = ok_payload()
    del payload["usage"]["prompt_tokens_details"]
    c, _ = client(payload)
    assert c.assess(URI).cached_tokens == 0


def test_temperature는_0이고_스키마가_강제된다():
    c, fake = client(ok_payload())
    c.assess(URI)
    body = fake.bodies[0]
    assert body["temperature"] == 0
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == P.SCHEMAS["surface"]


def test_스키마_선택이_요청에_반영된다():
    c, fake = client(ok_payload(content=json.dumps({"is_trail": True, "confidence": 9})),
                     schema_name="eval", system_version="system_v3")
    v = c.assess(URI)
    assert v.confidence == 9
    assert fake.bodies[0]["response_format"]["json_schema"]["schema"] == P.SCHEMAS["eval"]


def test_프롬프트_버전을_고를_수_있다():
    c, fake = client(ok_payload(content=json.dumps({"is_trail": True})),
                     system_version="system_v1", schema_name="walk")
    c.assess(URI)
    assert fake.bodies[0]["messages"][0]["content"] == P.load("system_v1")


# ── is_trail 은 v4 에서 서버가 아니라 설정이 정한다 ─────────────────────────

def test_범주가_산책로인지는_설정이_정한다(monkeypatch):
    """v4 는 is_trail 을 아예 내지 않는다. 경계는 `vlm.trail_surfaces` 다 —
    프롬프트를 안 바꾸고 경계만 옮길 수 있어야 하는 것이 이 설계의 요점이다."""
    for surface, want in [("park_path", True), ("waterside", True),
                          ("shared_alley", False), ("roadway", False),
                          ("pedestrian_way", False),   # 정본에서 뺐다
                          ("sidewalk", False)]:
        c, _ = client(ok_payload(content=json.dumps({"camera_surface": surface})))
        v = c.assess(URI)
        assert v.is_trail is want, f"{surface} 가 {v.is_trail}"
        assert v.camera_surface == surface, "원본 범주를 안 남기면 재해석 불가"


def test_경계를_옮기면_같은_응답의_판정이_바뀐다():
    """같은 camera_surface 를 다시 판정받지 않고도 A/B 할 수 있어야 한다."""
    body = json.dumps({"camera_surface": "pedestrian_way"})
    strict, _ = client(ok_payload(content=body))
    assert strict.assess(URI).is_trail is False
    loose, _ = client(ok_payload(content=body))
    loose.trail_surfaces = loose.trail_surfaces | {"pedestrian_way"}
    assert loose.assess(URI).is_trail is True


def test_모르는_범주는_설정_로드에서_터진다():
    """오타 하나가 그 범주를 통째로 False 로 만들고 에러는 안 난다 —
    리포트에서는 모델이 못 맞힌 것처럼 보인다."""
    import dataclasses

    from trailwalk import settings as S
    st = S.SETTINGS
    bad = dataclasses.replace(st, vlm=dataclasses.replace(
        st.vlm, trail_surfaces=["park_paths"]))
    with pytest.raises(ValueError, match="park_paths"):
        VlmClient(settings=bad)


def test_프롬프트와_스키마의_짝이_안_맞으면_터진다():
    """짝이 안 맞으면 HTTP 200 에 파싱도 성공하고 값만 환각이다."""
    with pytest.raises(ValueError, match="짝이 아니다"):
        VlmClient(system_version="system_v4", schema_name="walk")
    with pytest.raises(ValueError, match="짝이 아니다"):
        VlmClient(system_version="system_v3", schema_name="surface")


# ── 서버 장애 ───────────────────────────────────────────────────────────────

def test_500이_세_번_연속이면_포기한다():
    """⚠️ Metal OOM 좀비는 **자체 복구되지 않는다.** 계속 두드려봐야 실패 로그만
    쌓인다. 사람을 불러야 하는 상태라는 걸 예외로 말해야 한다."""
    c, _ = client(http_error(500))
    with pytest.raises(ServerDeadError) as e:
        c.assess(URI)
    assert "재시작" in str(e.value)


def test_500이_두_번이면_아직_재시도한다():
    c, _ = client(http_error(500), http_error(500), ok_payload())
    assert c.assess(URI).is_trail is True
    assert c.stats.retries == 2


def test_성공하면_500_연속이_끊긴다():
    """연속이어야 좀비다. 간헐적 500 을 좀비로 오인하면 멀쩡한 런이 중단된다."""
    c, _ = client(http_error(500), ok_payload(), http_error(500), http_error(500),
                  ok_payload())
    c.assess(URI)
    c.assess(URI)                     # 여기서 ServerDeadError 가 나면 안 된다
    assert c.stats.calls == 2


def test_400은_재시도하지_않는다():
    """깨진 이미지다. 같은 바이트로 다시 물어도 같은 답이다."""
    c, fake = client(http_error(400, b"bad image"))
    with pytest.raises(VlmError) as e:
        c.assess(URI)
    assert not isinstance(e.value, ServerDeadError)
    assert len(fake.bodies) == 1


def test_503은_재시도한다():
    c, _ = client(http_error(503), ok_payload())
    assert c.assess(URI).is_trail is True
    assert c.stats.retries == 1


# ── 응답 이상 ───────────────────────────────────────────────────────────────

def test_잘린_출력은_실패로_본다():
    """finish_reason 이 stop 이 아니면 JSON 이 반쪽이다. thinking 모드 의심."""
    c, _ = client(ok_payload(finish="length"))
    with pytest.raises(VlmError) as e:
        c.assess(URI)
    assert "thinking" in str(e.value)


def test_JSON_파싱_실패는_재시도하고_결국_실패한다():
    c, _ = client(ok_payload(content="{{{"))
    with pytest.raises(VlmError):
        c.assess(URI)
    assert c.stats.parse_failures >= 1


def test_warmup은_결과도_예외도_삼킨다():
    """콜드 스타트를 녹이는 게 목적이다. 여기서 죽으면 런이 시작도 못 한다."""
    c, _ = client(http_error(500))
    c.warmup(URI)                      # 예외가 새어나오면 실패
    assert c.stats.calls == 0          # 측정에도 섞이지 않는다
