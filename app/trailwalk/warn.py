"""경고 — 런 도중 **사람이 알아야 할 일**.

`stop_reason` 과 역할이 다르다. 둘을 섞으면 어느 쪽도 답을 못 준다:

  stop_reason  = 루프가 왜 끝났나. 정확히 한 값, 항상 존재, 닫힌 집합.
                 답하는 질문: "이 결과를 **완결된 것으로 읽어도 되나**."
  warnings[]   = 런 도중 사람이 알아야 할 일. 0..n 개, 런이 계속됐는지와 무관.
                 답하는 질문: "이 결과를 **믿어도 되나**, 사용자에게 뭐라고 말하나."

겹칠 때의 규칙: **"런이 성립하지 않은 이유" 는 두 곳 모두에 넣는다.** 중복이
아니라 다른 질문에 답하는 두 필드다 — 웹은 stop_reason 을 안 읽어도 되고,
frontier 를 소비하는 이어탐색 로직은 warnings 를 안 읽어도 된다.

### 왜 문구를 표로 두는가

호출 자리에서 문자열을 짓지 않는다. 웹이 분기할 `code` 목록이 한곳에서
열거돼야 하고, 표에 없는 code 를 쓰면 터지는 테스트를 걸 수 있다. 문구가
호출부에 흩어지면 같은 상황이 런마다 다르게 표현된다.

### 왜 파일 이름이 warnings.py 가 아닌가

stdlib 의 `warnings` 와 이름이 겹치면 `import warnings` 가 어느 쪽인지
읽는 사람이 매번 확인해야 한다.
"""
from __future__ import annotations

# code → 사람이 읽는 한 줄. detail 을 format 인자로 받는다.
#
# 여기 없는 code 는 make() 가 거부한다. 새 경고를 추가하면 이 표에도 넣어야
# 하고, 그때 "웹에 뭐라고 띄울 것인가" 를 한 번은 생각하게 된다.
TEXT: dict[str, str] = {
    # ── 런이 성립하지 않았다 (1회성) ──
    "no_coverage":       "시작점 반경 {radius_m:.0f}m 안에 로드뷰가 없다",
    "provider_error":    "로드뷰 provider 가 실패했다: {error}",
    "image_ignored":     "서버가 이미지를 무시했다 — 판정이 전부 환각이다",
    "server_dead":       "VLM 서버 백엔드가 죽었다. 사람이 재시작해야 한다",
    "vlm_error":         "VLM 호출이 복구 불가로 실패했다: {error}",
    # ── 결과를 믿을지 판단해야 한다 (집계형) ──
    "neighbors_missing": "이웃 목록을 못 얻은 지점이 {count}곳 있었다 — 그만큼 갈래를 못 봤다",
    "capture_failed":    "캡처가 실패한 방향이 {count}건 있었다 — 판정을 못 받고 건너뛴 갈래다",
    "render_unsettled":  "프레임이 안 멎은 캡처가 {count}건 — 반쯤 로드된 화면이 "
                         "판정에 들어갔을 수 있다",
    "tiles_timeout":     "타일 로딩이 안 끊긴 캡처가 {count}건 있었다",
    "cache_miss":        "프리픽스 캐시 미스 {count}/{calls} — system turn 에 "
                         "가변값이 섞였는지 확인",
    "parse_failure":     "JSON 파싱 실패 {count}회",
}


class UnknownWarning(KeyError):
    """TEXT 에 없는 code. 조용히 통과시키면 웹이 못 읽는 경고가 생긴다."""


def make(code: str, **detail) -> dict:
    """`{code, message, detail}` 한 건.

    `count` 는 detail 이 아니라 최상위로 올린다 — 웹이 "몇 건인가" 를
    detail 스키마를 몰라도 읽을 수 있어야 한다.
    """
    if code not in TEXT:
        raise UnknownWarning(
            f"모르는 경고 code: {code!r}. trailwalk/warn.py 의 TEXT 에 문구를 "
            f"추가할 것 — 웹이 분기할 목록이 거기 하나여야 한다")
    try:
        message = TEXT[code].format(**detail)
    except (KeyError, IndexError, ValueError) as e:
        # 문구가 요구하는 필드를 안 넘긴 것이다. 조용히 원문을 내보내면
        # 웹에 `{count}` 가 그대로 뜬다 — 여기서 터뜨린다.
        raise UnknownWarning(
            f"경고 {code!r} 의 문구에 필요한 값이 없다: {e}") from e

    w = {"code": code, "message": message}
    if "count" in detail:
        w["count"] = detail["count"]
    rest = {k: v for k, v in detail.items() if k != "count"}
    if rest:
        w["detail"] = rest
    return w
