#!/usr/bin/env python
"""로컬 웹 UI — 판정을 지도에서 조회하고 라벨을 붙인다.

    python app/run_web.py
    python app/run_web.py --config app/config/other.yaml

**CLI 인자는 `--config` 하나뿐이다** (→ CLAUDE.md "설정"). host·port·DB 경로는
설정의 `web:` 섹션이다.

이 프로세스는 조회만 한다 — 탐색 실행은 잡 큐에 넣으면 별도 워커가 집는다.
Playwright 는 여기 절대 들어오지 않는다 (→ web/api.py).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import settings

APP = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None,
                    help=f"설정 파일 경로 (기본: {settings.DEFAULT_PATH})")
    a = ap.parse_args()

    try:
        st = settings.load(a.config)
    except settings.SettingsError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    import uvicorn
    from web.api import create_app

    db = APP / st.web.db
    app = create_app(st, db)

    host, port = st.web.host, st.web.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        # 루프백 밖은 수집한 pano 좌표를 LAN 에 노출하는 것이다 — 막지는
        # 않되(사용자 결정) 조용히 넘어가지도 않는다
        print(f"⚠  루프백이 아닌 {host} 에 바인드한다 — 수집 좌표가 네트워크에 "
              f"노출된다. 배포 범위를 넓히려면 docs/23 §2(약관)부터 볼 것",
              file=sys.stderr)
    print(f"DB: {db}")
    # 접속 주소는 바인드 주소가 아니라 Kakao 콘솔에 등록된 도메인 쪽이어야
    # 한다 — Kakao 는 localhost 와 127.0.0.1 을 다른 도메인으로 본다.
    # 지도가 빈 화면이면 이 줄이 첫 번째 확인 대상이다 (→ trailwalk.yaml web:)
    print(f"접속: http://localhost:{port}/  (Kakao 도메인 등록이 localhost 기준)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
