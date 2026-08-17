"""프롬프트 소유권은 애플리케이션에 있다.

서빙 레포(docs/00-design.md §7)가 명시한 경계다 — 게이트웨이는 system turn 의
**바이트 동일성을 보장하는 메커니즘**을 갖고, 그 안에 무슨 말이 들어가는지와
출력 필드가 무엇을 뜻하는지는 애플리케이션 관심사다.

그래서 여기가 유일한 진실이다:

    app/prompts/system_v*.txt   ← 판정 기준 (사람이 고치는 곳)
    PINS                        ← 그 파일이 의도치 않게 변한 것을 잡는 핀

### 세 버전은 서로 다른 질문이다

    v1  "이 사진에 산책로가 보이는가"
    v2  "카메라가 산책로 위에 서 있는가"      — 너무 엄격해서 폐기
    v3  같은 질문, 폭·노면 조건을 뺀 것        ← 기본값

원래 v1 은 둘 중 무엇인지 말한 적이 없었고, 그래서 프레임 안에 길이 보이지만
카메라는 차도 위에 있는 장면에서 판정이 갈렸다. 모델이 틀린 게 아니라
질문이 정해지지 않았던 것이다.

탐색 루프에는 "위에 서 있는가" 가 맞다. 루프는 "저기 길이 있다" 가 아니라
"여기로 한 칸 가도 되는가" 를 물어야 하고, 보이기만 하는 길을 yes 로 세면
강 건너나 둑 위로 끌려간다. 실제로 v1 은 청계천로(차도) 위에서 True 를 냈다 —
갓길의 빨간 자전거도로가 프레임에 보였기 때문이다.

v2 는 그 차도 오판은 고쳤지만 **진짜 산책로를 거부했다**(24건 중 4건). 원인은
금지 목록에 넣은 "streambed" 와 "continuous walkable surface" 였다. 청계천
산책로는 하천 바닥에 있고 폭이 1인용이며 노면이 돌·흙이라 그 조건에 걸린다.
v3 는 폭·노면을 판정에서 빼고 "정의된 길이 카메라 아래로 이어지는가" 만 남겼다.

**어느 버전도 지우지 않는다.** 이전 런이 어느 기준으로 난 결과인지 알 수 있어야
하고, 같은 구간에서 정의끼리 비교할 수 있어야 한다.

`bench/sweep.py` 에도 비슷한 문자열이 있지만 그건 **토큰 비용 측정용 픽스처**이지
운영 프롬프트가 아니다. 둘을 동기화하려 하지 말 것 — 벤치 수치의 비교 가능성은
프롬프트를 고정하는 데서 오고, 판정 품질은 이 파일을 고치는 데서 온다.

### 왜 파일이고 왜 해시인가

system turn 이 1바이트라도 달라지면 프리픽스 캐시 적중률이 0 이 된다. 에러는
안 나고 그냥 느려진다(docs/10-client-guide.md §3.1). 문자열을 코드에 두면
f-string 하나, 공백 하나가 조용히 섞인다. 파일 + 해시 핀이면 바뀐 순간 터진다.

프롬프트를 고쳤으면 PINS 도 같이 고친다. **판정 기준이 바뀌면 새 버전 파일을
만든다** — 같은 파일을 덮어쓰면 예전 평가 결과가 어느 프롬프트로 난 것인지
알 수 없게 된다.
"""
import hashlib
import json
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 버전 → 그 파일의 sha256. 새 프롬프트를 만들면 여기에 줄을 추가한다.
# 기존 줄은 고치지 않는다 — 고치는 순간 이전 런로그의 fingerprint 가 거짓이 된다.
PINS = {
    "system_v1": "8c97695d6d2e506917bffc3f4a327737e9385bf3de5cc13a755334b15b8dbab6",
    "system_v2": "ad67dd1827767304752f0d31e2f415b3027bf6b219e8d921f2fe358cdadc6f81",
    "system_v3": "cf0dfb7e79dad20929bf51803e3e4c9cb18982c5c6450e92ab4c24e99855b893",
}
DEFAULT_VERSION = "system_v3"


class PromptDriftError(RuntimeError):
    pass


_cache: dict[str, str] = {}


def load(version: str = DEFAULT_VERSION) -> str:
    """system turn 을 바이트 그대로. 같은 버전은 매번 **같은 객체**를 돌려준다.

    캐시는 성능이 아니라 안전장치다. 호출할 때마다 파일을 읽으면 런 도중
    누가 파일을 고쳤을 때 system turn 이 조용히 갈라진다 — 에러는 안 나고
    프리픽스 캐시만 죽는다.
    """
    if version in _cache:
        return _cache[version]
    if version not in PINS:
        raise PromptDriftError(
            f"모르는 프롬프트 버전 {version!r}. 아는 것: {', '.join(sorted(PINS))}")
    path = PROMPT_DIR / f"{version}.txt"
    raw = path.read_bytes()                 # verbatim. strip 하지 않는다
    got = hashlib.sha256(raw).hexdigest()
    if got != PINS[version]:
        raise PromptDriftError(
            f"system 프롬프트가 핀과 다르다.\n"
            f"  파일: {path}\n  기대: {PINS[version]}\n  실제: {got}\n"
            f"의도한 변경이면 PINS[{version!r}] 을 갱신하고, 평가 결과를 비교할\n"
            f"생각이면 새 버전 파일을 만들 것 (판정 기준이 바뀌면 이전 런과 비교 불가).")
    _cache[version] = raw.decode("utf-8")
    return _cache[version]


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
    return f"Assess this scene. The camera faces {round(heading) % 360} degrees (0=north)."


def fingerprint(version: str = DEFAULT_VERSION) -> dict:
    """런로그에 박을 프롬프트 식별자. 어느 프롬프트로 난 결과인지 모르면 쓸모없다."""
    return {"system_version": version, "system_sha256": PINS[version],
            "schemas": {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:12]
                        for k, v in SCHEMAS.items()}}
