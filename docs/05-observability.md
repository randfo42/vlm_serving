# 관측 — 무엇이 기록되고, 무엇이 기록되지 않는가

측정: 2026-08-17, llama.cpp b10450, gemma-4-E4B-it-qat-q4_0, M4/16GB
관련: `02-open-questions.md` §7 "관측" 항목

---

## 1. 기본 설정에서 기록되는 것 — 2분할뿐

### API 응답 (`/v1/chat/completions`)

```json
"timings": {
  "cache_n": 66,                      // 프리픽스 캐시 적중 토큰
  "prompt_n": 281,                    // prefill 계산한 토큰 (캐시분 제외)
  "prompt_ms": 1676.82,
  "prompt_per_token_ms": 5.97,
  "prompt_per_second": 167.6,
  "predicted_n": 17,                  // decode 토큰
  "predicted_ms": 612.36,
  "predicted_per_token_ms": 36.02,
  "predicted_per_second": 27.8
},
"usage": {
  "prompt_tokens": 281, "completion_tokens": 17, "total_tokens": 298,
  "prompt_tokens_details": {"cached_tokens": 66}
}
```

### 서버 로그 (기본 verbosity) — 같은 2분할

```
slot print_timing: id 0 | task 84 | prompt eval time = 1676.82 ms / 281 tokens (5.97 ms/tok, 167.6 tok/s)
slot print_timing: id 0 | task 84 |        eval time =  612.36 ms /  17 tokens (36.0 ms/tok,  27.8 tok/s)
slot print_timing: id 0 | task 84 |       total time = 2289.18 ms / 298 tokens
slot print_timing: id 0 | task 84 |    graphs reused = 76
```

### 기본 설정에서 **기록되지 않는** 것

| 원하는 값 | 기본 제공 | 비고 |
|---|---|---|
| 비전 인코딩 시간 | ❌ | `prompt_ms` 안에 섞여 있음 |
| 이미지 토큰 수 | ❌ | `prompt_n`은 이미지+텍스트 합계 |
| 텍스트 토큰 수 | ❌ | 위와 같음 |
| 리사이즈 후 해상도 | ❌ | |

> `00-design.md`가 전제한 `t_vision / t_prefill / t_decode` 3분할은
> **기본 설정으로는 얻을 수 없다.** prefill과 decode 2분할만 나온다.

---

## 2. 이미지 토큰 수는 뺄셈으로 구할 수 있다

`--image-min-tokens = --image-max-tokens = N`으로 고정해도
**실제 이미지 토큰 수는 N이 아니다.**

예산 280, 원본 1024×768 (4:3):

| 값 | 결과 |
|---|---|
| 리사이즈 후 해상도 | **912 × 672** |
| 실제 이미지 토큰 | **266** (280 아님) |

Gemma 4의 리사이즈는 종횡비를 보존한 채 pooled patch size 배수로 **내림**하므로,
예산은 **상한(cap)**이지 정확한 값이 아니다. 종횡비마다 다른 값이 나온다.

**구하는 법**: 같은 프롬프트를 이미지 없이 한 번 보내 텍스트 토큰 수를 재고 빼면 된다.

```
이미지 있음: prompt_tokens = 298
이미지 없음: prompt_tokens =  30
                    이미지 = 268
```

게이트웨이는 종횡비별로 이 값을 1회 캘리브레이션해 캐시해두면 된다.
**클라이언트가 리사이즈를 담당하므로 종횡비를 하나로 고정하면 이 값도 상수가 된다.**

---

## 3. `-lv 10` (verbose)이면 전체 분해가 나온다

```
D image_tokens->nx = 266                                        ← 이미지 토큰 수
D mtmd_batch_encode_impl: encoding batch with 1 entries and total 266 tokens
D clip_encode: copying image 1/1 to input buffer (nx=912, ny=672) ← 리사이즈 결과
D clip_encode: output embedding shape [2560, 266, 1]
I decoding image batch 1/1, n_tokens_batch = 266
I image decoded (batch 1/1) in 84 ms
D sched_reserve: MTL0 compute buffer size = 454.91 MiB
D sched_reserve: CPU  compute buffer size = 166.09 MiB
D sched_reserve: reserve took 79.16 ms
D set_causal_attn: value = 0 / 1                                ← 비전↔텍스트 전환
```

타임스탬프 형식은 **`분.초.밀리.마이크로`** (`0.14.851.601` = 14.8516초).
파싱: `분*60000 + 초*1000 + 밀리 + 마이크로/1000`

---

## 4. 실측 분해 — `prompt eval time` 1677 ms의 정체

단독 실행, 예산 280, `-ub 2048`, 워밍업 후 중앙값:

| 구간 | 시간 | 비중 |
|---|---:|---:|
| 슬롯 셋업 · 컨텍스트 체크포인트 복원 | 33 ms | 2% |
| 이미지 전처리 (리사이즈) | 2 ms | 0% |
| **clip_encode (비전 타워)** | **508 ms** | **30%** |
| `sched_reserve` ① (non-causal 전환 후) | 79 ms | 5% |
| 이미지 토큰 KV 삽입 | 84 ms | 5% |
| **`sched_reserve` ② (causal 복귀 후)** | **851 ms** | **51%** |
| context checkpoint 생성 | 113 ms | 7% |
| 실제 텍스트 prefill | 79 ms | 5% |

### 가장 중요한 발견: prefill의 절반 이상이 연산이 아니다

```
544.2  set_causal_attn: value = 0     ← 비전용 non-causal 전환 → 그래프 재예약 (79ms)
628.0  set_causal_attn: value = 1     ← 텍스트용 causal 복귀   → 그래프 재예약 (851ms)
```

**요청마다 compute graph를 두 번 재예약한다.** 합계 ~930 ms, prefill의 **55%**.

이것이 `03-vision-encoding-constraint.md` 제약 (A)의 실제 비용이다.
"이미지를 쪼갤 수 없다"에 그치지 않고, **non-causal↔causal 전환이
그래프 재구성을 강제**한다. clip_encode(508ms)보다 큰 비용이다.

재예약은 항상 `worst-case: n_tokens = <ubatch>` 기준으로 잡는다.

---

## 5. `-ub` 튜닝 — 작지만 공짜인 이득

예산 280(실제 266토큰) 고정, 이미지 7장 중앙값:

| `-ub` | prefill | total | img/s |
|---:|---:|---:|---:|
| **320** | **1508 ms** | 2.13 s | **0.47** |
| 512 | 1543 ms | 2.17 s | 0.46 |
| 1024 | 1603 ms | 2.23 s | 0.45 |
| 2048 | 1669 ms | 2.29 s | 0.44 |

**`-ub`를 이미지 토큰 수 바로 위로 낮추면 +7%.** 공짜이므로 취한다.

다만 §4의 851ms 공백은 ubatch에 비례하지 않는다 —
"재예약 비용이 `-ub`에 비례한다"는 가설은 **부분적으로만 맞았다.**

> ⚠️ `-ub`는 이미지 토큰 수보다 반드시 커야 한다. 작으면 **하드 크래시**
> (`GGML_ASSERT: non-causal attention requires n_ubatch >= n_tokens`).
> 예산이 상한일 뿐이라 실제 토큰 수가 종횡비에 따라 변하므로 **여유를 둘 것.**

---

## 6. 메모리 실측치

| 항목 | 값 |
|---|---|
| MTL0 compute buffer (`-ub 2048`) | **454.91 MiB** |
| CPU compute buffer | **166.09 MiB** |
| Metal 총 예산 | 12,124 MiB |
| 프로세스 RSS | 3.99 GB (대부분 mmap clean) |

조사 단계에서 못 찾아 "0.5~1.5GB 추정"으로 뒀던 compute buffer의 실제 값.

---

## 7. ⚠️ 치명적 실패 모드: `/health`가 거짓말한다

Metal OOM이 나면 백엔드가 영구히 죽지만 **프로세스와 헬스체크는 살아 있다.**

```
E ggml_metal_graph_compute: backend is in error state from a previous
  command buffer failure - recreate the backend to recover
E error: Insufficient Memory (kIOGPUCommandBufferCallbackErrorOutOfMemory)
```

이 상태에서 실측:

| 엔드포인트 | 응답 |
|---|---|
| `GET /health` | **HTTP 200** ✅ |
| `GET /v1/models` | **HTTP 200** ✅ |
| `POST /v1/chat/completions` | **HTTP 500** ❌ (텍스트 전용 요청도 실패) |

**Metal 백엔드는 OOM에서 자체 복구하지 않는다.** 재시작만이 답이다.

### 게이트웨이 요구사항

1. **`/health`를 신뢰하지 말 것.** 라이브니스 프로브는 **실제 추론 요청**이어야 한다
   (작은 이미지 + 1토큰 출력).
2. 500이 연속 N회 나오면 **프로세스를 죽이고 재시작**한다. 자체 복구를 기다리지 말 것.
3. 로그에서 `kIOGPUCommandBufferCallbackErrorOutOfMemory` /
   `backend is in error state`를 감시해 즉시 알람.

---

## 8. 게이트웨이 계측 설계

### 상시 수집 (기본 verbosity로 충분)

API `timings`를 그대로 요청 로그에 남긴다:
`prompt_n`, `prompt_ms`, `predicted_n`, `predicted_ms`, `cache_n`

파생 지표:
- `image_tokens` = §2 캘리브레이션 상수 (종횡비 고정 시)
- `text_tokens` = `prompt_n + cache_n - image_tokens`
- `cache_hit_rate` = `cache_n / (prompt_n + cache_n)` — **system turn 바이트 동일성 회귀 탐지용**
- `output_tokens` = `predicted_n` — **스키마 토큰 예산 초과 감시** (`04-b1-results.md` §3)

### 진단 시에만 (`-lv 10`)

`clip_encode` 구간, 재예약 비용, 실제 해상도/토큰 수.

```bash
pkill -f llama-server
./configs/smoke.sh -lv 10 2>&1 | tee /tmp/llama-verbose.log
```

(`configs/smoke.sh`는 여분 인자를 `llama-server`에 그대로 넘긴다.)

**verbose 상시 사용은 권하지 않는다** — 요청당 로그 수백 줄이고,
성능 영향은 미측정이다. 진단 세션에서만 켠다.

### 알람 걸 것

| 신호 | 의미 |
|---|---|
| `cache_n`이 0으로 떨어짐 | system turn에 가변값이 섞임. 조용히 깨지는 버그 |
| `predicted_n`이 예산 초과 | 스키마가 커졌거나 thinking이 켜짐 |
| `finish_reason == "length"` | thinking 모드 의심 (`04-b1-results.md` §1) |
| 추론 500 + `/health` 200 | **Metal OOM. 즉시 재시작** |
| `prompt_ms` 급증 | 콜드 스타트 또는 메모리 압박 (관측: 유휴 후 첫 요청 1.7s → 12.6s) |
