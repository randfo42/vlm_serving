# 서버 운영 가이드

대상: 이 서비스를 띄우고 관리하는 사람
관련: `05-observability.md`(계측), `06-logging.md`(로깅), `04-b1-results.md`(성능 근거)

---

## 1. 구성 요소

| 요소 | 현재 |
|---|---|
| 추론 엔진 | `llama-server` — **소스 빌드**, `vendor/llama.cpp/build/bin/llama-server` |
| 버전 고정 | `configs/llama.pin` → **b10450**, commit `ece963f41` |
| 빌드 스크립트 | `scripts/build-llama.sh` |
| 모델 | `models/gemma-4-E4B-qat/gemma-4-E4B_q4_0-it.gguf` (4.8 GB) |
| 비전 프로젝터 | `models/gemma-4-E4B-qat/gemma-4-E4B-it-mmproj.gguf` (946 MB) |
| 백엔드 | Metal (Apple M4, 10코어 GPU) |
| 실행 스크립트 | `configs/smoke.sh` (경로 해석은 `configs/env.sh`) |

### 왜 소스 빌드인가

처음에는 Homebrew 바이너리(`brew install llama.cpp`)를 썼다. **버전을 핀할 수 없어서 버렸다.**

`docs/04-b1-results.md`의 모든 수치 — 프리필 계수 5.7 ms/토큰, decode 37 ms/토큰,
`-ub` 튜닝 +7%, 구간 분해 851 ms `sched_reserve` — 는 전부 b10450 한 커밋에서 나온 값이다.
brew는 무관한 `brew upgrade` 한 번에 엔진을 갈아치우고, **그 사실이 아무 데도 안 남는다.**
성능이 바뀌어도 원인을 모르고, 기준선 문서를 신뢰할 수 없게 된다.

소스 빌드로 얻는 것:

| | brew | 소스 빌드 |
|---|---|---|
| 커밋 고정 | ❌ 불가 | ✅ `configs/llama.pin` |
| 빌드 플래그 통제 | ❌ 수식대로 | ✅ `LLAMA_CURL=OFF` 등 |
| 업그레이드 시점 | 남이 정함 | 우리가 정함 (§8) |
| 패치 적용 | ❌ | ✅ (그래프 재예약 TODO 같은 조사에 필요) |
| 디스크 | 없음 | 소스 206 MB + 빌드 174 MB |

> **Xcode 전체는 필요 없다.** 이 머신에는 Command Line Tools만 있고 `xcrun metal`이 없지만,
> `-DGGML_METAL_EMBED_LIBRARY=ON`이 셰이더를 바이너리에 임베드하고 런타임에 컴파일한다.
> 이 플래그를 빼면 실행 시 `default.metallib`를 못 찾고 **조용히 CPU로 폴백한다** (에러 없음, 그냥 느려짐).

### 1.1 최초 셋업 / 빌드

```bash
./scripts/build-llama.sh          # clone(핀 커밋) + cmake + build
./scripts/build-llama.sh --clean  # build/ 지우고 처음부터
```

멱등하다. 이미 핀 커밋으로 빌드돼 있으면 증분 빌드만 돈다.
M4 10코어에서 클린 빌드 약 3분.

체크아웃이 핀과 다르면 스크립트가 **멈춘다.** 측정치와 바이너리가 어긋난 채로
벤치를 돌리는 게 최악이기 때문이다.

빌드 검증:

```bash
vendor/llama.cpp/build/bin/llama-server --version
# version: 0.1.0-dev (build 10450, commit ece963f)   ← 이 두 값이 llama.pin 과 같아야 한다
```

> `build 10450`이 나오는 이유: shallow clone은 커밋이 1개라 llama.cpp의
> `git rev-list --count HEAD` 기반 빌드 번호가 항상 1이 된다.
> `scripts/build-llama.sh`가 `-DLLAMA_BUILD_NUMBER`를 태그에서 직접 주입해 바로잡는다.
> 안 그러면 `/props`의 `build_info`가 `1-ece963f`가 되어 버전 확인(§4)이 무의미해진다.

`LLAMA_CURL=OFF`로 빌드한다. `-hf` 원격 모델 다운로드와 **원격 이미지 URL 로드가 없다.**
우리는 로컬 GGUF + base64 data URI만 쓰므로(`10-client-guide.md` §2.2) 잃는 게 없고,
서버가 외부로 나가는 경로가 아예 사라진다.

---

## 2. 기동 / 정지

```bash
# 기동  (내부적으로 vendor/ 빌드 바이너리를 쓴다. PATH 를 보지 않는다)
./configs/smoke.sh

# 정지
pkill -f llama-server

# 상태
pgrep -f llama-server && curl -s localhost:8080/metrics | head -3
```

> **`llama-server`를 PATH에서 직접 실행하지 말 것.** brew판이 아직 깔려 있으면
> 그게 잡힌다. 실행 중인 바이너리 확인:
> ```bash
> ps -o command= -p "$(pgrep -f llama-server)" | awk '{print $1}'
> # → .../vlm_serving/vendor/llama.cpp/build/bin/llama-server 여야 한다
> ```
> brew판이 남아 있어 헷갈린다면 `brew uninstall llama.cpp`로 치운다.

### 기동 완료 판정

**`/health`를 쓰지 말 것.** 모델 로딩 중에도 200을 반환한다.

```bash
# 잘못됨 — 로딩 중에도 통과
until curl -s localhost:8080/health >/dev/null; do sleep 2; done

# 올바름 — /metrics 는 로딩 중 503 을 준다
until curl -s localhost:8080/metrics 2>/dev/null | grep -q '^#'; do sleep 3; done
```

기동 시간: **콜드 11초 / 웜 2.7초** (페이지 캐시에 모델이 남아 있을 때).

---

## 3. 설정 레퍼런스

`configs/smoke.sh`의 각 값과 근거. **바꾸기 전에 근거를 읽을 것.**

| 플래그 | 값 | 근거 |
|---|---|---|
| `-ngl 99` | 전 레이어 GPU | Metal 예산 12,124 MiB에 여유 있음 |
| `--ctx-size 8192` | | 1턴 워크로드엔 과분하나 KV가 0.25GB뿐이라 무해 |
| `--parallel 1` | **1** | 실측: `-np 4`는 처리량 +20%에 지연 3.3배. 슬롯당 KV 사전할당도 낭비 |
| `--image-max-tokens` | **280** | 모델 기본값. 장면 판단은 저예산에서 거의 무손실 |
| `--image-min-tokens` | **1** | §3.3 — max와 같게 두면 안 된다 |
| `--ubatch-size` / `--batch-size` | **320** | §3.1 — **잘못 만지면 하드 크래시** |
| `--cache-ram 0` | **끔** | 기본 8192 MiB로 켜져 있음. stateless엔 무용하고 이미지 캐싱 OOM 이슈 존재 |
| `--reasoning off` + `--reasoning-budget 0` | **필수** | §3.2 |
| `--metrics` | 켬 | Prometheus 엔드포인트 |
| `--swa-full` | **안 씀** | 8K에서 0.25→0.77GB로 싸지만 stateless엔 이득 없음 |
| `-ctk/-ctv` | **안 씀** | KV가 0.25GB뿐. Metal에서 K/V 타입 혼용 시 실패 이슈(#21450) |

### 3.1 ⚠️ `-ub`는 이미지 토큰 수보다 커야 한다 — 아니면 크래시

비전 인코더는 non-causal attention이라 이미지 토큰 전체가 단일 ubatch에 들어가야 한다.
부족하면 **친절한 에러가 아니라 프로세스가 죽는다**:

```
GGML_ASSERT: (cparams.causal_attn || cparams.n_ubatch >= n_tokens_all)
  "non-causal attention requires n_ubatch >= n_tokens" failed  → SIGABRT
```

사전 검증이 없다 (issue #21461, #21550 둘 다 "not planned").

**그리고 실제 이미지 토큰 수는 예산과 다르다.** 예산 280에서 종횡비에 따라
1:1 → 256, 16:9 → 264, 4:3 → 266 (`10-client-guide.md` §2.3 실측표).
`--image-max-tokens`를 올리면 `-ub`도 반드시 함께 올릴 것.

```
안전 규칙:  -ub ≥ 예산 × 1.15
현재:      예산 280 → -ub 320
예산 560 → -ub 640 / 예산 1120 → -ub 1280
```

`-ub` 튜닝 효과 (예산 280 고정):

| `-ub` | prefill | img/s |
|---:|---:|---:|
| **320** | **1508 ms** | **0.47** |
| 512 | 1543 ms | 0.46 |
| 1024 | 1603 ms | 0.45 |
| 2048 | 1669 ms | 0.44 |

### 3.2 ⚠️ thinking 모드를 반드시 끌 것

**Gemma 4는 thinking이 기본 켜짐이다.** 끄지 않으면:

- 출력이 `message.content`가 아니라 `message.reasoning_content`로 간다
- `content`는 **빈 문자열**이 된다
- 추론에 수백 토큰을 태운다 → decode 37 ms/token → **요청당 수 초 추가**
- `finish_reason`이 `"length"`가 된다

`--reasoning off --reasoning-budget 0`. 절대 빼지 말 것.

### 3.3 `--image-min-tokens`를 max와 같게 두지 말 것

`min`은 **작은 이미지를 업스케일해 토큰 수를 채우는** 하한이다.
`min = max = 280`으로 두면 리사이즈 목표보다 작은 입력이 강제 확대된다.

실측 (verbose 로그의 실제 리사이즈 결과):

| 입력 | `min=280 max=280` | `min=1 max=280` |
|---|---|---|
| 640×480 | **960×720으로 확대** → 300토큰 | 624×480 → **130토큰** |
| 320×240 | **960×720으로 확대** → 300토큰 | 336×240 → **35토큰** |
| 1024×768 | 912×672 → 266토큰 | 912×672 → 266토큰 |
| 2048×1536 | 912×672 → 266토큰 | 912×672 → 266토큰 |

리사이즈 목표 이상인 정상 입력에는 **아무 차이가 없다.** 차이는 과소 입력에서만 난다.

**`min=1`을 쓰는 이유는 성능이 아니라 관측성이다:**

| | `min=max=280` | `min=1` |
|---|---|---|
| 정상 입력 | 동일 | 동일 |
| 과소 입력의 비용 | 정상가 (업스케일에 낭비) | 저렴 |
| 과소 입력의 정보량 | 늘지 않음 (확대일 뿐) | 동일 |
| **클라이언트 실수 탐지** | **은폐됨** | **`image_tokens` 급감으로 드러남** |

업스케일은 정보를 만들어내지 않는다. 비용만 정상가로 맞춰 **오류를 감출 뿐**이다.
`min=1`로 두면 게이트웨이가 `vlm.tokens.image`로 과소 입력을 탐지할 수 있다
(`06-logging.md` §7 알람).

---

## 4. 헬스 체크 — `/health`를 신뢰하지 말 것

실측된 `/health`의 거짓 양성 두 가지:

| 상황 | `/health` | 실제 |
|---|---|---|
| 모델 로딩 중 | **200** | 추론 불가 (`/metrics`는 503) |
| Metal OOM 후 | **200** | **모든 추론이 500** |

### 올바른 라이브니스 프로브

작은 이미지 + `max_tokens: 1`의 **실제 추론 요청**이어야 한다.
텍스트 전용 프로브로도 OOM 상태는 잡히지만(텍스트 요청도 500), 비전 경로는 못 본다.

```bash
probe() {
  curl -sf -m 30 -X POST localhost:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"messages\":[{\"role\":\"user\",\"content\":[
         {\"type\":\"image_url\",\"image_url\":{\"url\":\"$TINY_JPEG_DATA_URI\"}},
         {\"type\":\"text\",\"text\":\"x\"}]}],\"max_tokens\":1}" >/dev/null
}
```

### 실행 중인 엔진 버전 확인

`/props`의 `build_info`가 `configs/llama.pin`과 일치해야 한다.
이 값은 `06-logging.md`의 `cfg.build` 필드와 같은 문자열이다.

```bash
curl -s localhost:8080/props | jq -r .build_info
# → b10450-ece963f
```

불일치하면 **brew판이나 옛 빌드가 떠 있는 것이다.** §2의 경고를 볼 것.

---

## 5. 🔴 최우선 실패 모드: Metal OOM 좀비

### 증상

```
E ggml_metal_graph_compute: backend is in error state from a previous
  command buffer failure - recreate the backend to recover
E error: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)
```

| 엔드포인트 | 응답 |
|---|---|
| `/health`, `/v1/models`, `/metrics` | **200** ✅ |
| 모든 추론 (텍스트 전용 포함) | **500 Compute error** ❌ |

### 성질

- **자체 복구되지 않는다.** 로그가 명시적으로 "recreate the backend to recover"라고 말한다
- 프로세스는 살아 있으므로 프로세스 감시로는 못 잡는다
- 메트릭에도 안 나타난다 (`requests_processing` = 0, 에러 카운터 자체가 없음)

### 대응

**재시작이 유일한 해결책이다.** 자동화할 것:

```bash
# 프로브 연속 3회 실패 시
pkill -f llama-server; sleep 3; ./configs/smoke.sh
```

### 원인과 예방

가장 흔한 원인은 **서버 두 개를 동시에 띄우는 것**이다 (모델이 메모리에 두 벌).
실제로 이 실수로 재현했다.

- 기동 전에 항상 `pkill -f llama-server`
- 포트를 바꿔 두 번째 서버를 띄우지 말 것. 벤치도 하나씩 순차로
- `-ub`나 `--ctx-size`를 크게 올릴 때 주의

---

## 6. 알아둬야 할 상류 이슈

| 이슈 | 상태 | 우리에게 |
|---|---|---|
| [#21655](https://github.com/ggml-org/llama.cpp/issues/21655) | **OPEN** | **M4에서 3.8배 성능 회귀** 보고 (26B-A4B). pre-M5 커널 선택 의심 |
| [#26470](https://github.com/ggml-org/llama.cpp/issues/26470) | **OPEN** | Metal에서 **E4B Q8_0 디코드 ~13% 회귀** (b9730→b10219) |
| [#24146](https://github.com/ggml-org/llama.cpp/issues/24146) | closed/not planned | `<unused49>` 출력 버그. 12B에서만 보고. **E4B에선 재현 안 됨**(고대비 프로브 통과) |
| [#14530](https://github.com/ggml-org/llama.cpp/issues/14530) | closed | 이미지 배칭 미지원. 설계 전제 |
| [#20228](https://github.com/ggml-org/llama.cpp/pull/20228) | 미병합 종료 | 사전계산 임베딩 주입 불가 |
| [#21450](https://github.com/ggml-org/llama.cpp/issues/21450) | closed/not planned | Metal에서 `-ctk`/`-ctv` 타입 혼용 실패 |

> **"최신 빌드 = 가장 빠름"이 아니다.** 열려 있는 Apple Silicon 회귀가 두 건이다.
> 업그레이드 시 §8을 따를 것.

---

## 7. 일상 점검

```bash
# 처리량 / 캐시 적중률 (부팅 이후 누적)
curl -s localhost:8080/metrics | grep -E "prompt_tokens_(total|cached_total)|requests_"

# 슬롯 상태
curl -s localhost:8080/slots

# 에러 감시 (서버 로그)
grep -iE "OutOfMemory|error state|Compute error|GGML_ASSERT" <서버로그>
```

메트릭의 한계 — 자세히는 `06-logging.md` §2:
- histogram이 없어 p95를 못 낸다
- **에러 카운터가 없다** (OOM 좀비가 안 잡힘)
- 라벨이 없어 설정별 분해 불가

성능 회귀 탐지는 게이트웨이의 JSONL 로그가 담당한다.

---

## 8. 업그레이드 절차

llama.cpp 버전을 올릴 때. **회귀 이슈가 열려 있으므로 그냥 올리지 말 것.**

업그레이드는 `configs/llama.pin`을 고치는 일이다. 그 커밋 하나가 이 레포의 엔진 버전이다.

```bash
# 1. 현재 성능 기록 (기준선). 결과 JSON 에 build 가 함께 박힌다
.venv/bin/python bench/sweep.py 280 min
.venv/bin/python bench/concurrency.py 1
cp bench/results/sweep_min.json bench/results/sweep_min.b10450.json

# 2. 핀을 새 태그로 고친다 (TAG 와 COMMIT 둘 다)
$EDITOR configs/llama.pin

# 3. 재빌드 — 핀과 체크아웃이 다르면 스크립트가 멈춘다
./scripts/build-llama.sh

# 4. 동일 벤치 재실행 후 비교
pkill -f llama-server
.venv/bin/python bench/sweep.py 280 min
```

**되돌리기는 `configs/llama.pin`을 되돌리고 `./scripts/build-llama.sh`를 다시 도는 것이 전부다.**
brew 시절에는 이게 불가능했다 (`brew`는 임의 버전을 다시 설치할 수 없다).

새 커밋 해시 얻기:

```bash
git -C vendor/llama.cpp ls-remote --tags origin 'b*' | tail -5
```

확인할 것:
- 처리량이 기준선 대비 떨어지지 않았는가 (#21655, #26470)
- `--reasoning off`가 여전히 동작하는가
- `--image-min/max-tokens` 플래그명이 유지되는가
- 고대비 이미지에서 `<unused49>`가 나오지 않는가 (#24146)

**벤치 결과에는 항상 빌드 커밋을 함께 기록한다** (`06-logging.md` §4의 `cfg.build`).

---

## 9. 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| 프로세스가 즉사, `GGML_ASSERT ... n_ubatch` | `-ub` < 이미지 토큰 | `-ub`를 올린다 (§3.1) |
| `content`가 빈 문자열, `finish_reason: "length"` | thinking 모드 켜짐 | `--reasoning off` (§3.2) |
| 모든 요청 500, `/health`는 200 | Metal OOM 좀비 | **재시작** (§5) |
| 모델이 응답에서 "이미지를 주세요"라고 함 | 클라이언트가 WEBP 전송 | 클라이언트 수정 (`10-client-guide.md` §2.1) |
| `cached_tokens`가 항상 0 | system turn에 가변값 | 클라이언트 수정 (`10-client-guide.md` §3.1) |
| 첫 요청만 5~10배 느림 | 콜드 스타트 (1.7s → 12.6s 관측) | 주기적 워밍업 요청 |
| 기동 직후 요청이 실패 | 로딩 중 (`/health`는 200) | `/metrics`로 준비 판정 (§2) |
| `FATAL: llama-server 없음` | 빌드를 안 했음 | `./scripts/build-llama.sh` (§1.1) |
| `WARN: vendor/llama.cpp HEAD ... 핀과 다름` | 수동 체크아웃/실험 중 | 의도한 게 아니면 `./scripts/build-llama.sh` |
| `/props`의 `build_info`가 핀과 불일치 | brew판 또는 옛 빌드가 떠 있음 | `pkill -f llama-server` 후 `./configs/smoke.sh` (§2) |
| 갑자기 전체가 5~10배 느림, 로그에 Metal 없음 | `GGML_METAL_EMBED_LIBRARY` 없이 빌드 → CPU 폴백 | `./scripts/build-llama.sh --clean` |
| 처리량이 기대보다 낮음 | 출력 토큰 과다 | 스키마 축소 (`04-b1-results.md` §3) |

---

## 10. 성능 기준선 (b10450, 2026-08-17)

회귀 판정의 기준. 재측정 시 이 값과 비교한다.

| 설정 | prefill | decode | total | img/s |
|---|---:|---:|---:|---:|
| 예산 280, `-ub 320`, tiny 스키마 | 1508 ms | 620 ms | 2.13 s | **0.47** |
| 예산 280, `-ub 2048`, min 스키마 | 1676 ms | 1678 ms | 3.35 s | 0.30 |
| 예산 70, `-ub 2048`, min 스키마 | 702 ms | 1694 ms | 2.40 s | 0.42 |

- decode: **약 37 ms/token** (예산과 무관하게 일정)
- prefill: **고정 ~250–300 ms + 5.7 ms/이미지토큰**
- 메모리: RSS 3.99 GB, MTL0 compute buffer 454.91 MiB (`-ub 2048` 기준)

재현: `bench/sweep.py`, `bench/concurrency.py`
