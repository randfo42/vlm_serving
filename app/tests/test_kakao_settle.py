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


def test_안정화_실패는_이벤트로도_나간다():
    """카운터만 있던 시절엔 아무도 안 읽었다 — 실측 사고(반쯤 로드된 프레임이
    판정을 뒤집음)가 재발해도 런로그가 정상으로 보였다. 런 스크립트가
    on_event 에 RunLog.event 를 꽂으면 probe 옆에 시간순으로 남는다."""
    events = []
    page = FakePage([str(i) for i in range(50)])
    p = provider(page)
    p.on_event = lambda kind, **kw: events.append((kind, kw))
    p._settle("PANO1")
    assert ("render_unsettled", {"pano_id": "PANO1"}) in events


def test_on_event_가_없으면_카운터만_남는다():
    """테스트·스크립트 밖 사용에서 배선이 없어도 죽으면 안 된다."""
    page = FakePage([str(i) for i in range(50)])
    p = provider(page)
    p._settle("PANO1")            # on_event 는 클래스 기본값 None
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
    """Kakao 가 계속 뭔가를 받아오는 상황에서 런이 멈추면 안 된다.
    다만 조용히는 아니다 — 카운터와 이벤트로 남는다."""
    monkeypatch.setattr(kakao, "TILE_WAIT_MAX_MS", 60)
    events = []
    page = FakePage(["A"])
    p = provider(page)
    p.on_event = lambda kind, **kw: events.append(kind)
    p._inflight = 1               # 영원히 안 끝난다
    p._settle("PANO1")            # 그래도 돌아와야 한다
    assert p._tile_timeouts == 1
    assert "tiles_timeout" in events


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


# ── 이웃 응답 파싱 실패 ─────────────────────────────────────────────────────

class FakeResp:
    url = kakao.NODE_API_MARK

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_노드_응답_형식이_바뀌면_neighbors_가_터진다():
    """조용히 버리면 실재하는 갈래가 그래프에서 증발한 채 탐색이 계속 돈다 —
    spot 하나의 필드 결손도 형식 변경으로 취급해 시끄럽게 죽는다."""
    p = provider(FakePage(["A"]))
    p._spots = {}
    p._sniff_node(FakeResp({"street_view": {"street": {
        "id": "1", "spot": [{"id": "2"}]}}}))          # pan/wgsx/wgsy 없음
    with pytest.raises(kakao.ProviderError):
        p.neighbors(Pano(pano_id="1", lat=0, lng=0))


def test_정상_노드_응답은_이웃이_된다():
    p = provider(FakePage(["A"]))
    p._spots = {}
    p._sniff_node(FakeResp({"street_view": {"street": {
        "id": "1", "spot": [{"id": "2", "pan": 91.4, "wgsy": 37.5, "wgsx": 127.0,
                             "st_name": "청계천로"}]}}}))
    nbrs = p.neighbors(Pano(pano_id="1", lat=0, lng=0))
    assert [(n.pano_id, n.heading) for n in nbrs] == [("2", 91.4)]


def test_이미_받아둔_이웃은_한_번도_기다리지_않는다():
    """대부분의 노드가 이 경로다. 여기에 대기가 붙으면 런 전체에 곱해진다."""
    page = FakePage(["A"])
    p = provider(page)
    p._spots = {}
    p._sniff_node(FakeResp({"street_view": {"street": {
        "id": "1", "spot": [{"id": "2", "pan": 91.4, "wgsy": 37.5, "wgsx": 127.0}]}}}))
    p.neighbors(Pano(pano_id="1", lat=0, lng=0))
    assert page.waits == [], f"캐시 히트인데 기다렸다: {page.waits}"


def test_이웃을_기다리는_자리는_사후_폴링_하나뿐이다():
    """⚠️ 사전 폴링(`range(6)`, 최대 600ms)을 되살리면 여기서 터진다.

    BFS 로 새로 발견한 pano 는 **띄운 적이 없어서** 노드 응답이 지나간 적이
    없다 — SDK 는 자기가 표시한 pano 의 JSON 만 받아온다. 그래서 그 사전
    폴링은 성공할 수 없는 대기였고, 노드마다 611ms 를 통째로 소진했다
    (2026-08-23 약수역사거리 실측: 노드 11개가 전부 끝까지 돌았다).

    사후 폴링이 같은 경우를 이미 덮으므로 지워도 잃는 것이 없다.
    """
    calls = []
    page = FakePage(["A"])
    page.evaluate = lambda js, *a, **k: calls.append(js)
    p = provider(page)
    p._spots = {}                       # 응답이 영영 안 온다 — 폴링이 소진된다
    assert p.neighbors(Pano(pano_id="1", lat=0, lng=0)) == []
    tries = kakao.PANO_WAIT_MS // kakao.PANO_POLL_MS
    assert len(page.waits) == tries, f"사전 폴링이 돌아왔다 ({len(page.waits)}회 대기)"
    assert set(page.waits) == {kakao.PANO_POLL_MS}


def test_노드_JSON_대기창이_show_가_얹어주던_만큼은_된다():
    """⚠️ `__goto` 는 즉시 반환하므로 `__show` 가 얹어 주던 대기(전환 완료까지,
    JS deadline 12초)가 사라졌다. 전환하려면 노드 JSON 이 와 있어야 하므로
    그 시간이 곧 대기창이었다.

    짧게 줄이면 느린 회선에서 **있는 갈래를 없다고 한다.** neighbors_missing
    으로 집계는 되지만, 같은 그래프가 머신·회선 상태에 따라 다르게 나오면
    재현성이 깨진다 — 이 레포가 프레임 안정화에 들인 노력과 같은 종류의
    문제다. 빠를 때는 비용이 0 이라(오는 즉시 빠져나온다) 줄일 이유가 없다.
    """
    assert kakao.PANO_WAIT_MS >= 12_000, \
        "__show 가 얹어 주던 12초보다 짧으면 대기창이 줄어든 것이다"
    # JS 쪽 deadline 도 같은 상수에서 온다 — 정본이 둘이면 언젠가 갈라진다
    assert f"Date.now() + {kakao.PANO_WAIT_MS}" in kakao.build_page("KEY")
    assert "{{PANO_WAIT_MS}}" not in kakao.build_page("KEY"), "치환이 안 됐다"


@pytest.mark.parametrize("wait,poll", [(12_000, 0), (12_000, -1), (50, 100)])
def test_대기창이_말이_안_되면_provider_를_안_만든다(wait, poll):
    """설정으로 뺀 값이라 이제 사람이 0 을 줄 수 있다. 0 이면 폴링 횟수
    계산이 ZeroDivisionError 로 죽고, 음수면 대기가 통째로 사라져 **있는
    갈래를 없다고 하면서 에러는 안 난다** — 뒤쪽이 더 나쁘다."""
    from dataclasses import replace

    from trailwalk import settings
    s = settings.load()
    bad = replace(s, kakao=replace(s.kakao, pano_wait_ms=wait, pano_poll_ms=poll))
    with pytest.raises(kakao.ProviderError, match="pano_poll_ms"):
        kakao.KakaoProvider(appkey="k", settings=bad)


def test_이웃은_좌표를_기다리는_show_를_쓰지_않는다():
    """`__show` 의 400ms 는 getPosition 을 유효하게 만들려는 대기인데,
    neighbors 는 그 반환값을 쓰지 않는다 — 노드 JSON 만 오면 된다."""
    calls = []
    page = FakePage(["A"])
    page.evaluate = lambda js, *a, **k: calls.append(js)
    p = provider(page)
    p._spots = {}
    p.neighbors(Pano(pano_id="1", lat=0, lng=0))
    assert calls, "pano 를 띄우지도 않았다"
    assert all("__goto" in js and "__show" not in js for js in calls), calls


def test_페이지가_goto_를_정의한다():
    """위 두 테스트는 가짜 page 라 JS 존재를 확인하지 못한다."""
    assert "window.__goto" in kakao.build_page("KEY")


def test_노드_아닌_응답은_무시한다():
    p = provider(FakePage(["A"]))
    p._spots = {}

    class Other:
        url = "https://example.com/other"

        def json(self):
            raise AssertionError("열어보지도 말아야 한다")

    p._sniff_node(Other())
    assert p._spots == {} and p._sniff_error is None


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
