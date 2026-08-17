# trailwalk — 로드뷰를 따라 산책로를 이어가는 클라이언트

서빙(`../docs/`)은 **이미지 1장 → 판정 1건**만 한다.
어디로 갈지·어떻게 이어갈지·언제 멈출지는 전부 여기 있다.

```
설계 근거 → docs/20-app-design.md
왜 브라우저를 모는가 → docs/21-roadview-providers.md
막힌 것 → docs/23-open-questions.md
```

---

## 빠른 시작

### 1. 서버를 띄운다

```bash
cd .. && ./configs/smoke.sh
```

### 2. 배선을 확인한다 (API 키 불필요)

`fixture` provider 가 로컬 이미지를 로드뷰인 척 돌려준다.

```bash
pip install -r app/requirements.txt
python app/run_walk.py --provider fixture --start 37.5665,126.9780 --bearing 90 --steps 6
```

기대 출력:

```
멈춘 이유: max_steps
스텝 6 · VLM 호출 7 · 26s (3.73s/호출)
```

첫 호출이 13초쯤 걸리면 정상이다 (콜드 스타트). `--warmup` 으로 측정 밖으로 뺀다.

### 3. 실제 로드뷰 — ⚠️ 아직 미검증

키는 `app/.env` 에 둔다 (형식: `app/.env.example`). 커밋되지 않고, 코드가
`trailwalk.config.load_env()` 로 알아서 읽으므로 명령줄에 붙일 필요가 없다.

```bash
cp app/.env.example app/.env      # 값을 채운다
playwright install chromium

python app/check_kakao.py         # ← 먼저 이것. 진단 전용, VLM 불필요
python app/run_walk.py --provider kakao \
    --start 37.5768,127.0246 --bearing 90 --steps 20 --headed
```

`check_kakao.py` 는 서울 좌표 6곳(**차도 대조군 포함**)에서 pano 가 잡히는지,
실제로 그려지는지를 표로 준다. 차도까지 실패하면 커버리지가 아니라 설정 문제다.

콘솔에서 켜야 하는 것이 **두 가지**다. 하나만 해도 증상은 똑같이 "안 뜸" 이다:

| | 위치 |
|---|---|
| 카카오맵 제품 활성화 | 내 애플리케이션 > 제품 설정 > 카카오맵 > 활성화 설정 |
| Web 사이트 도메인 | 플랫폼 > Web > `http://127.0.0.1:8731` (`localhost` 는 별개 도메인) |

> ⚠️ **REST API 키가 아니라 JavaScript 키다.** 콘솔의 앱 키 화면에 네 종류가
> 나란히 있고 전부 32자라 겉으로 구별되지 않는다. 잘못 넣으면 증상이
> "로드뷰가 안 뜬다" 로만 보여서 커버리지 문제로 오인하기 쉽다.
> 그리고 플랫폼 > Web > 사이트 도메인에 `http://127.0.0.1:8731` 을 등록해야 한다.

`--headed` 로 시작한다. 완전 headless 에서 WebGL 이 안 그려져 검은 화면이 찍히는
경우가 있고, 그러면 원인을 로드뷰 커버리지로 오인하기 쉽다.

**먼저 `docs/23-open-questions.md` §1 을 읽을 것.**

### 비밀값 취급

`app/.env` 는 **Claude 가 읽지 못한다.** `.claude/hooks/block-secret-reads.py` 가
Read/Edit/Write/Grep/Glob/Bash 의 접근을 막는다 — 값이 한 번 대화 컨텍스트에
들어가면 트랜스크립트와 이후 모든 요청에 남고 되돌릴 수 없기 때문이다.

값 없이 상태만 확인하려면:

```bash
python3 .claude/hooks/keycheck.py
# app/.env  (50 bytes, mode 644)
#   gitignore: ✓ 무시됨
#   KAKAO_MAP_API_KEY         32자  sha256:81c5c6da
```

키 이름·길이·지문·gitignore 상태만 나오고 값은 나오지 않는다. 지문으로 "같은
키인지" 는 비교할 수 있고 원문은 복원할 수 없다. `.env.example` 은 값이 없는
형식 파일이라 막지 않는다.

### 4. 레이블

```bash
python app/labels/fetch_gil_seoul.py            # 150개 산책로 → labels/trails.json
python app/labels/fetch_gil_seoul.py --detail   # + 경유지 (~3분)
```

---

## 런로그

실행마다 `runs/<시각>-<provider>.jsonl` 이 생긴다. 한 줄 = VLM 호출 한 번.

```bash
python -c "
import json,sys
for l in open(sys.argv[1]):
    d=json.loads(l)
    if d['type']=='probe':
        print(f\"s{d['step']} h{d['heading']:5.1f} trail={d['is_trail']} \"
              f\"pt={d['prompt_tokens']} cached={d['cached_tokens']} {d['latency_ms']:.0f}ms\")
" runs/<파일>.jsonl
```

보존할 런은 `runs/keep/` 으로 옮긴다 (`runs/*.jsonl` 은 gitignore).

---

## 반드시 아는 것

| # | 사실 | 상세 |
|---|---|---|
| 1 | **서버로 나가는 이미지는 전부 `imaging.py` 를 지난다.** 우회 금지 | `20-app-design.md` §5 |
| 2 | **WEBP 는 HTTP 200 인데 조용히 무시된다.** `prompt_tokens` 로만 잡힌다 | `../docs/10-client-guide.md` §2.1 |
| 3 | **system 프롬프트는 파일 + 해시 핀이다.** 고치면 해시도 고칠 것 | `20-app-design.md` §4 |
| 4 | **스키마에만 넣고 프롬프트에서 설명 안 한 필드는 쓰레기를 낸다** | `20-app-design.md` §4 |
| 5 | **동시 요청 금지.** 순차만. 병렬은 서빙 쪽 문제 | `../docs/04-b1-results.md` §4 |
| 6 | **이웃 pano 목록 API 는 없다.** 좌표를 직접 민다 | `21-roadview-providers.md` §1.3 |
| 7 | **500 연속 3회면 재시도를 멈춘다.** 자체 복구 안 됨 | `../docs/11-server-ops.md` §5 |
| 8 | **코스 폴리라인이 아직 없다.** 정량 평가의 구멍 | `22-labels.md` §2 |
