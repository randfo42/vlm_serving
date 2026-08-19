"""비밀값 로딩. app/.env → 프로세스 환경변수.

의존성 없는 최소 구현이다 (python-dotenv 를 끌어오지 않는다). 하는 일이
"KEY=VALUE 를 읽어 os.environ 에 넣는다" 뿐이라 남의 라이브러리를 쓸 이유가 없다.

### 값을 절대 출력하지 않는다

이 모듈의 어떤 함수도 키 값을 반환하거나 찍지 않는다. `require()` 만이 값을
돌려주고, 그 값은 호출자가 곧바로 API 로 넘기는 데 쓴다. 로그·예외 메시지·
`repr` 어디에도 값이 새지 않게 할 것.

에러 메시지에 값을 넣고 싶은 유혹이 반드시 생긴다 ("키가 이상한데 뭐가 들었지").
넣지 말 것 — 예외는 스택트레이스에 남고 스택트레이스는 어디로든 간다.
확인이 필요하면 `.claude/hooks/keycheck.py` 가 값 없이 알려준다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# Kakao **JavaScript** 키의 이름 후보. 순서가 곧 우선순위다.
#
# REST 키 이름(KAKAO_MAP_REST_API_KEY)은 일부러 넣지 않는다. 둘 다 32자 16진수라
# 값으로는 구별되지 않으므로, 실수로 REST 키가 여기 끼면 SDK 가 조용히 로드에
# 실패하고 증상은 "로드뷰가 안 뜬다" 로만 보인다 — 커버리지 문제로 오인하기 딱 좋다.
# 이름으로 갈라놓는 것이 이 혼동을 막는 유일한 수단이다.
KAKAO_KEY_NAMES = ("KAKAO_MAP_JS_API_KEY", "KAKAO_JS_KEY", "KAKAO_MAP_API_KEY")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def load_env(path: Path | None = None, *, override: bool = False) -> int:
    """app/.env 를 읽어 os.environ 에 넣는다. 넣은 개수를 돌려준다.

    기본적으로 **이미 있는 환경변수를 덮어쓰지 않는다.** 쉘에서 준 값이
    파일보다 우선이어야 일회성 실험(`KAKAO_MAP_API_KEY=other python ...`)이
    가능하다.
    """
    p = path or ENV_FILE
    if not p.exists():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        name, value = m.group(1), _unquote(m.group(2))
        if override or name not in os.environ:
            os.environ[name] = value
            n += 1
    return n


def require(*names: str, what: str = "") -> str:
    """이름들 중 처음으로 값이 있는 것을 돌려준다. 없으면 안내와 함께 실패.

    **예외 메시지에 값을 넣지 않는다.** 이름만 말한다.
    """
    load_env()
    for name in names:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    raise RuntimeError(
        f"{what or names[0]} 이(가) 없다.\n"
        f"  찾아본 이름: {', '.join(names)}\n"
        f"  {ENV_FILE} 에 넣거나 환경변수로 줄 것 (형식은 app/.env.example).\n"
        f"  들어 있는 키를 값 없이 확인하려면: python3 .claude/hooks/keycheck.py")


def kakao_appkey() -> str:
    """Kakao 지도 JS SDK 의 appkey.

    ⚠️ **REST API 키가 아니라 JavaScript 키다.** 콘솔의 '앱 키' 화면에는 네 종류가
    나란히 있고 전부 32자 16진수라 겉으로 구별되지 않는다. 잘못 넣으면 SDK 가
    로드되다 실패하고, 증상은 "로드뷰가 안 뜬다" 로만 보여서 커버리지 문제로
    오인하기 쉽다. → app/docs/23-open-questions.md §1
    """
    load_env()
    # REST 키만 있는 상태를 조용히 넘기지 않는다. 이 경우 require() 는 "키가 없다"
    # 는 엉뚱한 안내를 하게 되는데, 실제 문제는 "키 종류가 틀렸다" 이다.
    if not any(os.environ.get(n, "").strip() for n in KAKAO_KEY_NAMES) \
            and os.environ.get("KAKAO_MAP_REST_API_KEY", "").strip():
        raise RuntimeError(
            "REST API 키만 있다. 로드뷰는 **JavaScript 키**를 요구한다 —\n"
            "  kakao.maps.Roadview 는 JS SDK 에만 있고 SDK 로더의 appkey 는 JS 키를 받는다.\n"
            f"  {ENV_FILE} 에 KAKAO_MAP_JS_API_KEY 를 추가할 것.\n"
            "  두 키는 둘 다 32자 16진수라 값으로는 구별되지 않는다. 콘솔 > 내 애플리케이션\n"
            "  > 앱 키 에서 'JavaScript 키' 항목을 확인할 것.")
    return require(*KAKAO_KEY_NAMES, what="Kakao JavaScript 키")


def kakao_rest_key() -> str:
    """Kakao **REST** API 키 — 로컬 검색(지오코딩)·좌표변환(transcoord)용.

    `KAKAO_KEY_NAMES` 에 절대 넣지 않는다. 저 목록은 JS SDK 전용이고, REST 키가
    끼면 SDK 가 조용히 로드에 실패한다 (위 주석). 반대 방향도 같다 — 이 함수는
    REST 이름 하나만 보고, JS 키로 폴백하지 않는다. REST 엔드포인트에 JS 키를
    보내면 401 이 아니라 **403 quota 류의 오해하기 좋은 에러**가 온다.
    """
    return require("KAKAO_MAP_REST_API_KEY", what="Kakao REST API 키")
