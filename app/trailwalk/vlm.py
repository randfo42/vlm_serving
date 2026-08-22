"""VLM 호출. 요청 1건 = 이미지 1장 = 1턴. 동시 요청 없음.

docs/10-client-guide.md 의 체크리스트를 코드로 옮긴 것이다. 이 서비스의 실패는
대부분 **에러 없이** 일어나므로, 성공 응답에도 검사가 붙는다.

  · prompt_tokens 가 비정상적으로 작다  → 이미지가 통째로 무시됐다 (WEBP 사고)
  · cached_tokens 가 0                 → system turn 에 가변값이 섞였다
  · finish_reason != "stop"            → 출력이 잘렸다

동시성을 쓰지 않는 이유는 클라이언트 사정이 아니라 서버 사정이다. 비전 인코딩이
원자적·직렬이라 GPU 를 독점하고, -np 4 는 처리량 +20% 에 지연 3.3배다
(서빙 실측). 병렬화는 여기가 아니라 서빙 쪽에서 풀 문제다.
"""
import contextlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import prompt as P
from .settings import SETTINGS

# 값의 정본은 app/config/trailwalk.yaml 이다 (근거 주석도 그쪽에 있다).
# VlmClient 는 생성자에서 settings 를 받아 인스턴스 속성으로 쓰므로,
# 아래 별칭은 "설정을 안 넘겼을 때의 기본" 자리에서만 쓰인다.
DEFAULT_URL = SETTINGS.vlm.url
MAX_TOKENS = SETTINGS.vlm.max_tokens
TIMEOUT_S = SETTINGS.vlm.timeout_s
FATAL_500_STREAK = SETTINGS.vlm.fatal_500_streak


class VlmError(RuntimeError):
    """재시도해도 소용없는 실패."""


class ServerDeadError(VlmError):
    """서버 백엔드 사망 추정. 사람이 재시작해야 한다."""


class ImageIgnoredError(VlmError):
    """HTTP 200 인데 이미지가 무시됐다. 포맷 사고 — 이미지를 다시 만들어야 한다."""


@dataclass
class Verdict:
    is_trail: bool
    confidence: int | None
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    latency_ms: float
    raw: dict = field(repr=False, default_factory=dict)


@dataclass
class Stats:
    """런 전체에 걸친 조용한 이상 신호 카운터. 끝나고 반드시 들여다볼 것."""
    calls: int = 0
    retries: int = 0
    cache_misses: int = 0     # cached_tokens == 0 인 횟수
    parse_failures: int = 0
    total_ms: float = 0.0


class VlmClient:
    def __init__(self, url: str | None = None, schema_name: str | None = None,
                 system_version: str | None = None, settings=None):
        # settings 를 안 주면 정본을 쓴다. 인자로 받아 두는 이유는 --config 로
        # 다른 설정을 준 런이 모듈 상수(=정본)를 조용히 쓰면 안 되기 때문이다.
        #
        # 인자 기본값을 None 으로 두는 것이 요점이다. `url=DEFAULT_URL` 처럼
        # 모듈 상수를 박아 두면 settings 만 넘긴 호출이 옛 URL 로 요청을 보내고
        # 에러도 안 난다 — 호출부가 매번 같이 넘겨 우회하고 있을 뿐인 함정이다.
        s = settings or SETTINGS
        self.url = url if url is not None else s.vlm.url
        schema_name = schema_name if schema_name is not None else s.vlm.schema
        system_version = (system_version if system_version is not None
                          else s.vlm.prompt_version)
        self.max_tokens = s.vlm.max_tokens
        self.timeout_s = s.vlm.timeout_s
        self.fatal_500_streak = s.vlm.fatal_500_streak
        # 이미지 무시(WEBP 사고)를 잡는 유일한 신호. 모듈 상수로 읽으면
        # --config 로 expected_image_tokens 를 바꾼 런이 옛 하한을 쓴다
        self.min_prompt_tokens = s.image.min_prompt_tokens
        self.schema_name = schema_name
        self.schema = P.SCHEMAS[schema_name]
        # 클라이언트 수명 내내 같은 문자열을 재사용한다. 요청마다 다시 읽으면
        # 1바이트만 달라져도 프리픽스 캐시가 죽는데 에러는 안 난다.
        self.system_version = system_version
        self.system = P.load(system_version)
        self.stats = Stats()
        self._streak_500 = 0

    # ── HTTP ────────────────────────────────────────────────────────────
    def _post(self, body: dict) -> tuple[dict, float]:
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            payload = json.load(r)
        return payload, (time.perf_counter() - t0) * 1000

    def _call_once(self, data_uri: str, text: str) -> tuple[dict, float]:
        body = {
            "messages": [
                {"role": "system", "content": self.system},       # 바이트 고정 (§3.1)
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},  # 이미지가 먼저
                    {"type": "text", "text": text},                          # 가변값은 뒤
                ]},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "trail", "schema": self.schema, "strict": True}},
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        return self._post(body)

    # ── 공개 API ─────────────────────────────────────────────────────────
    def assess(self, data_uri: str, *, heading: float | None = None) -> Verdict:
        """이미지 1장을 판정한다. 실패는 예외로 나간다 — 조용히 넘기지 않는다."""
        text = P.user_text(heading=heading)
        backoff = 1.0

        for attempt in range(4):
            try:
                payload, ms = self._call_once(data_uri, text)
                self._streak_500 = 0
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode("utf-8", "replace")
                if e.code == 400:
                    # 깨진 이미지. 재시도 금지 — 같은 바이트로 다시 물어봐야 같은 답이다.
                    raise VlmError(f"400 잘못된 이미지: {detail}") from e
                if e.code == 503:
                    time.sleep(backoff); backoff *= 2
                    self.stats.retries += 1
                    continue
                if e.code == 500:
                    self._streak_500 += 1
                    if self._streak_500 >= self.fatal_500_streak:
                        raise ServerDeadError(
                            f"500 이 {self._streak_500}회 연속. Metal OOM 좀비 상태로 보인다.\n"
                            f"  /health 는 200 을 돌려주지만 거짓이다. 재시작이 유일한 해결책:\n"
                            f"    docs/11-server-ops.md §5\n  마지막 응답: {detail}") from e
                    time.sleep(backoff); backoff *= 2
                    self.stats.retries += 1
                    continue
                raise VlmError(f"HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    raise VlmError(f"서버에 닿지 못했다: {e}") from e
                time.sleep(backoff); backoff *= 2
                self.stats.retries += 1
                continue

            verdict = self._parse(payload, ms)
            if verdict is not None:
                return verdict
            # JSON 파싱 실패 — 1회만 재시도한다 (§5.2)
            self.stats.parse_failures += 1
            self.stats.retries += 1

        raise VlmError("재시도 예산 소진")

    def _parse(self, payload: dict, ms: float) -> Verdict | None:
        usage = payload.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        # ⚠️ vLLM 은 이 키를 `null` 로 **명시해서** 보낸다. `.get(key, {})` 의
        # 기본값은 키가 **없을 때만** 먹으므로 None 이 그대로 나와 터진다.
        # llama.cpp 는 키를 아예 빼서 이 경로가 드러나지 않았다 (2026-08-22).
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

        # ── 이 순서가 중요하다: 내용을 보기 전에 이미지가 들어갔는지 먼저 본다.
        # 이미지가 무시되면 모델은 그럴듯한 JSON 을 만들어낸다. 파싱은 성공하고
        # 값은 순전한 환각이다. prompt_tokens 만이 이걸 잡아낸다 (§2.1).
        if pt < self.min_prompt_tokens:
            raise ImageIgnoredError(
                f"prompt_tokens={pt} (기대 {self.min_prompt_tokens}+). 이미지가 무시됐다.\n"
                f"  거의 확실히 WEBP 를 보냈다 — 서버는 200 을 주고 로그도 남기지 않는다.\n"
                f"  imaging.py 를 거치지 않은 data URI 가 있는지 확인할 것.")

        choice = payload["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise VlmError(f"finish_reason={choice.get('finish_reason')!r} — 출력이 잘렸다. "
                           f"thinking 모드가 켜졌는지 확인 (docs/11-server-ops.md §3.2)")

        try:
            obj = json.loads(choice["message"]["content"])
        except (json.JSONDecodeError, KeyError):
            return None

        # 런의 첫 호출은 캐시가 비어 있는 게 정상이다. 그걸 경고로 세면 매 런마다
        # 경고가 한 줄 뜨고, 사람은 곧 경고를 무시하게 된다. 둘째 호출부터 센다.
        if cached == 0 and self.stats.calls > 0:
            self.stats.cache_misses += 1
        self.stats.calls += 1
        self.stats.total_ms += ms

        # 본문 값은 재검증하지 않는다 — §3.1 의 strict json_schema 를 서버가
        # 강제한다는 계약 위에서만 안전하다. is_trail 이 boolean 이 아니라
        # 문자열로 오면 bool("false") is True 라 판정이 조용히 뒤집힌다
        # (→ docs/10-client-guide.md §4.1).
        return Verdict(
            is_trail=bool(obj["is_trail"]),
            confidence=obj.get("confidence"),
            prompt_tokens=pt,
            cached_tokens=cached,
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=ms,
            raw=payload.get("timings", {}),
        )

    def warmup(self, data_uri: str) -> None:
        """콜드 스타트 완화. 유휴 뒤 첫 요청이 12초까지 튄 사례가 있다 (§7).

        결과는 버린다. 측정에 섞이면 안 된다.
        """
        with contextlib.suppress(Exception):
            self._call_once(data_uri, "warmup")
