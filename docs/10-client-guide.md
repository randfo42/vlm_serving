# 클라이언트 가이드 — VLM 판정 서비스 사용법

대상: 이 서비스를 호출하는 애플리케이션 개발자
관련: `00-design.md`(범위), `04-b1-results.md`(성능 실측)

---

## 0. 이 서비스가 하는 일 / 안 하는 일

```
보내는 것:  로드뷰 이미지 1장 + 약간의 동적 텍스트
받는 것:    구조화된 JSON 판정 1건
```

**stateless다.** 세션 없음, 히스토리 없음, 요청 간 의존 없음.
같은 요청을 두 번 보내면 두 번 다 처음부터 처리한다.

### 클라이언트가 책임지는 것

- 어디로 갈지 / 어떻게 이어갈지 (탐색 루프, 방향 결정, 종료 조건)
- 지도 API 연동, 로드뷰 수집, 위치 이동
- **이미지 리사이즈와 포맷 변환** (§2 — 규칙이 까다로우니 반드시 읽을 것)
- 판정 결과의 의미론 (필드가 무엇을 뜻하는지)

---

## 1. 엔드포인트

**OpenAI Chat Completions 호환**이다.

```
POST http://<host>:8080/v1/chat/completions
Content-Type: application/json
```

> 현재는 `llama-server`를 직접 호출한다. 게이트웨이가 붙어도 **같은 형태를 유지**하므로
> 클라이언트 코드는 바뀌지 않는다. 게이트웨이가 추가할 것: 큐잉, 프롬프트 템플릿 고정,
> 계측, 재시작 관리.

---

## 2. ⚠️ 이미지 전처리 규칙 (가장 중요)

전부 실측 기반이다. 어기면 조용히 느려지거나 조용히 실패한다.

### 2.1 포맷: JPEG 또는 PNG만. **WEBP 금지**

| 포맷 | 결과 |
|---|---|
| JPEG | ✅ 정상 |
| PNG | ✅ 정상 |
| **WEBP** | ❌ **HTTP 200인데 이미지가 통째로 무시된다** |

WEBP를 보내면 서버는 **에러를 내지 않는다.** 200을 반환하고, 모델은 이미지를 못 본 채
"이미지를 주세요"라고 답한다.

실측으로 확인한 성질:

- MIME을 `image/jpeg`로 **속여도 소용없다.** 서버가 내용을 스니핑하므로 동일하게 버려진다
- **서버 로그에 경고가 한 줄도 안 남는다**
- `prompt_tokens`가 텍스트 분량만 나온다 (예: 이미지 있을 때 289 → 없을 때 12)

**감지 방법은 `prompt_tokens`가 비정상적으로 작은 것뿐이다.**
클라이언트는 `prompt_tokens < 예상 이미지 토큰 수`를 어서션으로 걸어둘 것.

**절대 WEBP를 보내지 말 것.** 지도 API가 WEBP를 주면 클라이언트에서 JPEG로 변환한다.

### 2.2 전달 방식: base64 data URI만

```json
{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
```

**원격 URL을 넣지 말 것.** 서버가 직접 다운로드를 시도하다 실패한다
(`HTTP 500: Failed to download image`). 서버는 외부 네트워크에 나가면 안 된다.

### 2.3 종횡비를 하나로 고정하고, 리사이즈 목표 이상으로 보낼 것

서버는 종횡비를 보존한 채 토큰 예산(`--image-max-tokens`, 현재 280)에 맞춰
**다운스케일**한다. 실측 ground truth (verbose 로그의 실제 리사이즈 결과):

| 입력 | 종횡비 | 서버 리사이즈 결과 | 이미지 토큰 |
|---|---:|---|---:|
| 896×896 | 1:1 | 768×768 | **256** |
| 1280×720 | 16:9 | 1056×576 | **264** |
| 1024×768 | 4:3 | 912×672 | **266** |
| 2048×1536 | 4:3 | 912×672 | **266** |
| 640×480 | 4:3 | 624×480 | 130 |
| 320×240 | 4:3 | 336×240 | 35 |

두 가지가 읽힌다:

1. **리사이즈 목표 이상이면 입력 크기와 무관하게 토큰 수가 같다.**
   1024×768과 2048×1536이 둘 다 912×672 / 266토큰이다.
   → 종횡비만 고정하면 토큰 수는 상수가 된다.
2. **종횡비가 토큰 수를 결정한다.** 1:1은 256, 16:9는 264, 4:3은 266.

### 2.4 과대 이미지는 순수 낭비

4000×3000을 보내도 1024×768과 **토큰 수가 같다**. 대역폭과 서버 전처리만 낭비한다.
긴 변 **1024~1280px 정도**로 줄여 보내는 것으로 충분하다.

### 2.5 과소 이미지는 정보 손실

리사이즈 목표보다 작으면 다운스케일이 일어나지 않아 토큰 수가 급감한다
(640×480 → 130토큰). 빨라지긴 하지만 **모델이 보는 정보가 절반 이하**가 된다.

> 서버가 `--image-min-tokens`를 높게 잡으면 작은 이미지를 **업스케일**해 토큰 수를
> 채운다. 이 경우 비용은 정상이지만 정보량은 늘지 않아 순수 낭비이고,
> 무엇보다 **클라이언트의 실수가 은폐된다.** 현재 서버는 `min`을 낮게 두어
> 작은 이미지가 토큰 수로 드러나도록 설정한다 (`11-server-ops.md` §3.3).

**권장: 긴 변 1280px, 종횡비 고정.** 로드뷰가 가로형이면 `1280×720` (264토큰).
최종 값은 평가 세트로 정확도까지 보고 정한다.

### 2.5 요청당 이미지는 1장

2장 이상도 동작하지만(토큰이 배로 늘어남), **비전 인코딩은 배칭되지 않아 순차 처리**된다.
이득이 없다. 1장씩 나눠 보낼 것.

---

## 3. 요청 형태

### 3.1 메시지 구조 — 3존 레이아웃을 지킬 것

```json
{
  "messages": [
    {
      "role": "system",
      "content": "<<< 완전히 고정된 텍스트 >>>"
    },
    {
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
        {"type": "text", "text": "<<< 동적 텍스트 >>>"}
      ]
    }
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {"name": "trail", "schema": { /* ... */ }, "strict": true}
  },
  "max_tokens": 60,
  "temperature": 0
}
```

세 가지 규칙:

1. **`system` 내용은 바이트 단위로 항상 동일해야 한다.**
   타임스탬프, 요청 ID, 좌표, 순번을 **절대** 넣지 말 것.
   여기가 프리픽스 캐시 대상이고, 한 글자만 달라져도 캐시 적중률이 0이 된다.
   조용히 느려지므로 알아채기 어렵다.

2. **`user` 안에서는 이미지가 텍스트보다 먼저.**
   Google 공식 권장이며, causal decoder에서 이미지 뒤 텍스트만 이미지를 참조할 수 있다.

3. **동적 값은 전부 이미지 뒤 텍스트에.** 좌표, 진행 방향, 시도 횟수 등.

### 3.2 출력 스키마 — 토큰이 곧 시간이다

**decode는 약 37 ms/token이다.** 출력 토큰 하나하나가 지연이다.

실측:

| 스키마 | 출력 토큰 | decode 시간 |
|---|---:|---:|
| `is_trail`만 | 17 | **620 ms** |
| + `confidence`, `surface` | 44 | 1,678 ms |
| + `reason` (자유 텍스트) | 79 | **2,988 ms** |

**자유 텍스트 필드 하나가 요청당 1.3초다.**

- 사람이 읽을 설명(`reason`)이 정말 필요한지 재고할 것
- 필요하다면 `enum`으로 좁힐 것 (`["paved_path","dirt_trail","roadside","no_path"]`)
- 숫자는 정수 또는 소수 1자리로 제한

### 3.3 스트리밍을 쓰지 말 것

`"stream": true`를 쓰지 않는다. 출력이 짧은 단발 JSON이라 이득이 없고,
게이트웨이 큐잉과 계측만 복잡해진다.

---

## 4. 응답

```json
{
  "choices": [{
    "finish_reason": "stop",
    "message": {"role": "assistant", "content": "{\"is_trail\": true}"}
  }],
  "usage": {
    "prompt_tokens": 347,
    "completion_tokens": 17,
    "prompt_tokens_details": {"cached_tokens": 66}
  },
  "timings": {
    "prompt_n": 281, "prompt_ms": 1676.82,
    "predicted_n": 17, "predicted_ms": 612.36,
    "cache_n": 66
  }
}
```

`message.content`는 **문자열**이다. JSON으로 한 번 더 파싱해야 한다.

### 반드시 확인할 것

| 확인 | 의미 |
|---|---|
| `finish_reason == "stop"` | 정상. `"length"`면 잘렸다 — §6 참조 |
| `content`를 JSON 파싱 성공 | 실패 시 재시도 대상 |
| `usage.prompt_tokens_details.cached_tokens > 0` | 0이면 프리픽스 캐시가 깨졌다는 신호 |

---

## 5. 에러 처리

| 상황 | HTTP | 응답 | 클라이언트 대응 |
|---|---|---|---|
| 깨진 이미지 | **400** | `Failed to load image or audio file` | **재시도 금지.** 이미지를 다시 받아올 것 |
| 원격 URL 사용 | **500** | `Failed to download image` | **재시도 금지.** base64로 보낼 것 (§2.2) |
| 서버 백엔드 사망 | **500** | `Compute error.` | §5.1 |
| 모델 로딩 중 | **503** | `Loading model` | 백오프 후 재시도 |

### 5.1 ⚠️ 500이 반복되면 재시도하지 말 것

Metal OOM이 나면 서버 백엔드가 **영구히 죽는다.** 그런데 프로세스는 살아 있고
`/health`는 200을 반환한다. 이 상태에서는 **모든 요청이 500**이다 (텍스트 전용 요청조차).

**자체 복구되지 않는다.** 재시도는 무의미하다.

클라이언트는 **500이 연속 3회 나오면 재시도를 멈추고 운영자에게 알린다.**
서버 재시작이 유일한 해결책이다 (`11-server-ops.md` §5).

### 5.2 재시도 정책 요약

```
400        → 재시도 금지 (입력 문제)
503        → 지수 백오프 재시도 (로딩 중)
500 1~2회  → 짧은 백오프 후 재시도
500 3회 연속 → 중단 + 알람. 서버가 죽었을 가능성
JSON 파싱 실패 → 1회 재시도, 그래도 실패하면 기록하고 넘어감
```

---

## 6. 조용히 깨지는 것들 — 클라이언트가 감시할 신호

이 서비스의 실패는 대부분 **에러 없이** 일어난다. 다음을 계측할 것:

| 신호 | 의미 | 조치 |
|---|---|---|
| **`prompt_tokens`가 예상보다 250 이상 작음** | **이미지가 무시됨** (§2.1). 가장 확실한 탐지 | 포맷 확인 |
| 응답에 "이미지를 주세요" 류 문구 | 위와 같은 원인 | 포맷 확인 |
| `cached_tokens`가 0 | system turn에 가변값 혼입 (§3.1) | 프롬프트 생성 코드 점검 |
| `finish_reason == "length"` | 출력이 잘림. thinking 모드 의심 | 운영자에게 보고 |
| `completion_tokens`가 평소보다 큼 | 스키마가 커졌거나 모델이 장황해짐 | 스키마 점검 |
| 지연이 갑자기 5배 | 콜드 스타트 또는 메모리 압박 | §7 |

---

## 7. 성능 기대치

실측 (M4/16GB, 예산 280, `-ub 320`, `-np 1`, tiny 스키마):

| 지표 | 값 |
|---|---|
| 요청당 지연 | **약 2.1 초** |
| 처리량 | **약 0.47 img/s** (시간당 ~1,700장) |
| prefill | ~1.5 초 |
| decode | ~37 ms/token |

**대략적인 계산식:**
```
지연(ms) ≈ 1500 + 37 × 출력토큰수
```

### 동시 요청을 늘려도 거의 안 빨라진다

비전 인코딩이 원자적·직렬이라 GPU를 독점한다. 실측:

| 동시 요청 | 처리량 | p50 지연 |
|---:|---:|---:|
| 1 | 0.42 img/s | 2.4 s |
| 4 | 0.51 img/s (**+20%**) | 7.9 s (**3.3배**) |

**클라이언트는 동시 요청을 4개 이상 띄우지 말 것.** 처리량은 안 오르고 지연만 나빠진다.
throughput이 필요하면 요청을 큐에 넣고 순차 처리하는 편이 예측 가능하다.

### 콜드 스타트

유휴 상태가 지속된 뒤 첫 요청은 **1.7초 → 12.6초**로 튄 사례가 있다.
지연에 민감하다면 주기적으로 워밍업 요청(작은 이미지, `max_tokens: 1`)을 보낸다.

---

## 8. 최소 예제

```python
import base64, io, json, urllib.request
from PIL import Image

URL = "http://127.0.0.1:8080/v1/chat/completions"
TARGET = (1280, 720)          # 종횡비 고정 + 리사이즈 목표 이상 (§2.3). → 264 이미지 토큰
EXPECTED_IMAGE_TOKENS = 264   # §2.3 실측값. 어서션용

SYSTEM = "You are a walking-trail assessor. ..."   # 절대 변하지 않음 (§3.1)

SCHEMA = {
    "type": "object",
    "properties": {
        "is_trail": {"type": "boolean"},
        "surface": {"type": "string",
                    "enum": ["paved", "dirt", "gravel", "none"]},   # enum 으로 좁힘 (§3.2)
    },
    "required": ["is_trail", "surface"],
    "additionalProperties": False,
}


def to_data_uri(raw: bytes) -> str:
    """항상 JPEG로, 항상 고정 해상도로. WEBP 금지 (§2.1)."""
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize(TARGET, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def assess(raw_image: bytes, context: str) -> dict:
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": to_data_uri(raw_image)}},
                {"type": "text", "text": context},      # 동적 값은 이미지 뒤 (§3.1)
            ]},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "trail", "schema": SCHEMA, "strict": True}},
        "max_tokens": 60,
        "temperature": 0,
    }
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=120))

    if resp["choices"][0]["finish_reason"] != "stop":
        raise RuntimeError("출력이 잘림 — 운영자에게 보고")           # §6
    if resp["usage"]["prompt_tokens_details"]["cached_tokens"] == 0:
        pass  # 경고 로깅: system turn 이 변했을 수 있음 (§3.1)

    return json.loads(resp["choices"][0]["message"]["content"])
```

---

## 9. 체크리스트

호출 코드를 작성했다면 다음을 확인한다.

- [ ] 이미지가 항상 **JPEG 또는 PNG** (WEBP 아님)
- [ ] 이미지가 항상 **동일한 종횡비**이고, 긴 변이 1024px 이상
- [ ] **base64 data URI**로 전달 (원격 URL 아님)
- [ ] 요청당 이미지 **1장**
- [ ] `system` 내용에 가변값이 **전혀** 없음 — 테스트로 검증할 것
- [ ] 이미지가 텍스트보다 **먼저**
- [ ] 출력 스키마에 자유 텍스트 필드가 없거나, 필요성을 검토함
- [ ] `finish_reason`을 확인함
- [ ] `cached_tokens == 0`을 경고로 계측함
- [ ] 500 연속 3회 시 재시도를 **중단**함
- [ ] 동시 요청 4개 이하
