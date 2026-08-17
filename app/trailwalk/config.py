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

# Kakao 키 이름. 콘솔이 부르는 이름과 사람마다 쓰는 이름이 갈려서 둘 다 받는다.
KAKAO_KEY_NAMES = ("KAKAO_MAP_API_KEY", "KAKAO_JS_KEY")


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
    return require(*KAKAO_KEY_NAMES, what="Kakao JavaScript 키")
