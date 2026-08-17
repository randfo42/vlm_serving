"""Kakao 로드뷰 provider — headless 브라우저로 JS SDK 를 몬다.

⚠️ **미검증이다.** Kakao JS 앱키가 없어 아직 한 번도 돌려보지 못했다.
   실행 전에 docs/23-open-questions.md §1 을 읽을 것.

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
- 이웃 pano 목록 API 가 없다. 이동은 walk.py 가 좌표 계산으로 만든다.
- 이미지 저장은 Kakao 운영정책상 회색지대다. 연구용 로컬 실행 범위를 넘기기 전에
  docs/23-open-questions.md §2 를 볼 것.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .base import Pano, ProviderError

VIEW_W, VIEW_H = 1280, 720   # 16:9 — imaging.TARGET_SIZE 와 맞춰 리사이즈를 무해하게

_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:#000}
#rv{width:%dpx;height:%dpx}</style>
<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey=%s&autoload=false"></script>
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

// panoId + pan 으로 화면을 세팅하고, 렌더가 끝나면 실제 좌표를 돌려준다.
// init 은 pano 가 바뀔 때만 뜨므로 같은 pano 면 viewpoint 만 바꾸고 바로 끝낸다.
window.__show = function (panoId, pan) {
  return new Promise(function (resolve, reject) {
    var to = setTimeout(function () { reject('timeout'); }, 15000);
    var done = function () {
      clearTimeout(to);
      var p = rv.getPosition();
      // 브라우저가 실제로 프레임을 그릴 틈을 준다. 이걸 빼면 검은 화면을 찍는다.
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        setTimeout(function () { resolve({lat: p.getLat(), lng: p.getLng()}); }, 250);
      }); });
    };
    if (String(rv.getPanoId()) === String(panoId)) {
      rv.setViewpoint({pan: pan, tilt: 0, zoom: 0});
      done();
    } else {
      kakao.maps.event.addListener(rv, 'init', function handler() {
        rv.setViewpoint({pan: pan, tilt: 0, zoom: 0});
        done();
      });
      rv.setPanoId(Number(panoId), null);
    }
  });
};
</script></body></html>
""" % (VIEW_W, VIEW_H, "%s")


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

    def __init__(self, appkey: str, *, host: str = "127.0.0.1", port: int = 8731,
                 headless: bool = True):
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

        handler = type("H", (_Handler,), {"page": (_PAGE % appkey).encode()})
        self._httpd = HTTPServer((host, port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            # WebGL 을 소프트웨어로라도 그리게 한다. 완전 headless 에서 검은 화면이
            # 나오는 가장 흔한 원인이 GPU 컨텍스트 부재다.
            args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        self._page = self._browser.new_page(viewport={"width": VIEW_W, "height": VIEW_H})
        self._page.goto(f"http://{host}:{port}/", wait_until="load")
        self._page.wait_for_function("window.__ready && window.__ready()", timeout=30_000)

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
        return Pano(pano_id=pid, lat=pos["lat"], lng=pos["lng"])

    def capture(self, pano: Pano, heading: float, fov_deg: float) -> bytes:
        # fov_deg 는 쓰지 않는다. Kakao 의 zoom 은 −3~3 의 이산 배율이라 각도로
        # 지정할 수 없다. zoom 0 의 기본 화각을 그대로 쓴다. → 23-open-questions.md §3
        try:
            self._page.evaluate("([p,h]) => window.__show(p,h)", [pano.pano_id, heading % 360])
        except Exception as e:
            raise ProviderError(f"로드뷰 렌더 실패 (pano={pano.pano_id}): {e}") from e
        return self._page.locator("#rv").screenshot(type="png")

    def close(self) -> None:
        for shut in (lambda: self._browser.close(), lambda: self._pw.stop(),
                     lambda: self._httpd.shutdown()):
            try:
                shut()
            except Exception:
                pass
