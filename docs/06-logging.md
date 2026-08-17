# 로깅 설계

관련: `05-observability.md`(무엇이 측정 가능한가) → 이 문서(어떻게 기록할 것인가)

---

## 1. 결론 먼저

**게이트웨이가 요청당 JSON 한 줄(JSONL)을 남기는 것이 주력이다.**
Prometheus/Grafana는 지금 세우지 않는다.

근거:

| 사실 | 함의 |
|---|---|
| 최대 처리량 ~0.5 req/s (하루 ~4만 건) | **샘플링 불필요, 전량 기록 가능** |
| 단일 노드·단일 프로세스 | 분산 트레이싱 불필요. 구간 타이밍을 레코드에 평평하게 넣으면 그게 trace |
| 설정을 계속 스윕할 예정 | 라벨/차원이 자유로워야 함 → 메트릭보다 구조화 로그가 적합 |
| 벤치와 운영이 같은 질문에 답함 | 같은 스키마를 쓰면 `bench/results`와 운영 로그를 한 테이블에서 비교 |

Prometheus 형식은 **나중에 필요해질 때** 같은 데이터에서 뽑아 쓰면 된다.
지금 세우는 것은 과잉이다.

---

## 2. llama-server가 주는 것 (`--metrics`)

```
llamacpp:prompt_tokens_total            counter  (캐시 제외)
llamacpp:prompt_tokens_cached_total     counter  ← 캐시 적중률
llamacpp:prompt_seconds_total           counter
llamacpp:tokens_predicted_total         counter
llamacpp:tokens_predicted_seconds_total counter
llamacpp:n_decode_total                 counter
llamacpp:n_tokens_max                   counter
llamacpp:requests_processing            gauge    ← 큐 깊이
llamacpp:requests_deferred              gauge    ← 대기 중
llamacpp:n_busy_slots_per_decode        gauge
llamacpp:spec_decode_*                  counter  (speculative 미사용)
```

`--slots`(기본 활성)로 `/slots`에서 슬롯별 상태도 볼 수 있다.

### 부족한 점 — 게이트웨이가 메워야 하는 것

1. **histogram이 없다.** counter/gauge뿐이라 p50/p95/p99를 못 낸다.
2. **누적 평균 gauge는 알람에 못 쓴다.** `prompt_tokens_seconds`는 부팅 이후 평균이다.
   Prometheus를 쓴다면 `rate(prompt_tokens_total[5m]) / rate(prompt_seconds_total[5m])`.
3. **에러 카운터가 없다.** `05-observability.md` §7의 Metal OOM 좀비 상태는
   메트릭에 **전혀 나타나지 않는다** (`requests_processing`=0, 에러 미집계).
4. **라벨이 없다.** 토큰 예산·스키마·이미지 크기별 분해 불가. 설정 스윕에 치명적.
5. **비전 인코딩 시간 없음.** (`05-observability.md` §1)

> 요약: llama-server 메트릭은 **"서버가 살아서 뭔가 하고 있다"** 수준의 정보다.
> 성능 분석과 회귀 탐지는 게이트웨이 로그가 담당해야 한다.

---

## 3. 필드명은 OpenTelemetry GenAI 규약에 맞춘다

새로 발명하지 않는다. 표준 도구에 나중에 꽂을 때 이름을 안 바꿔도 되도록.

| OTel GenAI | 이 프로젝트 |
|---|---|
| `gen_ai.operation.name` | `"chat"` |
| `gen_ai.request.model` | GGUF 파일명 |
| `gen_ai.usage.input_tokens` | `prompt_n + cache_n` |
| `gen_ai.usage.output_tokens` | `predicted_n` |
| `gen_ai.response.finish_reasons` | `["stop"]` / `["length"]` |

나머지 프로젝트 고유 필드는 `vlm.*` 네임스페이스로 둔다.

---

## 4. 요청 레코드 스키마 (JSONL, 한 줄 = 한 요청)

```jsonc
{
  "ts": "2026-08-17T12:41:03.221Z",
  "request_id": "…",                  // 클라이언트가 주면 그대로, 없으면 생성
  "client_id": "…",                   // 상관관계 추적용

  // ── 결과 ────────────────────────────────────────
  "status": 200,
  "gen_ai.response.finish_reasons": ["stop"],
  "schema_valid": true,               // 응답이 스키마를 통과했는가

  // ── 토큰 ────────────────────────────────────────
  "gen_ai.usage.input_tokens": 347,
  "gen_ai.usage.output_tokens": 17,
  "vlm.tokens.prompt_computed": 281,  // timings.prompt_n (캐시 제외)
  "vlm.tokens.cached": 66,            // timings.cache_n
  "vlm.tokens.image": 266,            // §5 캘리브레이션으로 산출
  "vlm.tokens.text": 81,              // input - image

  // ── 시간 (ms) ───────────────────────────────────
  "vlm.time.e2e_ms": 2289,            // 게이트웨이 기준 wall clock
  "vlm.time.queue_ms": 0,             // 게이트웨이 큐 대기
  "vlm.time.prefill_ms": 1677,        // timings.prompt_ms
  "vlm.time.decode_ms": 612,          // timings.predicted_ms

  // ── 입력 (원본은 절대 기록하지 않음) ───────────────
  "vlm.image.sha256": "a3f…",         // 바이트 해시
  "vlm.image.bytes": 96421,
  "vlm.image.wh": [1024, 768],
  "vlm.prompt.system_sha256": "9c1…", // system turn 불변성 검증용
  "vlm.prompt.user_sha256": "44b…",

  // ── 설정 지문 (반드시 포함) ──────────────────────
  "cfg.build": "b10450-ece963f",   // = /props 의 build_info 를 그대로. configs/llama.pin 과 대조
  "cfg.model": "gemma-4-E4B-it-qat-q4_0",
  "cfg.image_budget": 280,
  "cfg.ubatch": 320,
  "cfg.n_parallel": 1,
  "cfg.reasoning": "off",
  "cfg.schema": "min"
}
```

### 설계 규칙

- **평평하게.** 중첩 금지. `jq` / DuckDB로 바로 열 수 있어야 한다.
- **요청 완료 시 1회 emit.** 시작/종료 두 줄로 나누지 않는다.
- **원본을 남기지 않는다.** 이미지 바이트, 프롬프트 전문 모두 금지.
  해시 + 크기 + 차원만. 디버깅용 표본은 별도 디렉터리에 소량만
  (llama.cpp의 `--log-prompts-dir`도 있으나 게이트웨이가 직접 관리하는 편이 낫다).
- **`cfg.*`를 매 레코드에 박는다.** 설정을 계속 스윕할 것이므로,
  이게 없으면 나중에 "이 숫자가 어느 설정에서 나왔는지" 복원 불가.
  **벤치 결과(`bench/results/`)도 같은 `cfg.*` 필드를 쓴다.**

---

## 5. `vlm.tokens.image` 산출

예산은 상한일 뿐 실제 토큰 수와 다르다 (`05-observability.md` §2).
예산 280 + 4:3 → 실제 266.

**클라이언트가 리사이즈를 담당하므로 종횡비를 하나로 고정하면 상수가 된다.**
기동 시 1회 캘리브레이션해 캐시한다:

```
1. 이미지 포함 요청 1건 → input_tokens_with
2. 동일 프롬프트, 이미지 없이 1건 → input_tokens_without
3. image_tokens = with - without    → 상수로 보관
```

종횡비가 여러 개면 (종횡비 → 토큰 수) 맵으로 관리한다.

---

## 6. 측정하지 말 것 — TTFT / ITL

LLM 관측 가이드가 항상 강조하는 TTFT(Time To First Token)와
ITL/TPOT(Inter-Token Latency)는 **스트리밍 UX 지표**다.

이 워크로드는 **비스트리밍 JSON 단발**이다. 사용자가 토큰이 흐르는 걸 보지 않는다.
TTFT는 사실상 `prefill_ms`와 같고, ITL은 아무도 체감하지 않는다.

**E2E 지연과 처리량(img/s)만 본다.** 카고컬트하지 않는다.

---

## 7. 알람 조건

`05-observability.md` §8에서 정리한 것 + 로깅 관점 보강:

| 신호 | 쿼리 | 의미 |
|---|---|---|
| **추론 500 + `/health` 200** | — | **Metal OOM 좀비. 즉시 프로세스 재시작** |
| `vlm.tokens.cached`가 0으로 급락 | 최근 N건 평균 | system turn에 가변값 혼입. 조용히 깨지는 버그 |
| `vlm.prompt.system_sha256` 값이 변함 | distinct count > 1 | 위와 같은 원인, 더 직접적인 탐지 |
| `finish_reasons == ["length"]` 급증 | | thinking 켜짐 또는 스키마 폭주 |
| `gen_ai.usage.output_tokens` 상승 | p95 | 스키마 토큰 예산 초과. decode ~37ms/token |
| `schema_valid == false` | | 문법 강제 실패 또는 `<unused49>` 류 버그 |
| `vlm.time.prefill_ms` 급증 | p95 | 콜드 스타트 또는 메모리 압박 (관측: 유휴 후 1.7s → 12.6s) |

### 라이브니스 프로브

**`/health`를 쓰지 않는다.** 200을 반환하면서 모든 추론이 실패하는 상태가 실재한다.
게다가 모델 로딩 중에도 `/health`는 200을 주고 `/metrics`는 503을 준다.

프로브는 **작은 이미지 + `max_tokens: 1` 실제 추론 요청**이어야 한다.
연속 N회 실패 시 자체 복구를 기다리지 말고 프로세스를 죽인다.

---

## 8. 저장과 분석

```
logs/requests-YYYY-MM-DD.jsonl     일자별 롤링
bench/results/*.json               같은 cfg.* 스키마
```

분석은 DuckDB로 JSONL을 직접 쿼리한다. 별도 파이프라인 불필요:

```sql
SELECT "cfg.image_budget", "cfg.schema",
       median("vlm.time.e2e_ms")  AS p50,
       quantile("vlm.time.e2e_ms", 0.95) AS p95,
       count(*) AS n
FROM read_json_auto('logs/*.jsonl')
GROUP BY 1, 2 ORDER BY 1, 2;
```

보존: 원본 JSONL 30일, 일별 집계는 무기한. 볼륨이 작아 압박이 없다.

---

## 9. 미결정

- 로그 라이브러리 선택 (`structlog` vs 표준 `logging` + JSON formatter). 무엇이든 무방.
- verbose(`-lv 10`) 모드의 성능 오버헤드 — **미측정**. 상시 사용은 권하지 않으며
  진단 세션에서만 켠다.
- 게이트웨이가 llama-server `/metrics`를 주기적으로 스크레이프해 JSONL에 합칠지 여부.
  요청당 `timings`가 더 풍부하므로 우선순위 낮음.
