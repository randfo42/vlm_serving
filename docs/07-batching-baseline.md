# 배칭 베이스라인 — 수정 실험 전의 원시 데이터

측정: 2026-08-18, llama.cpp **b10450** (`ece963f41`), gemma-4-E4B-it-qat-q4_0, M4/16GB
수집기: `bench/batching_baseline.py` → `bench/results/batching_{serial,conc,multi}.json`

**목적.** llama.cpp 배칭을 직접 수정해 보기 전의 "고치기 전" 기준선.
기존 벤치(`04-b1-results.md`)는 중앙값 표만 남겼는데, 수정 효과를 보려면
요청별 · 구간별 원시 행이 필요하다. 외부 조사 배경은 `01-research-2026-08.md`
(요약: cross-request 이미지 배칭은 upstream에 없고, 요청 내 배칭 API는
b10450에 있으며, 작성자 본인이 Apple Silicon에서 "almost no gain"이라 실측).

---

## 1. 요청 1건의 해부 (serial)

비워밍업 11건 중앙값. `reserve`는 causal↔non-causal 전환 후 그래프 재예약의
**벽시계 공백**이다 (아래 §4 주의 참조).

| budget | ub | e2e | prefill | clip_encode | reserve① (→비전) | reserve② (→텍스트) | 이미지 KV | decode |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 70 | 2048 | 1275 | 697 | 141 | 84 | **231** | 87 | 571 |
| 280 | 320 | 2124 | 1508 | 504 | 24 | **753** | 32 | 582 |
| 280 | 2048 | 2260 | 1680 | 506 | 97 | **854** | 102 | 570 |
| 1120 | 2048 | 8211 | 7551 | 3813 | 96 | **3380** | 108 | 594 |

(단위 ms. clip/reserve/KV는 verbose 로그의 타임스탬프에서 파싱)

읽어야 할 것:

- **reserve②가 prefill의 40~50%를 차지하는 순수 오버헤드**이고, 고정비가 아니라
  이미지 토큰 수에 대략 선형으로 자란다 (231 → 753/854 → 3380).
  `05-observability.md` §4의 "851ms 공백"의 스케일링 특성이 이걸로 확정됐다.
- clip_encode는 토큰에 superlinear (280→1120: 토큰 4배, 시간 7.5배) —
  non-causal full attention O(n²)와 일치.
- 운영 설정(280/ub320)에서 요청당 비용 순위:
  **reserve② 753 > decode 582 > clip 504 >> 나머지.**

## 2. 동시성 원시 행 (conc)

budget 280, tiny 스키마, ub 2048, ctx 슬롯당 4096 — `04-b1-results.md` §4와 동일
조건, 22요청. 요청별 행(응답 timings + 클라이언트 e2e + 시작 오프셋)이 JSON에 있다.

| -np | img/s | p50 지연 | 04-b1 §4 (11요청) |
|---:|---:|---:|---|
| 1 | 0.437 | 2.28 s | 0.422 / 2.38 s |
| 2 | 0.478 | 4.21 s | 0.442 / 4.41 s |
| 4 | 0.499 | 7.97 s | 0.508 / 7.89 s |

재현 확인. `-np` 이득(+14~20%)은 decode 겹침에서 나온다.

## 3. 요청 내 배치 인코딩 프로브 (multi) — 핵심 발견

이미지 N장을 한 요청에 넣으면 b10450의 `mtmd_batch_encode`가 발동하는가?
budget 280, ub 2048, `--mtmd-batch-max-tokens 2048`, N당 3반복 중앙값.

| N | batch entries | prefill | clip/장 | reserve/장 | KV 삽입/장 |
|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 1692 | 506 | 956 | ~120 |
| 2 | **2** | 3088 | 477 | 941 | ~105 |
| 3 | **3** | 4598 | 476 | 962 | ~110 |
| 4 | **4** | 6084 | 480 | 969 | ~100 |
| 6 | **6** | 9184 | 479 | 1003 | ~100 |

세 가지가 확정된다:

1. **우리 모델 그래프는 배치 인코딩 지원 대상이다** (`entries` = N).
   b10450 화이트리스트(gemma4v/internvl/deepseekocr)에 든다는 뜻.
2. **배치 인코딩의 이득은 장당 ~5% (506→479ms)뿐이다.**
   ngxson의 "almost no gain" (PR #24384)이 이 GPU·이 모델에서 그대로 재현됐다.
   인코더가 batch=1에서 이미 GPU를 포화시킨다.
3. **재예약은 배치 인코딩과 무관하게 이미지마다 2회씩 일어난다**
   (작은 것 ~60–170ms + 큰 것 ~830–1000ms). N=6이면 reserve 12회, 합 ~6초.
   인코딩을 묶어도 KV 삽입이 장별로 causal 전환을 반복하기 때문이다.

## 4. 수정 실험에의 함의

- **과녁은 인코더 배칭이 아니라 reserve② — causal 전환 시 그래프 재예약이다.**
  운영점에서 요청당 753ms(35%), 어떤 배칭으로도 상각되지 않고, 순수 오버헤드다.
  관련 upstream: PR #24361 (shared backend sched, open) — 전환 시 sched를 공유해
  재예약을 없애려는 시도. 이 방향이 배치 인코딩(이득 ~26ms/장)보다 크기가 10배 이상.
- cross-request 이미지 배칭을 직접 구현해도 기대 이득은 §3의 2번 그대로
  (장당 ~26ms)다. 만들기 전에 이 수를 기억할 것.
- ⚠️ **로그의 `reserve took N ms`를 믿지 말 것.** 그 값은 예약 계산만 잰다.
  실제 공백은 직전 `sched_reserve: reserving ...` 줄과의 타임스탬프 차이에 있다
  (실측: took 22ms vs 벽시계 769ms). `batching_baseline.py`의 파서가 이 방식이다.

## 5. 재현

```bash
# 서버는 스크립트가 페이즈마다 재기동한다 (기존 서버는 내려둘 것)
.venv/bin/python bench/batching_baseline.py all      # serial+conc+multi, ~12분
.venv/bin/python bench/batching_baseline.py serial   # 개별 실행도 가능
```

결과 JSON에는 요청별 원시 행과 빌드 지문(`build`)이 박힌다. 엔진을 수정한 뒤
같은 명령으로 다시 돌리면 `bench/results/*.json`끼리 직접 비교할 수 있다 —
수정 빌드는 핀과 다르므로 `build`가 `UNPINNED-…`로 찍혀 섞임이 방지된다.
