"""프레임 안정화 — 이 레포에서 가장 비싸게 배운 버그.

### 무슨 일이 있었나

같은 시작점에서 두 번 걸었는데 경로가 갈렸다. temperature 는 0 이다.
원인을 갈라보니 **모델은 결정적이고 렌더가 비결정적**이었다. pano 전환 직후
스크린샷이 타일이 덜 붙은 프레임이었고, 그 반쯤 로드된 그림이 반대 판정을 받아
탐색이 다른 길로 샜다.

1차 수정: "연속 두 프레임이 같아질 때까지 찍는다." 두 런이 일치해서 고쳤다고
판단했다. **틀렸다.** 같은 pano 를 5번 찍으니 PNG 가 4가지로 나왔고, 5번 다
그 검사를 통과한 상태였다. 타일이 저해상도→고해상도로 덮어쓰는 중간에 잠깐
멎는 구간이 있어서 거기서 두 프레임이 일치해버린다.

2차 수정(현재): 타일 요청이 끊길 때까지 기다리고(`_await_tiles`), 그다음
연속 3프레임을 본다. 세션의 첫 캡처는 찍고 버린다.

브라우저 없이 검증한다 — `_settle` 과 `_await_tiles` 는 페이지 객체의 좁은
인터페이스만 쓰므로 가짜 페이지로 갈아끼울 수 있다.
"""
import pytest

from trailwalk.providers import kakao
from trailwalk.providers.base import Pano


class FakePage:
    """screenshot 이 정해진 프레임 시퀀스를 순서대로 돌려준다."""

    def __init__(self, frames, on_wait=None):
        self.frames = list(frames)
        self.shots = 0
        self.waits = []
        self._on_wait = on_wait

    def locator(self, _sel):
        return self

    def screenshot(self, **_kw):
        f = self.frames[min(self.shots, len(self.frames) - 1)]
        self.shots += 1
        return f if isinstance(f, bytes) else str(f).encode()

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        if self._on_wait:
            self._on_wait(self)

    def evaluate(self, *_a, **_k):
        return None


def provider(page) -> kakao.KakaoProvider:
    """__init__ 을 건너뛴다 — 브라우저도 Kakao 앱키도 필요 없다."""
    p = kakao.KakaoProvider.__new__(kakao.KakaoProvider)
    p._page = page
    p._unsettled = 0
    p._warmed = True          # 개별 테스트에서 필요하면 False 로 되돌린다
    p._inflight = 0
    p._last_net = 0.0
    return p


# ── 중간 정체 구간 ──────────────────────────────────────────────────────────

def test_중간에_두_프레임이_같아도_속지_않는다():
    """⚠️ 1차 수정이 뚫린 지점. A,A 에서 멈추면 반쯤 로드된 그림을 보낸다."""
    page = FakePage(["A", "A", "B", "B", "B"])
    assert provider(page)._settle() == b"B"


def test_연속_세_프레임이_같아야_안정이다():
    page = FakePage(["A", "B", "C", "C", "C"])
    assert provider(page)._settle() == b"C"
    assert page.shots == 5


def test_이미_안정된_화면은_빨리_끝난다():
    """모든 pano 에 비용을 물리면 안 된다. 실측상 대부분은 즉시 안정이다."""
    page = FakePage(["S"])
    p = provider(page)
    assert p._settle() == b"S"
    assert page.shots == 3        # 최초 + 연속 2회 확인
    assert p._unsettled == 0


def test_끝내_안_멎으면_기록을_남긴다():
    """조용히 넘기지 않는다. 이 카운터가 잦으면 대기 상수를 올려야 한다는 신호다."""
    page = FakePage([str(i) for i in range(50)])   # 매번 다른 프레임
    p = provider(page)
    p._settle()
    assert p._unsettled == 1


def test_무한정_기다리지_않는다():
    page = FakePage([str(i) for i in range(50)])
    provider(page)._settle()
    assert page.shots <= kakao.RENDER_SETTLE_TRIES + 2


# ── 타일 로딩 대기 ──────────────────────────────────────────────────────────

def test_타일이_도는_동안_기다린다():
    """화면이 '안 변한다' 는 것만으로는 다 붙었다고 할 수 없다.
    중간 단계에서 잠깐 멎었다가 다음 타일이 도착해 다시 바뀐다."""
    def drain(page):
        if len(page.waits) >= 3:
            page._p._inflight = 0

    page = FakePage(["A"], on_wait=drain)
    p = provider(page)
    page._p = p
    p._inflight = 4
    p._await_tiles()
    assert p._inflight == 0
    assert len(page.waits) >= 3, "요청이 도는데 기다리지 않았다"


def test_타일이_끝내_안_끊겨도_포기한다(monkeypatch):
    """Kakao 가 계속 뭔가를 받아오는 상황에서 런이 멈추면 안 된다."""
    monkeypatch.setattr(kakao, "TILE_WAIT_MAX_MS", 60)
    page = FakePage(["A"])
    p = provider(page)
    p._inflight = 1               # 영원히 안 끝난다
    p._await_tiles()              # 그래도 돌아와야 한다


def test_타일_추적은_이미지_요청만_센다():
    """SDK 는 이미지 말고도 계속 뭔가를 부른다. 전부 세면 영영 조용해지지 않는다."""
    p = provider(FakePage(["A"]))

    class Req:
        def __init__(self, t):
            self.resource_type = t

    p._net_start(Req("xhr"))
    p._net_start(Req("script"))
    assert p._inflight == 0
    p._net_start(Req("image"))
    assert p._inflight == 1
    p._net_end(Req("image"))
    assert p._inflight == 0


def test_카운터가_음수로_안_내려간다():
    """페이지 이동 등으로 끝 이벤트만 오는 경우가 있다. 음수가 되면 _await_tiles 가
    영원히 통과해버려 안정화가 통째로 무력화된다."""
    p = provider(FakePage(["A"]))

    class Req:
        resource_type = "image"

    for _ in range(3):
        p._net_end(Req())
    assert p._inflight == 0


# ── 세션 첫 캡처 ────────────────────────────────────────────────────────────

def test_세션의_첫_캡처는_버린다():
    """⚠️ 2차 수정. 브라우저·SwiftShader·HTTP 캐시가 전부 찬 상태의 첫 렌더는
    같은 pano 인데도 다른 바이트가 나왔다 (3.0s vs 1.15s). 원래 판정이
    뒤집혔던 s0 가 정확히 그 런의 첫 캡처였다."""
    page = FakePage(["COLD", "COLD", "COLD", "WARM", "WARM", "WARM"])
    p = provider(page)
    p._warmed = False
    assert p.capture(Pano(pano_id="X", lat=0, lng=0), 90.0) == b"WARM"


def test_두_번째_캡처부터는_버리지_않는다():
    page = FakePage(["A"])
    p = provider(page)
    p._warmed = False
    p.capture(Pano(pano_id="X", lat=0, lng=0), 90.0)
    before = page.shots
    p.capture(Pano(pano_id="Y", lat=0, lng=0), 90.0)
    assert page.shots - before == 3, "매 캡처마다 워밍업을 돌고 있다"


def test_렌더_실패는_ProviderError로_나간다():
    """탐색 루프가 이걸 잡아 '캡처 실패' 로 기록한다. 다른 예외면 런이 죽는다."""
    page = FakePage(["A"])

    def boom(*_a, **_k):
        raise RuntimeError("WebGL 죽음")

    page.evaluate = boom
    with pytest.raises(kakao.ProviderError):
        provider(page).capture(Pano(pano_id="X", lat=0, lng=0), 90.0)


# ── 안정화 상수 ─────────────────────────────────────────────────────────────

def test_상수가_실측_근거를_벗어나지_않는다():
    """연속 2프레임으로 되돌리면 이 파일 맨 위의 버그가 그대로 돌아온다."""
    assert kakao.RENDER_SETTLE_STABLE >= 3
    assert kakao.TILE_QUIET_MS >= 200


# ── 종료 ────────────────────────────────────────────────────────────────────

def test_close_는_리스닝_소켓까지_닫는다():
    """shutdown() 만으로는 포트가 안 풀린다 — 한 프로세스에서 두 번 못 연다.

    check_fov.py 가 화살표 유무로 세션을 두 번 여는데 거기서 실제로 터졌다
    (OSError: Address already in use). shutdown() 은 serve_forever 루프만
    멈추고 리스닝 소켓은 그대로 열어둔다.
    """
    called = []

    class FakeHttpd:
        def shutdown(self): called.append("shutdown")
        def server_close(self): called.append("server_close")

    p = kakao.KakaoProvider.__new__(kakao.KakaoProvider)
    p._browser = type("B", (), {"close": lambda self: called.append("browser")})()
    p._pw = type("P", (), {"stop": lambda self: called.append("pw")})()
    p._httpd = FakeHttpd()
    p.close()

    assert "server_close" in called, "리스닝 소켓이 안 닫힌다 — 포트가 물린 채 남는다"
    assert called.index("shutdown") < called.index("server_close")


def test_close_는_하나가_실패해도_나머지를_닫는다():
    called = []

    class FakeHttpd:
        def shutdown(self): called.append("shutdown")
        def server_close(self): called.append("server_close")

    def boom(self):
        raise RuntimeError("브라우저가 이미 죽었다")

    p = kakao.KakaoProvider.__new__(kakao.KakaoProvider)
    p._browser = type("B", (), {"close": boom})()
    p._pw = type("P", (), {"stop": boom})()
    p._httpd = FakeHttpd()
    p.close()

    assert called == ["shutdown", "server_close"], "브라우저가 죽으면 포트가 물린 채 남는다"
