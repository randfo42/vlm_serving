"""Kakao 로드뷰 provider — headless 브라우저로 JS SDK 를 몬다.

실주행 확인됨 (2026-08-17, 청계천 20스텝). 설정에서 걸리기 쉬운 것들은
docs/23-open-questions.md §1 에 정리해뒀다.

### 왜 브라우저인가

Kakao 로드뷰에는 서버사이드 REST 가 없다. 파노라마 이미지를 좌표+방위로 돌려주는
엔드포인트가 문서 어디에도 없고, 로드뷰는 `kakao.maps.Roadview` 가 브라우저에서
WebGL 로 그리는 것만 존재한다. Naver 도 같고, Google 은 REST 가 있지만 2021년에
한국 실외 이미지를 대부분 내렸다. → docs/21-roadview-providers.md

그래서 남는 길이 "브라우저를 띄워 SDK 에게 그리게 하고 그 화면을 찍는다" 하나다.

### 왜 로컬 HTTP 서버인가

Kakao 앱키는 **도메인 등록제**다. `file://` 로 열면 SDK 가 거부한다. 그래서
127.0.0.1 에 한 페이지짜리 서버를 띄우고, 그 origin 을 Kakao 개발자 콘솔의
플랫폼 > Web 사이트 도메인에 등록해야 한다.

### 주의

- WebGL 은 완전 headless 에서 안 그려지는 경우가 있다. 검게 찍히면
  `headless=False` 로 두고 확인할 것. 이게 첫 번째 의심 대상이다.
- 화면의 방향 화살표는 기본적으로 **남긴다**. 인접 pano 가 어느 쪽에 있는지를
  말해주는 정보이고, 그건 API 로는 안 나온다. 지우려면 `hide_arrows=True`.
- 이웃 pano 목록은 **공개 API 에 없지만** SDK 가 받아오는 JSON 에 들어 있다.
  `neighbors()` 가 그 응답을 가로챈다 — 우리가 따로 요청하지는 않는다.
- 이미지 저장은 Kakao 운영정책상 회색지대다. 연구용 로컬 실행 범위를 넘기기 전에
  docs/23-open-questions.md §2 를 볼 것.
"""
import contextlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..settings import SETTINGS
from .base import Neighbor, Pano, ProviderError

# 기본 뷰포트. 16:9 — imaging.TARGET_SIZE 와 맞춰 리사이즈를 무해하게.
#
# ⚠️ **이 값이 가로 화각을 정한다.** Kakao 는 세로 화각을 ~59.5°(zoom 0)로 고정하고
# 가로는 종횡비에서 파생시킨다 — 1280×720 에서 90.9° 다 (실측 → check_fov.py,
# docs/23-open-questions.md §3). 그런데 이 숫자는 화각을 보고 고른 것이 아니라
# **이미지 토큰을 264 로 고정하려고** TARGET_SIZE 에 맞춘 것이다. 즉 지금의 화각은
# 토큰 예산 결정의 부산물이다. 바꾸면 화각과 토큰 수가 같이 움직인다.
# 값의 정본은 app/config/trailwalk.yaml 의 image.target_size 다 — 뷰포트와
# 이미지 크기를 **한 값**으로 묶어야 리사이즈가 무해하고 화각이 안 미끄러진다.
VIEW_W, VIEW_H = SETTINGS.image.target_size

# SDK 가 pano 를 띄울 때 스스로 치는 내부 JSON 엔드포인트.
# 우리는 **추가 요청을 보내지 않고 응답만 가로챈다** (→ neighbors()).
NODE_API_MARK = "roadview-search/v2/node/"

# 프레임 안정화 (→ capture()). 조건이 둘이다: 타일 요청이 끊기고, 그다음
# 연속 N 프레임이 동일할 것. 하나만으로는 반쯤 로드된 그림이 통과한다.
# 값의 정본은 app/config/trailwalk.yaml 의 kakao 구획이다 (근거도 그쪽).
# 생성자에서 덮어쓸 수 있다 — 느린 회선에서 대기를 늘려 볼 수 있게.
TILE_QUIET_MS = SETTINGS.kakao.tile_quiet_ms
TILE_WAIT_MAX_MS = SETTINGS.kakao.tile_wait_max_ms
RENDER_SETTLE_MS = SETTINGS.kakao.render_settle_ms
RENDER_SETTLE_STABLE = SETTINGS.kakao.render_settle_stable
RENDER_SETTLE_TRIES = SETTINGS.kakao.render_settle_tries

SDK_URL = "https://dapi.kakao.com/v2/maps/sdk.js?appkey={key}&autoload=false"


def diagnose_sdk(appkey: str, origin: str) -> str:
    """SDK 가 왜 로드되지 않았는지 서버 쪽에서 직접 물어본다.

    브라우저 안에서는 알 수 없다. 인증에 실패하면 Kakao 가 JS 대신 JSON 에러를
    돌려주는데, 크롬은 그 응답을 `<script>` 로 받는 것을 거부하고
    `net::ERR_BLOCKED_BY_ORB` 로 통째로 막아버린다. **에러 내용이 사라진다.**
    30초 타임아웃만 남아서 원인을 짐작할 수 없게 된다.

    그래서 같은 URL 을 같은 Referer 로 파이썬에서 한 번 더 받아 본문을 읽는다.
    실제로 나오는 것들:

      403 NotAuthorizedError  "App(...) disabled OPEN_MAP_AND_LOCAL service."
          → 콘솔 > 앱 > 제품 설정 > 카카오맵 을 **활성화**해야 한다.
            도메인 문제가 아니다 (도메인이 틀리면 아래 401 이 나온다)
      401 AccessDeniedError   "domain mismatched! caller=..."
          → 플랫폼 > Web 사이트 도메인에 그 origin 을 등록해야 한다.
            127.0.0.1 과 localhost 는 **다른 도메인으로 취급된다**
      401                     appkey 자체가 틀림 (REST 키를 넣은 경우 등)

    반환값에 키 값이 섞이지 않게 마스킹한다.
    """
    req = urllib.request.Request(
        SDK_URL.format(key=appkey),
        headers={"Referer": origin + "/", "Origin": origin, "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(2000).decode("utf-8", "replace")
        if "kakao" in body and r.status == 200:
            return "SDK 자체는 정상 응답한다 — 렌더/네트워크 쪽을 의심할 것"
        status, detail = r.status, body
    except urllib.error.HTTPError as e:
        status, detail = e.code, e.read(2000).decode("utf-8", "replace")
    except Exception as e:
        return f"SDK URL 을 확인하지 못했다 ({type(e).__name__}: {e})"

    try:
        msg = json.loads(detail).get("message", detail)
    except Exception:
        msg = detail[:300]
    msg = msg.replace(appkey, "<KEY>")

    hint = ""
    if "disabled" in msg and "MAP" in msg.upper():
        hint = ("\n  → 앱에서 **카카오맵 서비스가 꺼져 있다.** 키나 도메인 문제가 아니다.\n"
                "     콘솔 > 내 애플리케이션 > 제품 설정 > 카카오맵 > 활성화 설정 ON")
    elif "domain" in msg.lower():
        hint = (f"\n  → 플랫폼 > Web > 사이트 도메인에 `{origin}` 을 등록할 것.\n"
                "     127.0.0.1 과 localhost 는 다른 도메인으로 취급된다")
    return f"HTTP {status} · {msg}{hint}"

_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:#000}
#rv{width:{{W}}px;height:{{H}}px}
/* 로드뷰 오버레이는 전부 캔버스가 아니라 별도 DOM 레이어라 CSS 로 다룰 수 있다.
   ID 접미사(_al_737 의 737)는 세션마다 바뀌므로 **접두사로** 잡아야 한다.

   기본은 크롬(줌·나침반·미니맵·워터마크)만 지운다. 매 프레임 같은 자리에
   찍히는 순수한 노이즈이기 때문이다.

   **방향 화살표(_al_)는 남긴다.** 처음엔 보행로를 가린다는 이유로 지웠는데,
   저건 UI 노이즈가 아니라 "여기서 어디로 갈 수 있는가" 라는 정보다 —
   우리가 문서에 "이웃 pano 목록 API 가 없다" 고 적어둔 바로 그 데이터가
   화면에 그려져 있는 셈이다. 지우려면 hide_arrows=True. */
#rv [id^="_box_util_"],        /* 줌 · 나침반 */
#rv [id^="_mm_"],              /* 미니맵 */
#rv [id^="_kakao_copyright_"],
#rv [id^="_extra_copyright_"] { display: none !important; }
{{ARROW_CSS}}</style>
<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={{APPKEY}}&autoload=false"></script>
</head><body>
<div id="rv"></div>
<script>
var rv = null, rvClient = null, ready = false;
kakao.maps.load(function () {
  rv = new kakao.maps.Roadview(document.getElementById('rv'));
  rvClient = new kakao.maps.RoadviewClient();
  ready = true;
});
window.__ready = function () { return ready; };

// 좌표 → panoId. 없으면 null.
window.__nearest = function (lat, lng, radius) {
  return new Promise(function (resolve) {
    rvClient.getNearestPanoId(new kakao.maps.LatLng(lat, lng), radius, function (panoId) {
      resolve(panoId === null || panoId === undefined ? null : String(panoId));
    });
  });
};

function curPano() {
  try { var p = rv.getPanoId(); return p === null || p === undefined ? '' : String(p); }
  catch (e) { return ''; }
}

// panoId + pan 으로 화면을 세팅하고, 렌더가 끝나면 실제 좌표를 돌려준다.
//
// 이벤트를 기다리지 않고 **폴링한다.** 처음엔 'init' 을 기다렸는데, Kakao 의
// init 은 로드뷰가 최초로 초기화될 때 딱 한 번만 뜬다. 두 번째 pano 부터는
// 영영 오지 않아서 전부 타임아웃했다 (첫 좌표만 성공하고 나머지는 실패).
// pano 전환 이벤트 이름은 SDK 버전에 따라 갈리므로, getPanoId() 가 목표값이
// 되는지 직접 보는 편이 이름 추측보다 튼튼하다.
window.__show = function (panoId, pan) {
  return new Promise(function (resolve, reject) {
    var target = String(panoId);
    var deadline = Date.now() + 12000;
    if (curPano() !== target) { rv.setPanoId(Number(panoId), null); }
    (function poll() {
      if (curPano() === target) {
        rv.setViewpoint({pan: pan, tilt: 0, zoom: 0});
        // 브라우저가 실제로 프레임을 그릴 틈을 준다. 이걸 빼면 검은 화면을 찍는다.
        requestAnimationFrame(function () { requestAnimationFrame(function () {
          setTimeout(function () {
            // ⚠️ 좌표는 **여기서** 읽는다. panoId 가 먼저 바뀌고 위치는 뒤늦게
            // 따라오기 때문에, 전환 직후에 읽으면 이전 pano 의 좌표나 NaN 이
            // 나온다. 실제로 스냅 거리가 반경 50m 인데 수 km 로 찍혔다.
            var p = rv.getPosition();
            var la = p.getLat(), ln = p.getLng();
            if (!isFinite(la) || !isFinite(ln)) {
              reject('pano ' + target + ' 의 좌표가 아직 유효하지 않다');
              return;
            }
            resolve({lat: la, lng: ln, panoId: curPano()});
          }, 400);
        }); });
        return;
      }
      if (Date.now() > deadline) {
        reject('pano ' + target + ' 로 전환되지 않았다 (현재 "' + curPano() + '")');
        return;
      }
      setTimeout(poll, 100);
    })();
  });
};
</script></body></html>
"""

# 화살표를 지우는 CSS. 기본은 비어 있다 (화살표를 남긴다).
_ARROW_CSS = '#rv [id^="_al_"], #rv [id^="_atl_"] { display: none !important; }'


def build_page(appkey: str, *, hide_arrows: bool = False,
               view_w: int = VIEW_W, view_h: int = VIEW_H) -> str:
    """페이지 HTML 조립.

    `%` 포매팅을 쓰지 않는다 — 본문이 CSS 와 JS 라 `%` 가 흔하고, 한 번
    엇갈리면 원인을 찾기 어려운 방식으로 깨진다.
    """
    return (_PAGE
            .replace("{{W}}", str(view_w))
            .replace("{{H}}", str(view_h))
            .replace("{{ARROW_CSS}}", _ARROW_CSS if hide_arrows else "")
            .replace("{{APPKEY}}", appkey))


class _Handler(BaseHTTPRequestHandler):
    page = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.page)))
        self.end_headers()
        self.wfile.write(self.page)

    def log_message(self, *a):
        pass   # 스텝마다 한 줄씩 찍히면 런로그가 안 보인다


class KakaoProvider:
    name = "kakao"
    # 런 스크립트가 RunLog.event 를 꽂는다 (→ run_explore.py). provider 는
    # 런로그 형식을 모른 채 신호만 쏘고, 안 꽂히면(테스트 등) 카운터만 남는다.
    on_event = None
    # 클래스 기본값 — __new__ 로 만드는 테스트 인스턴스에서도 안전하게.
    _tile_timeouts = 0
    _sniff_error: str | None = None

    # 대기 상수의 클래스 기본값. __init__ 이 settings 로 덮어쓴다.
    # 클래스에도 두는 이유는 __init__ 을 건너뛰고 만들어지는 인스턴스가 있기
    # 때문이다 (테스트가 브라우저 없이 _settle 만 돌린다 → test_kakao_settle.py).
    tile_quiet_ms = TILE_QUIET_MS
    tile_wait_max_ms = TILE_WAIT_MAX_MS
    render_settle_ms = RENDER_SETTLE_MS
    render_settle_stable = RENDER_SETTLE_STABLE
    render_settle_tries = RENDER_SETTLE_TRIES

    def __init__(self, appkey: str, *, host: str | None = None, port: int | None = None,
                 headless: bool | None = None, hide_arrows: bool | None = None,
                 view_w: int | None = None, view_h: int | None = None, settings=None):
        # 인자 기본값을 None 으로 두고 settings 에서 채운다. 모듈 상수를 시그니처에
        # 박아 두면 `KakaoProvider(key, settings=custom)` 이 정본 뷰포트로 찍고,
        # 그러면 화각이 image.target_size 와 어긋난 채 조용히 돈다.
        st = settings or SETTINGS
        k = st.kakao
        host = host if host is not None else k.host
        port = port if port is not None else k.port
        headless = headless if headless is not None else not st.run.headed
        hide_arrows = hide_arrows if hide_arrows is not None else k.hide_arrows
        view_w = view_w if view_w is not None else st.image.target_size[0]
        view_h = view_h if view_h is not None else st.image.target_size[1]
        if not appkey:
            raise ProviderError(
                "Kakao JS 앱키가 없다. 개발자 콘솔에서 발급하고 플랫폼 > Web 에 "
                f"http://{host}:{port} 를 등록한 뒤 KAKAO_JS_KEY 로 넘길 것.")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ProviderError(
                "playwright 가 없다:\n"
                "  pip install -r app/requirements.txt && playwright install chromium") from e

        # 대기 상수는 인스턴스에 둔다. 모듈 상수를 직접 읽으면 --config 로 다른
        # 설정을 준 런이 정본 값을 조용히 쓰게 된다.
        self.tile_quiet_ms = k.tile_quiet_ms
        self.tile_wait_max_ms = k.tile_wait_max_ms
        self.render_settle_ms = k.render_settle_ms
        self.render_settle_stable = k.render_settle_stable
        self.render_settle_tries = k.render_settle_tries

        # 뷰포트는 페이지 CSS 와 브라우저 viewport 양쪽에 박힌다. 둘이 어긋나면
        # 스크롤바가 생기거나 캔버스가 잘려서 화각이 조용히 달라진다.
        self.view_w, self.view_h = view_w, view_h
        handler = type("H", (_Handler,), {
            "page": build_page(appkey, hide_arrows=hide_arrows,
                               view_w=view_w, view_h=view_h).encode()})
        self._httpd = HTTPServer((host, port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            # WebGL 을 소프트웨어로라도 그리게 한다. 완전 headless 에서 검은 화면이
            # 나오는 가장 흔한 원인이 GPU 컨텍스트 부재다.
            args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        self._origin = f"http://{host}:{port}"
        self._page = self._browser.new_page(viewport={"width": view_w, "height": view_h})
        # pano 하나를 띄울 때마다 SDK 가 스스로 노드 정보를 받아온다. 그 응답에
        # 이웃 목록이 들어 있다 — 우리가 따로 요청하지 않고 지나가는 것을 줍는다.
        self._spots: dict[str, list[Neighbor]] = {}
        self._unsettled = 0        # 프레임이 끝내 안 멎은 캡처 수 (→ capture())
        self._tile_timeouts = 0    # 타일 요청이 끝내 안 끊긴 캡처 수 (→ _await_tiles)
        self._warmed = False       # 세션 첫 캡처를 버렸는가 (→ capture())
        self._inflight = 0         # 아직 안 끝난 타일 요청 수 (→ _await_tiles)
        self._last_net = 0.0       # 마지막 타일 요청이 끝난 시각
        self._page.on("response", self._sniff_node)
        self._page.on("request", self._net_start)
        self._page.on("requestfinished", self._net_end)
        self._page.on("requestfailed", self._net_end)
        self._page.goto(self._origin + "/", wait_until="load")
        try:
            self._page.wait_for_function("window.__ready && window.__ready()", timeout=20_000)
        except Exception:
            # 여기서 그냥 타임아웃을 던지면 "20초 기다렸는데 안 됨" 뿐이라 아무 도움이
            # 안 된다. 실패의 진짜 이유는 브라우저 밖에서만 알 수 있다 (diagnose_sdk).
            reason = diagnose_sdk(appkey, self._origin)
            self.close()
            raise ProviderError(f"Kakao SDK 가 로드되지 않았다.\n  {reason}") from None

    # ── 타일 로딩 추적 ──────────────────────────────────────────────────
    # 파노라마는 타일 여러 장으로 그려지고, 저해상도 → 고해상도로 덮어쓴다.
    # 화면이 "안 변한다" 는 것만으로는 다 붙었다고 할 수 없다 — 중간 단계에서
    # 잠깐 멎었다가 다음 타일이 도착해 다시 바뀐다. 요청이 끊긴 것을 봐야 한다.
    def _net_start(self, request) -> None:
        if request.resource_type == "image":
            self._inflight += 1

    def _net_end(self, request) -> None:
        if request.resource_type == "image":
            self._inflight = max(0, self._inflight - 1)
            self._last_net = time.monotonic()

    def _await_tiles(self) -> bool:
        """진행 중인 타일 요청이 없고, 그 상태가 잠시 유지될 때까지 기다린다.

        False = 제한 시간 안에 조용해지지 않았다. 그래도 진행은 한다 — Kakao 가
        계속 뭔가를 받아오는 상황에서 런이 멈추면 안 된다. 다만 신호는 남긴다
        (→ _settle 이 이벤트로 올린다). 이게 잦으면 상수를 의심할 것.
        """
        deadline = time.monotonic() + self.tile_wait_max_ms / 1000
        while time.monotonic() < deadline:
            quiet = time.monotonic() - self._last_net
            if self._inflight == 0 and quiet >= self.tile_quiet_ms / 1000:
                return True
            self._page.wait_for_timeout(50)   # 이벤트 루프를 돌려 콜백을 받는다
        self._tile_timeouts += 1
        return False

    # ── 이웃 (인접 pano) ────────────────────────────────────────────────
    def _sniff_node(self, response) -> None:
        """SDK 가 받아온 노드 응답에서 `spot[]` 만 뽑아 둔다.

        **우리는 이 엔드포인트를 직접 부르지 않는다.** 페이지를 정상적으로
        렌더하는 과정에서 SDK 가 이미 보낸 요청의 응답을 읽을 뿐이다.
        Kakao 에 나가는 요청 수가 늘지 않는다는 점이 중요하다 —
        문서화되지 않은 내부 API 를 따로 두드리는 것과는 성격이 다르다.
        (그래도 문서화된 계약은 아니다 → docs/23-open-questions.md §7)

        파싱 실패는 조용히 버리지 않는다. 예전에는 형식이 바뀌면 그 응답을
        (spot 하나가 깨지면 그 이웃 하나를) 말없이 버렸는데, 그러면 실재하는
        갈래가 그래프에서 증발한 채 탐색이 계속 돈다 — 상위는 "갈래가 3개였다"
        고 믿는다. 여기는 Playwright 콜백이라 raise 가 밖으로 안 나가므로,
        표식만 남기고 neighbors() 가 ProviderError 로 터뜨린다.
        """
        if NODE_API_MARK not in response.url:
            return
        try:
            street = (response.json().get("street_view") or {}).get("street") or {}
            pid = str(street["id"])
            spots = [Neighbor(pano_id=str(s["id"]), heading=float(s["pan"]),
                              lat=float(s["wgsy"]), lng=float(s["wgsx"]),
                              name=s.get("st_name"))
                     for s in street.get("spot") or []]
        except Exception as e:
            self._sniff_error = f"{type(e).__name__}: {e}"
            return
        self._spots[pid] = spots

    def _raise_if_sniff_broken(self) -> None:
        if self._sniff_error:
            raise ProviderError(
                f"이웃 응답 파싱 실패 — 노드 JSON 형식이 바뀐 것으로 보인다: "
                f"{self._sniff_error}\n"
                f"  비공개 계약이라 예고 없이 바뀔 수 있다 (docs/23-open-questions.md §7).\n"
                f"  형식을 확인하고 _sniff_node 를 맞출 것. 조용히 계속 돌면 실재하는\n"
                f"  갈래가 그래프에서 증발한다 — 그래서 여기서 멈춘다.")

    def neighbors(self, pano: Pano) -> list[Neighbor]:
        """이 pano 의 인접 pano 들. 화면의 흰 화살표와 같은 것이다.

        SDK 는 **띄운 적이 있는 pano** 의 노드 응답만 받아온다. 직선 이동은 항상
        현재 pano 를 띄운 채로 물어서 문제가 없었지만, explore 의 BFS 는 큐에서
        꺼낸 pano 를 아직 안 띄운 채 이웃부터 묻는다 — 그 경우 응답이 영영 안
        오고, 기다리다 빈 목록을 주면 호출자가 좌표 밀기로 새어 버린다
        (실측: 청계천 22노드 중 12개). 그래서 없으면 **직접 띄워서** 받아오게
        한다. 어차피 곧 capture 로 띄울 pano 라 렌더가 앞당겨질 뿐, Kakao 로
        나가는 요청이 늘지는 않는다.
        """
        for _ in range(6):              # 이미 지나간 응답이면 금방 온다
            self._raise_if_sniff_broken()
            if pano.pano_id in self._spots:
                return self._spots[pano.pano_id]
            self._page.wait_for_timeout(100)
        try:
            self._page.evaluate("([p]) => window.__show(p, 0)", [pano.pano_id])
        except Exception as e:
            raise ProviderError(f"로드뷰 렌더 실패 (pano={pano.pano_id}): {e}") from e
        for _ in range(25):             # 최대 2.5초
            self._raise_if_sniff_broken()
            if pano.pano_id in self._spots:
                return self._spots[pano.pano_id]
            self._page.wait_for_timeout(100)
        return []

    def nearest(self, lat: float, lng: float, radius_m: float) -> Pano | None:
        pid = self._page.evaluate(
            "([a,b,r]) => window.__nearest(a,b,r)", [lat, lng, radius_m])
        if not pid:
            return None
        # getNearestPanoId 는 id 만 준다. 스냅된 **실제** 좌표를 알려면 로드뷰에
        # 올려놓고 getPosition 을 읽는 수밖에 없다. 요청 좌표를 그대로 쓰면
        # 스텝마다 스냅 오차가 누적되어 경로가 실제 길에서 밀려난다.
        #
        # 여기서 pano 를 올려두면 곧바로 이어지는 capture 는 같은 panoId 라
        # viewpoint 만 바꾸고 끝난다 — 렌더 비용이 두 배가 되지 않는다.
        pos = self._page.evaluate("([p]) => window.__show(p, 0)", [pid])

        # 스냅 결과가 요청 반경 안에 있는지 확인한다. getNearestPanoId 는 반경을
        # 지키므로, 여기서 크게 벗어나면 좌표를 잘못 읽은 것이다 — 실제로 위치가
        # panoId 보다 늦게 갱신되는 탓에 50m 반경에서 수 km 가 나온 적이 있다.
        # 이걸 그냥 넘기면 탐색이 엉뚱한 곳으로 순간이동하면서도 계속 도는데,
        # 로그만 봐서는 멀쩡해 보여서 알아채기 어렵다.
        from ..geo import haversine_m
        off = haversine_m((lat, lng), (pos["lat"], pos["lng"]))
        if off > max(radius_m * 3, radius_m + 50):
            raise ProviderError(
                f"pano 스냅 위치가 요청 반경을 크게 벗어났다: {off:.0f}m (반경 {radius_m:.0f}m).\n"
                f"  로드뷰 위치 갱신이 늦은 것으로 보인다 — pano={pid}")
        return Pano(pano_id=pid, lat=pos["lat"], lng=pos["lng"])

    def capture(self, pano: Pano, heading: float) -> bytes:
        # 화각은 Kakao 가 주는 대로 쓴다 — zoom 0 고정. 각도로 지정할 수단이
        # 없다(zoom 은 −3~3 이산 배율). 실측 결과는 가로 90.9°/세로 59.5° 이고,
        # 가로는 뷰포트 종횡비에서 파생된다 (→ VIEW_W 주석, 23-open-questions.md §3).
        try:
            self._page.evaluate("([p,h]) => window.__show(p,h)", [pano.pano_id, heading % 360])
        except Exception as e:
            raise ProviderError(f"로드뷰 렌더 실패 (pano={pano.pano_id}): {e}") from e

        # ⚠️ 프레임이 **안정될 때까지** 찍는다. 고정 대기로는 부족하다.
        #
        # 새 pano 로 전환한 직후 첫 스크린샷이 타일이 덜 붙은 프레임인 경우가
        # 있다. 실측으로 잡았다 — 같은 pano·같은 방위에서 1회차만 다른 PNG 가
        # 나왔고, 그 반쯤 로드된 그림이 **반대 판정**을 받아 탐색이 다른 길로
        # 새어버렸다. (모델 자체는 결정적이다: 같은 바이트 → 같은 답.)
        #
        # 조건이 둘인 이유: "연속 두 프레임이 같다" 만으로는 부족했다. 타일이
        # 저해상도 → 고해상도로 덮어쓰는 중간에 잠깐 멎는 구간이 있어서, 거기서
        # 두 프레임이 일치해버린다. 같은 pano 를 5번 찍었더니 PNG 가 4가지로
        # 나왔고 그중 1회는 판정까지 뒤집혔다 — 전부 '안정' 판정을 통과한 채로.
        # 그래서 **타일 요청이 끊긴 것**(_await_tiles)을 먼저 보고, 그다음에
        # 연속 3프레임 일치를 본다.
        #
        # 스크린샷은 로컬이라 싸고, VLM 호출이 2.2초로 비싸다. 몇백 ms 를 더
        # 써서 판정 하나를 지키는 쪽이 압도적으로 남는 장사다.
        # 세션의 첫 캡처는 버린다. 브라우저·SwiftShader·HTTP 캐시가 전부 찬 상태라
        # 같은 pano 를 6번 찍었을 때 **1회차만** 다른 PNG 가 나왔다(3.0s vs 1.15s,
        # 2~6회차는 완전 동일). 안정 조건은 통과하는데 결과가 다르다 — 즉 "멎었다"
        # 를 잘못 판단한 게 아니라 첫 렌더 자체가 다르다. 버리는 캡처 한 번(~1.2s)
        # 으로 런 전체의 재현성을 산다.
        if not self._warmed:
            self._warmed = True
            self._settle(pano.pano_id)
        return self._settle(pano.pano_id)

    def _emit(self, kind: str, **kw) -> None:
        if self.on_event:
            self.on_event(kind, **kw)

    def _settle(self, pano_id: str | None = None) -> bytes:
        if not self._await_tiles():
            self._emit("tiles_timeout", pano_id=pano_id)
        prev = self._page.locator("#rv").screenshot(type="png")
        same = 1
        for _ in range(self.render_settle_tries):
            self._page.wait_for_timeout(self.render_settle_ms)
            cur = self._page.locator("#rv").screenshot(type="png")
            same = same + 1 if cur == prev else 1
            prev = cur
            if same >= self.render_settle_stable:
                return cur
        # 끝내 안 멎었다. 마지막 프레임을 쓰되 조용히 넘기지는 않는다 — 런로그
        # 이벤트와 카운터 둘 다에 남긴다. 반쯤 로드된 프레임이 판정을 뒤집은
        # 전례가 있으므로(→ capture 주석), 이 이벤트가 잦으면 대기 상수를 올릴 것.
        self._unsettled += 1
        self._emit("render_unsettled", pano_id=pano_id)
        return prev

    def close(self) -> None:
        for shut in (lambda: self._browser.close(), lambda: self._pw.stop(),
                     lambda: self._httpd.shutdown(), lambda: self._httpd.server_close()):
            # 하나가 실패해도 나머지는 닫아야 한다. 브라우저가 이미 죽어 있어도
            # 로컬 HTTP 서버 스레드는 반드시 내려가야 프로세스가 끝난다.
            #
            # ⚠️ shutdown() 과 server_close() 는 **둘 다** 필요하다. shutdown() 은
            # serve_forever 루프만 멈추고 리스닝 소켓은 열어둔다. 그래서 한 프로세스
            # 안에서 provider 를 닫고 다시 만들면 두 번째가 포트를 못 잡는다
            # (OSError: Address already in use). check_fov.py 가 화살표 유무로
            # 세션을 두 번 여는데 거기서 실제로 터졌다.
            with contextlib.suppress(Exception):
                shut()
