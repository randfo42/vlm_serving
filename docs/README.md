# 문서 색인

VLM 서빙 — 로드뷰 이미지 1장을 받아 구조화된 시각 판정 1건을 돌려주는 stateless 서비스.
상위 애플리케이션(산책로 탐색)의 탐색 루프는 **이 레포 범위 밖**이다.

---

## 읽는 순서

### 이 서비스를 **호출**하려면
→ **`10-client-guide.md`** 하나만 읽으면 된다.
   이미지 전처리 규칙(함정 많음), 요청 형태, 에러 처리, 성능 기대치, 체크리스트.

### 이 서비스를 **운영**하려면
→ **`11-server-ops.md`**
   빌드/기동/정지, 설정 근거, 헬스체크, 실패 모드, 업그레이드 절차, 문제 해결표.

### 처음 체크아웃했다면

```bash
./scripts/build-llama.sh   # 엔진 빌드 (configs/llama.pin 커밋 고정, ~3분)
./configs/smoke.sh         # 기동
```

엔진은 `vendor/llama.cpp`의 **소스 빌드**다. 시스템/brew의 `llama-server`를
쓰지 않는다 — 버전을 핀할 수 없어 아래 실측치가 조용히 무효가 되기 때문이다.
→ `11-server-ops.md` §1

### 이 서비스를 **개발**하려면
1. `00-design.md` — 범위와 경계, 아키텍처, 결정 사항
2. `04-b1-results.md` — 이 머신 실측 결과 (성능의 근거 전부)
3. `03-vision-encoding-constraint.md` — 왜 이 설계인가 (중심 제약)
4. `02-open-questions.md` — 남은 일과 우선순위

---

## 전체 목록

| 문서 | 내용 |
|---|---|
| `00-design.md` | 범위·경계, 워크로드, 모델, 스택, 3존 프롬프트, 아키텍처 |
| `01-research-2026-08.md` | Gemma 4 / llama.cpp 외부 조사 + 출처. **실측 아님** |
| `02-open-questions.md` | 블로커, 측정 계획, 우선순위, TODO |
| `03-vision-encoding-constraint.md` | 비전 인코딩 직렬 제약 (A)(B)(C) 상세 |
| **`04-b1-results.md`** | **이 머신 실측. 추정치를 대체하는 기준 문서** |
| `05-observability.md` | 무엇이 측정 가능한가. 구간 분해, verbose 사용법 |
| `06-logging.md` | 어떻게 기록할 것인가. JSONL 스키마, 알람 조건 |
| **`10-client-guide.md`** | **클라이언트 사용법** |
| **`11-server-ops.md`** | **서버 운영** |
| `../../doc/llama_cpp_vs_vllm_batching.md` | 배칭/KV/attention 커널 사전 조사 (프로젝트 이전) |

---

## 한 장 요약 — 반드시 아는 것들

실측으로 확인된, 모르면 사고 나는 항목:

| # | 사실 | 상세 |
|---|---|---|
| 1 | **Gemma 4는 thinking이 기본 켜짐.** 끄지 않으면 처리량이 무너진다 | `11-server-ops.md` §3.2 |
| 2 | **`-ub` < 이미지 토큰이면 프로세스가 죽는다** (에러 아님, SIGABRT) | `11-server-ops.md` §3.1 |
| 3 | **WEBP는 HTTP 200인데 조용히 무시된다.** 로그에도 안 남음 | `10-client-guide.md` §2.1 |
| 4 | **`/health`는 거짓말한다.** OOM 후에도, 로딩 중에도 200 | `11-server-ops.md` §4 |
| 5 | **Metal OOM은 자체 복구 안 됨.** 재시작만이 답 | `11-server-ops.md` §5 |
| 6 | **decode ~37 ms/token.** 출력 스키마가 1급 처리량 레버 | `04-b1-results.md` §3 |
| 7 | **`--image-min-tokens`를 max와 같게 두면 작은 이미지를 업스케일해 클라이언트 실수를 은폐한다** | `11-server-ops.md` §3.3 |
| 8 | **system turn에 가변값이 섞이면 캐시가 조용히 죽는다** | `10-client-guide.md` §3.1 |
| 9 | **동시성을 올려도 처리량이 거의 안 오른다** (+20%에 지연 3.3배) | `04-b1-results.md` §4 |
| 10 | **엔진은 핀된 소스 빌드다.** PATH의 `llama-server`를 직접 띄우면 딴 버전이 뜬다 | `11-server-ops.md` §2 |

---

## 현재 상태 (2026-08-17)

- ✅ B1~B4 블로커 통과. 모델이 뜨고 정상 동작
- ✅ 성능 축 측정 완료 (토큰 예산, 스키마, 동시성, `-ub`)
- ✅ 엔진을 brew → **커밋 고정 소스 빌드**로 전환. 동일 커밋 재측정으로 수치 동등성 확인
- ⬜ **평가 세트가 유일한 크리티컬 패스** — 정확도 없이는 운영점을 못 정한다
- ⬜ 게이트웨이 미구현 (현재는 `llama-server` 직접 호출)

기준선: llama.cpp **b10450** (`ece963f41`), `gemma-4-E4B-it-qat-q4_0`, 예산 280, `-ub 320`, `-np 1`
→ tiny 스키마 **2.13 s/요청 (0.47 img/s)** · min 스키마 **3.11 s/요청 (0.32 img/s)**
