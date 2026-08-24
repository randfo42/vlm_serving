"""프롬프트 소유권은 애플리케이션에 있다.

서빙 레포가 명시한 경계다 — 게이트웨이는 system turn 의
**바이트 동일성을 보장하는 메커니즘**을 갖고, 그 안에 무슨 말이 들어가는지와
출력 필드가 무엇을 뜻하는지는 애플리케이션 관심사다.

그래서 여기가 유일한 진실이다:

    app/prompts/system_v*.txt   ← 판정 기준 (사람이 고치는 곳)
    PINS                        ← 그 파일이 의도치 않게 변한 것을 잡는 핀

### 여섯 버전은 서로 다른 질문이다

    v1  "이 사진에 산책로가 보이는가"
    v2  "카메라가 산책로 위에 서 있는가"      — 너무 엄격해서 폐기
    v3  같은 질문, 폭·노면 조건을 뺀 것
    v4  "카메라 발밑이 무엇인가" (범주)
    v5  "주변에 자연이 얼마나 있는가" (0~3)
    v6  v5 + "걸을 데가 있는가" (0/1)          ← 기본값. 응답이 둘이다

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

v3 는 그 대가로 **골목을 전부 산책로로 셌다.** 골목은 "정의된 길이 카메라
아래로 이어진다" 를 정확히 만족한다. 2026-08-23 에 약수동 500장의 True 판정
8건을 눈으로 확인했더니 8건 전부 카메라가 차도 위였고, 그 중 여섯은 프레임
안에 중앙선·빗금·30 표지·어린이보호구역 노면표시가 찍혀 있었다 — **차가
다닌다는 증거가 사진 안에 있는데 프롬프트가 그걸 보라고 한 적이 없었다.**

v1·v2·v3 는 느슨/엄격 한 축에서만 왕복했다. v4 는 축을 하나 더 놓는다:
**이 노면을 차와 공유하는가.** 그리고 불리언을 버리고 범주(`camera_surface`)를
받는다 — 어디가 경계인지(보행자우선 골목을 산책로로 셀 것인가)는 라벨 없이
못 정하므로, 그 경계를 프롬프트 문장이 아니라 **설정**에 둔다
(`vlm.trail_surfaces`). 경계를 옮겨도 프롬프트 버전이 안 바뀌고, 이미 받아
둔 판정을 다시 해석할 수 있다.

### v5 는 축을 바꾼다 — 노면이 아니라 자연

v1~v4 는 전부 **노면**을 물었다. 카메라가 무엇 위에 있는가, 그 노면을 차와
공유하는가. v5 는 그 축을 통째로 버리고 **주변에 자연이 얼마나 있는가**만
0~3 으로 묻는다.

이유는 v4 의 실패에 있다. 2026-08-24 약수역 반경 1km explore(판정 1,916건)
에서 `park_path` 2건이 나왔는데 하나가 **터널 안 차도**였다 — 차선·조명·
주행 차량이 다 찍혀 있는데 산책로로 뒤집혔다. 노면만 보면 터널 바닥과 공원
길은 둘 다 "차선 없는 회색 면" 이라 헷갈릴 자리가 있다. 자연을 물으면 터널은
식생이 0이라 헷갈릴 수가 없다.

**대가가 있다:** v5 는 "여기로 걸어갈 수 있는가" 를 아예 안 묻는다. 차도
한복판에서 찍어도 길 건너가 공원이면 높은 점수가 나온다. §5 가 정하고
v2~v4 가 계속 싸운 "카메라가 그 위에 서 있는가" 규칙이 사라진다는 뜻이다.
이건 의도된 것이다 — 판정의 뜻이 달라졌다는 것을 알고 쓸 것.

등급은 사람이 정했다: 가로수만 1 · 큰 나무가 많으면 2 · 완전히 공원 같으면 3.
0(거의 없음)은 프롬프트를 쓰면서 더했다 — 셋 다 녹지가 있다는 전제라
콘크리트 골목이 들어갈 칸이 없었다.

### v6 은 축을 하나 되살린다 — 다만 따로 묻는다

v5 를 약수역 1km 로 돌린 결과(판정 1,971건)가 축 하나로는 부족하다고 말했다.
등급 3 세 건 중 하나가 **자연이 거의 없는 콘크리트 골목**이었고, 무엇보다
v5 는 "걸을 데가 있는가" 를 아예 안 묻는다 — 차도 한복판이어도 녹지만
많으면 높은 점수가 나온다.

v6 은 **응답을 둘로 나눈다.** `nature_level`(0~3)은 v5 와 **바이트 동일한
문구**를 쓰고, `footway`(0/1)를 따로 묻는다. 한 필드에 두 뜻을 섞지 않는
것이 요점이다 — 섞으면 낮은 점수가 "자연이 없다" 인지 "걸을 데가 없다"
인지 구별할 수 없고, 그건 v1~v4 가 반복해서 밟은 함정이다.

`footway` 의 근거는 셋이고 **어느 하나만 있어도 1** 이다: 물리적 분리(연석·
볼라드·난간·완충 식재) · 보행 공간을 지정한 도색 · 그 길을 따라 걷는 사람.
도색과 보행자를 근거로 넣은 것은 사람의 결정이다 — 처음 초안은 둘 다
배제했었다.

다만 두 군데를 좁혔다. **차량용 도색은 안 센다**(중앙선·차선·빗금·정지선).
안 그러면 황색 중앙선 하나로 1 이 되어 이 필드가 상수가 된다. 그리고
**횡단하는 사람은 안 센다** — 세면 사거리가 전부 1 이 된다.

**터널 차로는 0 이다.** 차선을 도색 근거로 인정하지 않는 것이 그 근거이기도
하다. v4 가 터널을 `park_path` 로 부른 오답(2026-08-24)이 여기서 걸린다.

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
    "system_v4": "e6831068faa826a4905c74f38dea7ed43285adfd3f30051fe54dc28d71920b0d",
    "system_v5": "9c4ae48bbcdbde607720af55cd9e22f7c5b0f91c7078afb4f517d54b553d6db2",
    "system_v6": "b0a903f554fb8274d3ecaed932b2817ea5c03cb34e7f2e73fe2bbdecd534eb14",
}
DEFAULT_VERSION = "system_v6"


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
# decode 는 ~37 ms/token 이고 출력 토큰 수에만 비례한다 (서빙 실측).
# 필드 하나가 곧 지연이므로 용도별로 최소 스키마를 따로 둔다.
#
#   walk         — v1~v3 용. is_trail 불리언 하나. 실측 17 출력토큰 / 620 ms.
#   eval         — v1~v3 평가용. + confidence.
#   surface      — v4 용 탐색 루프. camera_surface 범주 하나.
#   surface_eval — v4 평가용. + confidence.
#   nature       — v5 용 탐색 루프. nature_level 정수 하나.
#   nature_eval  — v5 평가용. + confidence.
#   nature_footway      — v6 용 탐색 루프. nature_level + footway 둘.
#   nature_footway_eval — v6 평가용. + confidence.
#
# confidence 를 0~10 정수로 둔 이유: 소수점 실수는 토큰을 더 먹는데
# 임계값 스윕에 11 단계면 충분하다.
#
# ⚠️ **walk/eval 을 지우지 않는다.** v1~v3 로 난 런을 재현하려면 그 스키마가
# 있어야 한다. v4 는 is_trail 을 아예 내지 않으므로 스키마도 별개다.

# camera_surface 가 가질 수 있는 값 전부 (→ prompts/system_v4.txt).
# 순서는 "차와 공유하는가" 로 정렬돼 있다 — 앞 둘이 차량 공유, 나머지가 보행.
# 이 목록이 곧 열거형이라 서버의 strict json_schema 가 다른 값을 못 내게 한다.
SURFACES = [
    "roadway",         # 차량 + 노면표시·표지
    "shared_alley",    # 차량, 차선 도색 없음 — 골목
    "sidewalk",        # 차도 옆 보도. 폭·가로수 무관
    "pedestrian_way",  # 차량 배제 — 볼라드·계단·폭 전체 보행포장
    "park_path",       # 공원·숲·산
    "waterside",       # 하천·수변 (하천 바닥 포함)
    "open_ground",     # 정의된 길 없음 — 잔디·공터·자갈
    "unclear",         # 노면을 못 봄
]

# 자연 등급 0~3 (→ prompts/system_v5.txt). 사람이 정한 것은 1·2·3 이고
# 0(거의 없음)은 프롬프트를 쓰며 더했다 — 1~3 이 전부 녹지가 있다는 전제라
# 콘크리트 골목이 들어갈 칸이 없었다.
#
#   0 거의 없음 (화분은 자연이 아니다. 노면을 못 봐도 여기)
#   1 가로수·산울타리 — 녹지가 가장자리 선
#   2 큰 나무가 많다 · 양옆 녹지 · 수관이 덮는다 · 물이 흐른다
#   3 공원·숲·물길이 프레임 대부분. 자연이 배경 그 자체
NATURE_MIN, NATURE_MAX = 0, 3
NATURE_SCHEMAS = frozenset({"nature", "nature_eval"})

# v6 은 여기에 footway 0/1 을 더한다. 한 필드에 두 뜻을 섞지 않는 것이 요점이라
# 등급과 별개로 둔다 — 섞으면 낮은 점수가 "자연이 없다" 인지 "걸을 데가 없다"
# 인지 구별할 수 없다.
NATURE_FOOTWAY_SCHEMAS = frozenset({"nature_footway", "nature_footway_eval"})

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
    "surface": {
        "type": "object",
        "properties": {"camera_surface": {"type": "string", "enum": SURFACES}},
        "required": ["camera_surface"],
        "additionalProperties": False,
    },
    "surface_eval": {
        "type": "object",
        "properties": {
            "camera_surface": {"type": "string", "enum": SURFACES},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["camera_surface", "confidence"],
        "additionalProperties": False,
    },
    "nature": {
        "type": "object",
        "properties": {"nature_level": {"type": "integer",
                                        "minimum": NATURE_MIN, "maximum": NATURE_MAX}},
        "required": ["nature_level"],
        "additionalProperties": False,
    },
    "nature_eval": {
        "type": "object",
        "properties": {
            "nature_level": {"type": "integer",
                             "minimum": NATURE_MIN, "maximum": NATURE_MAX},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["nature_level", "confidence"],
        "additionalProperties": False,
    },
    "nature_footway": {
        "type": "object",
        "properties": {
            "nature_level": {"type": "integer",
                             "minimum": NATURE_MIN, "maximum": NATURE_MAX},
            # 불리언이 아니라 0/1 정수다 — 사람이 그렇게 정했고, 등급과 같은
            # 모양이라 JSONL 을 읽을 때 형이 섞이지 않는다
            "footway": {"type": "integer", "minimum": 0, "maximum": 1},
        },
        "required": ["nature_level", "footway"],
        "additionalProperties": False,
    },
    "nature_footway_eval": {
        "type": "object",
        "properties": {
            "nature_level": {"type": "integer",
                             "minimum": NATURE_MIN, "maximum": NATURE_MAX},
            "footway": {"type": "integer", "minimum": 0, "maximum": 1},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["nature_level", "footway", "confidence"],
        "additionalProperties": False,
    },
}

# 범주를 내는 스키마. is_trail 은 이 경우 서버가 아니라 **설정**이 정한다
# (`vlm.trail_surfaces`) — VlmClient 가 유도한다.
SURFACE_SCHEMAS = frozenset({"surface", "surface_eval"})
# NATURE_SCHEMAS 는 SCHEMAS 앞에 있다 — 스키마 정의가 그 상수를 읽는다


# 프롬프트 버전 → 그 버전과 짝이 맞는 스키마.
#
# 짝이 안 맞으면 에러가 아니라 **환각**이 나온다. v4 에게 walk 스키마를 주면
# 모델은 프롬프트에 나온 적 없는 `is_trail` 을 지어내고, 서버의 strict
# json_schema 가 형식은 맞춰 주므로 HTTP 200 에 파싱도 성공한다. 이 레포가
# 실제로 당한 사고와 같은 모양이다(confidence 를 스키마에만 넣었더니 모델이
# "산책로다움" 점수로 해석했다 → tests/test_prompt.py).
COMPATIBLE = {
    "system_v1": ("walk", "eval"),
    "system_v2": ("walk", "eval"),
    "system_v3": ("walk", "eval"),
    "system_v4": ("surface", "surface_eval"),
    "system_v5": ("nature", "nature_eval"),
    "system_v6": ("nature_footway", "nature_footway_eval"),
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
