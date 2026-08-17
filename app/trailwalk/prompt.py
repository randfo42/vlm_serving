"""프롬프트 소유권은 애플리케이션에 있다.

서빙 레포(docs/00-design.md §7)가 명시한 경계다 — 게이트웨이는 system turn 의
**바이트 동일성을 보장하는 메커니즘**을 갖고, 그 안에 무슨 말이 들어가는지와
출력 필드가 무엇을 뜻하는지는 애플리케이션 관심사다.

그래서 여기가 유일한 진실이다:

    app/prompts/system_v1.txt   ← 판정 기준 (사람이 고치는 곳)
    SYSTEM_SHA256               ← 그 파일이 의도치 않게 변한 것을 잡는 핀

`bench/sweep.py` 에도 비슷한 문자열이 있지만 그건 **토큰 비용 측정용 픽스처**이지
운영 프롬프트가 아니다. 둘을 동기화하려 하지 말 것 — 벤치 수치의 비교 가능성은
프롬프트를 고정하는 데서 오고, 판정 품질은 이 파일을 고치는 데서 온다.

### 왜 파일이고 왜 해시인가

system turn 이 1바이트라도 달라지면 프리픽스 캐시 적중률이 0 이 된다. 에러는
안 나고 그냥 느려진다(docs/10-client-guide.md §3.1). 문자열을 코드에 두면
f-string 하나, 공백 하나가 조용히 섞인다. 파일 + 해시 핀이면 바뀐 순간 터진다.

프롬프트를 고쳤으면 SYSTEM_SHA256 을 같이 고치고, **v2 파일을 새로 만든다.**
같은 파일을 덮어쓰면 예전 평가 결과가 어느 프롬프트로 난 것인지 알 수 없게 된다.
"""
import hashlib
import json
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_FILE = PROMPT_DIR / "system_v1.txt"
SYSTEM_SHA256 = "8c97695d6d2e506917bffc3f4a327737e9385bf3de5cc13a755334b15b8dbab6"
SYSTEM_VERSION = "system_v1"


class PromptDriftError(RuntimeError):
    pass


def _read_system() -> str:
    raw = SYSTEM_FILE.read_bytes()          # verbatim. strip 하지 않는다
    got = hashlib.sha256(raw).hexdigest()
    if got != SYSTEM_SHA256:
        raise PromptDriftError(
            f"system 프롬프트가 핀과 다르다.\n"
            f"  파일: {SYSTEM_FILE}\n  기대: {SYSTEM_SHA256}\n  실제: {got}\n"
            f"의도한 변경이면 prompt.py 의 SYSTEM_SHA256 을 갱신하고, 평가 결과를\n"
            f"비교할 생각이면 system_v2.txt 로 새 파일을 만들 것.")
    return raw.decode("utf-8")


SYSTEM = _read_system()   # import 시점에 1회. 이후 절대 재생성하지 않는다


# ── 출력 스키마 ────────────────────────────────────────────────────────────
# decode 는 ~37 ms/token 이고 출력 토큰 수에만 비례한다(docs/04-b1-results.md §3).
# 필드 하나가 곧 지연이므로 용도별로 최소 스키마를 따로 둔다.
#
#   walk — 탐색 루프용. 스텝마다 여러 화각을 물어야 하므로 가장 싸야 한다.
#          실측 17 출력토큰 / 620 ms.
#   eval — 평가용. confidence 로 임계값을 움직여 ROC 를 그린다.
#          운영점을 정하려면 이게 필요하다. 실측상 +1 s 남짓.
#
# confidence 를 0~10 정수로 둔 이유: 소수점 실수는 토큰을 더 먹는데
# 임계값 스윕에 11 단계면 충분하다.
SCHEMAS = {
    "walk": {
        "type": "object",
        "properties": {"is_trail": {"type": "boolean"}},
        "required": ["is_trail"],
        "additionalProperties": False,
    },
    "eval": {
        "type": "object",
        "properties": {
            "is_trail": {"type": "boolean"},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["is_trail", "confidence"],
        "additionalProperties": False,
    },
}


def user_text(*, heading: float | None = None, step: int | None = None) -> str:
    """이미지 **뒤**에 붙는 가변 텍스트.

    좌표는 넣지 않는다. 모델이 좌표로 할 수 있는 일이 없고(위치를 모른다),
    토큰만 먹으며, 실수로 system 쪽에 새어 들어가면 캐시가 죽는다.
    진행 방향은 "길이 이어지는가" 판단에 실제로 도움이 되므로 남긴다.
    """
    if heading is None:
        return "Assess this scene."
    return f"Assess this scene. The camera faces {int(round(heading)) % 360} degrees (0=north)."


def fingerprint() -> dict:
    """런로그에 박을 프롬프트 식별자. 어느 프롬프트로 난 결과인지 모르면 쓸모없다."""
    return {"system_version": SYSTEM_VERSION, "system_sha256": SYSTEM_SHA256,
            "schemas": {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:12]
                        for k, v in SCHEMAS.items()}}
