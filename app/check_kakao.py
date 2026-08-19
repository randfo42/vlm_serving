#!/usr/bin/env python
"""Kakao 로드뷰 진단 — 실패의 원인을 갈라준다.

    python app/check_kakao.py             # headless
    python app/check_kakao.py --headed    # 브라우저를 띄워서

`run_walk.py --provider kakao` 가 안 될 때 원인이 넷인데 겉으로는 전부
"안 된다" 로 똑같이 보인다 (app/docs/23-open-questions.md §1):

    1. 키 종류가 틀림 / 도메인 미등록  → SDK 가 로드조차 안 됨
    2. 그 좌표에 로드뷰가 없음          → panoId 가 null
    3. WebGL 렌더 실패                  → 검은 화면
    4. 판정이 틀림                      → 그림은 멀쩡한데 is_trail=false

이 스크립트는 1~3 만 본다. VLM 을 부르지 않으므로 서버가 없어도 돌고,
4 는 애초에 여기서 답할 문제가 아니다.

각 좌표에서: panoId 가 잡히는가 → 실제로 그려지는가(픽셀 분산으로 판정) →
스냅 거리가 얼마인가. 마지막에 표로 요약한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import geo
from trailwalk.providers.base import ProviderError

# 서울 좌표 표본. **차도 대조군을 반드시 포함한다** — 차도조차 안 잡히면
# 커버리지 문제가 아니라 키/도메인 문제다. 이 구분이 이 스크립트의 존재 이유다.
SAMPLES = [
    ("시청 앞 대로 (대조군: 차도)",      37.5663, 126.9779,   0),
    ("청계천 산책로",                    37.5695, 127.0050,  90),
    ("서울숲 공원길",                    37.5444, 127.0374,   0),
    ("한강 반포 수변길",                 37.5100, 126.9960,  90),
    ("남산 둘레길",                      37.5512, 126.9882, 180),
    ("홍릉 두물길 (신설동역 부근)",       37.5754, 127.0250,  90),
]


def blackness(png: bytes) -> tuple[float, float]:
    """(평균 밝기 0~255, 표준편차). 렌더 실패는 분산이 0에 가깝게 나온다.

    평균만 보면 안 된다 — 밤 로드뷰나 터널도 어둡다. 진짜 렌더 실패는
    **모든 픽셀이 같은 값**이라 표준편차가 0 이다.
    """
    import io

    from PIL import Image
    g = Image.open(io.BytesIO(png)).convert("L")
    px = list(g.getdata())
    n = len(px)
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    return mean, var ** 0.5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--headed", action="store_true", help="브라우저를 띄운다")
    ap.add_argument("--radius", type=float, default=50.0, help="pano 검색 반경 (m)")
    ap.add_argument("--save", metavar="DIR", default=None,
                    help="캡처를 저장한다. 약관상 회색지대이니 진단 목적으로만")
    a = ap.parse_args()

    from trailwalk.config import kakao_appkey
    from trailwalk.providers.kakao import KakaoProvider

    try:
        key = kakao_appkey()
    except RuntimeError as e:
        print(f"✗ {e}")
        return 2
    print(f"JS 키 확인됨 ({len(key)}자). 브라우저 기동…\n")

    try:
        prov = KakaoProvider(appkey=key, headless=not a.headed)
    except ProviderError as e:
        print(f"✗ provider 기동 실패: {e}")
        return 2

    # SDK/인증 오류는 브라우저 콘솔에만 나온다. 이걸 안 보면 원인 1을 놓친다.
    #
    # ⚠️ 반드시 마스킹한다. appkey 는 SDK URL 의 쿼리스트링에 실려 있어서
    # 콘솔/네트워크 로그에 URL 이 통째로 찍히면 키가 그대로 새어 나온다.
    # 실제로 이 스크립트를 만들다 한 번 흘렸다.
    logs: list[str] = []

    def note(s: str) -> None:
        logs.append(s.replace(key, "<KEY>"))

    prov._page.on("console", lambda m: note(f"[{m.type}] {m.text}"))
    prov._page.on("pageerror", lambda e: note(f"[pageerror] {e}"))
    prov._page.on("requestfailed",
                  lambda r: note(f"[reqfail] {r.url} :: {r.failure}"))

    rows = []
    try:
        for name, lat, lng, heading in SAMPLES:
            try:
                pano = prov.nearest(lat, lng, a.radius)
            except Exception as e:
                # 예외 **메시지**가 진단의 전부다. 타입만 남기면 Playwright 의
                # 'Error' 하나로 뭉개져 아무것도 알 수 없다 (실제로 한 번 그랬다).
                msg = str(e).replace(key, "<KEY>").splitlines()
                rows.append((name, "오류", "-", "-", msg[0][:70] if msg else type(e).__name__))
                continue
            if pano is None:
                rows.append((name, "없음", "-", "-", "로드뷰 미촬영"))
                continue
            dist = geo.haversine_m((lat, lng), (pano.lat, pano.lng))
            try:
                png = prov.capture(pano, heading)
            except Exception as e:
                rows.append((name, "잡힘", f"{dist:.0f}m", "캡처실패", str(e)[:40]))
                continue
            mean, sd = blackness(png)
            verdict = "검은화면" if sd < 3.0 else "정상"
            if a.save:
                d = Path(a.save); d.mkdir(parents=True, exist_ok=True)
                (d / f"{pano.pano_id}_{int(heading)}.png").write_bytes(png)
            rows.append((name, "잡힘", f"{dist:.0f}m", verdict,
                         f"밝기{mean:.0f} 분산{sd:.0f} · {pano.pano_id}"))
    finally:
        prov.close()

    print(f"{'좌표':32} {'pano':6} {'거리':>6} {'렌더':8} 비고")
    print("-" * 100)
    for name, got, dist, verdict, note in rows:
        print(f"{name:32} {got:6} {dist:>6} {verdict:8} {note}")

    # ── 해석 ────────────────────────────────────────────────────────────
    found = [r for r in rows if r[1] == "잡힘"]
    rendered = [r for r in found if r[3] == "정상"]
    control_ok = any(r[1] == "잡힘" for r in rows if "대조군" in r[0])

    print()
    if not found:
        print("✗ 어느 좌표에서도 pano 를 못 잡았다.")
        print("  차도 대조군까지 실패했으므로 커버리지 문제가 아니다.")
        print("  → 키 종류(JavaScript 키인가) 또는 플랫폼>Web 도메인 등록"
              " (http://127.0.0.1:8731) 을 의심할 것.")
    elif not control_ok:
        print("⚠ 대조군(차도)이 실패했는데 다른 곳은 잡혔다. 표본 좌표를 의심할 것.")
    elif not rendered:
        print("✗ pano 는 잡히는데 전부 검은 화면이다 → WebGL 렌더 문제.")
        print("  --headed 로 다시 볼 것. 키와 커버리지는 정상이다.")
    else:
        trail_ok = [r for r in rendered if "대조군" not in r[0]]
        print(f"✓ {len(rendered)}/{len(rows)} 좌표에서 로드뷰가 정상 렌더됐다.")
        print(f"  이 중 산책로 표본 {len(trail_ok)}/{len(rows) - 1} 개."
              f" → 산책로 커버리지가 {'있다' if trail_ok else '없다'}.")

    if logs:
        print("\n── 브라우저 콘솔 (키/도메인 문제는 여기에만 나온다) ──")
        for line in logs[:25]:
            print(" ", line[:160])
    return 0 if rendered else 1


if __name__ == "__main__":
    raise SystemExit(main())
