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

설정은 `app/config/trailwalk.yaml` 하나에 다 있다. **CLI 인자는 `--config`
하나뿐이다** (→ 루트 `CLAUDE.md` "설정"). 기본 provider 가 이미 fixture 다.

```bash
pip install -r app/requirements.txt

# 배선만 확인할 때는 반경을 줄인다 — 기본 1000m 는 fixture 격자에서 30분짜리다
cp app/config/trailwalk.yaml app/config/smoke.yaml   # budget.max_distance_m: 30.0
python app/run_explore.py --config app/config/smoke.yaml
```

값을 바꾸려면 언제나 설정을 복사해서 고친다:

```bash
cp app/config/trailwalk.yaml app/config/my.yaml   # start·예산·프롬프트를 고치고
python app/run_explore.py --config app/config/my.yaml
```

기대 출력 (반경 30m):

```
멈춘 이유: exhausted
노드 47 · 판정 46 (산책로 42) · VLM 호출 46 · 99s (2.14s/호출)

예산에 걸려 못 간 갈래 18개:
  d 3 fx_37.569889_127.006488 → fx_37.569978_127.006528  [distance_budget]
  ...
```

`exhausted` 는 **반경 안을 다 봤다**는 뜻이다. 반경 밖 갈래 18개는 버려지지
않고 `frontier` 에 남는다 — 이어서 탐색할 때 그게 그대로 입력이다.

노드는 큐에서 꺼낸 지점, 판정은 간선 하나하나다. 반경 안을 다 돌면
노드 = 판정 + 1 이 된다 — 시작 노드만 판정 없이 들어가고, 이미 본 pano 는
후보에서 미리 빠져 판정이 낭비되지 않는다.

⚠️ **기본 반경 1000m 로 돌리면 30분 걸린다.** fixture 는 10m 격자 이웃을
무한히 주므로 반경 안 노드가 3만 개 수준이고, `budget.max_seconds`(1800s)가
먼저 걸린다. 실제 로드뷰는 길이 있는 곳에만 pano 가 있어 훨씬 적다.

첫 호출이 13초쯤 걸리면 정상이다 (콜드 스타트). `run.warmup: true` 로 측정 밖으로 뺀다.

### 3. 실제 로드뷰 — ✅ 검증됨 (2026-08-17)

청계천에서 실제로 주행했다 (20스텝 229m). 차도를 거부하고 하천 보행로를 따라갔다
(`docs/23-open-questions.md` §1). 아래는 처음 붙일 때 막히는 자리들이다.

키는 `app/.env` 에 둔다 (형식: `app/.env.example`). 커밋되지 않고, 코드가
`trailwalk.config.load_env()` 로 알아서 읽으므로 명령줄에 붙일 필요가 없다.

```bash
cp app/.env.example app/.env      # 값을 채운다
playwright install chromium

python app/check_kakao.py         # ← 먼저 이것. 진단 전용, VLM 불필요

# 설정에서 provider: kakao, start: [37.5768, 127.0246], headed: true 로 고친 뒤
python app/run_explore.py --config app/config/my.yaml
```

`check_kakao.py` 는 서울 좌표 6곳(**차도 대조군 포함**)에서 pano 가 잡히는지,
실제로 그려지는지를 표로 준다. 차도까지 실패하면 커버리지가 아니라 설정 문제다.

화각을 재거나 화살표가 그림과 맞는지 보려면 (VLM 불필요):

```bash
python app/check_fov.py            # zoom 0 화각 측정 + 스윕 이미지 15장
```

콘솔에서 켜야 하는 것이 **두 가지**다. 하나만 해도 증상은 똑같이 "안 뜸" 이다:

| | 위치 |
|---|---|
| 카카오맵 제품 활성화 | 내 애플리케이션 > 제품 설정 > 카카오맵 > 활성화 설정 |
| Web 사이트 도메인 | 플랫폼 > Web > `http://127.0.0.1:8731` (`localhost` 는 별개 도메인) |

> ⚠️ **REST API 키가 아니라 JavaScript 키다.** 콘솔의 앱 키 화면에 네 종류가
> 나란히 있고 전부 32자라 겉으로 구별되지 않는다. 잘못 넣으면 증상이
> "로드뷰가 안 뜬다" 로만 보여서 커버리지 문제로 오인하기 쉽다.
> 그리고 플랫폼 > Web > 사이트 도메인에 `http://127.0.0.1:8731` 을 등록해야 한다.

`run.headed: true` 로 시작한다. 완전 headless 에서 WebGL 이 안 그려져 검은 화면이 찍히는
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

### 판정을 눈으로 감사한다

`run.save_images: true` 로 켜면 probe 이미지가 `runs/images/<런이름>/` 에 쌓이고,
런로그의 각 probe 줄에 `image` 필드로 파일명이 붙는다.

```bash
# 설정에서 provider: kakao, save_images: true
python app/run_explore.py --config app/config/my.yaml
# runs/images/<런이름>/001_s00_1039598318_091.4_T.png
#                     ↑순서 ↑depth ↑pano   ↑방위  ↑판정
```

이름순 정렬이 곧 호출순이라 판정을 따라가며 볼 수 있다. **기본은 꺼져 있다** —
지도 사업자 약관상 이미지 캐싱이 회색지대다 (`docs/23-open-questions.md` §2).

### explore 결과를 지도 위에서 본다

`run.dump` 로 낸 JSON 을 SVG 한 장으로 만든다. 배경은 OSM 타일을
base64 로 내장하므로 파일 하나로 자족적이다 (오프라인이면 `--no-map`).

```bash
# 설정에서 provider: kakao, dump: /tmp/explore.json
python app/run_explore.py --config app/config/my.yaml
python app/eval/plot_explore.py /tmp/explore.json -o /tmp/explore.svg
```

산책로 = 초록 실선 · 아님 = 빨강 점선 · 미탐색(frontier) = 회색 점선 원.

---

## 반드시 아는 것

| # | 사실 | 상세 |
|---|---|---|
| 1 | **서버로 나가는 이미지는 전부 `imaging.py` 를 지난다.** 우회 금지 | `20-app-design.md` §5 |
| 2 | **WEBP 는 HTTP 200 인데 조용히 무시된다.** `prompt_tokens` 로만 잡힌다 | `../docs/10-client-guide.md` §2.1 |
| 3 | **system 프롬프트는 파일 + 해시 핀이다.** 고치면 해시도 고칠 것 | `20-app-design.md` §4 |
| 4 | **스키마에만 넣고 프롬프트에서 설명 안 한 필드는 쓰레기를 낸다** | `20-app-design.md` §4 |
| 5 | **동시 요청 금지.** 순차만. 병렬은 서빙 쪽 문제 | `../docs/04-b1-results.md` §4 |
| 6 | **이웃 pano 그래프로만 걷는다.** 좌표를 밀어 이동하던 폴백은 없앴다 | `20-app-design.md` §3 |
| 7 | **500 이 연속으로 나면 재시도를 멈춘다** (`vlm.fatal_500_streak`). 자체 복구 안 됨 | `../docs/11-server-ops.md` §5 |
| 8 | **라벨 세트가 양성 전용이다.** 재현율만 나오고 precision·ROC 는 정의되지 않는다 | `22-labels.md` §4 |
| 9 | **차량 촬영 pano 와 도보 촬영 pano 는 그래프로 안 이어져 있다.** 시작점이 어느 계열에 스냅되느냐가 결과를 가른다 | `23-open-questions.md` §7 |
| 10 | **화각은 provider 가 정한다.** kakao 는 90.9° 고정, fixture 는 사진마다 다르다 — 둘을 화각 축에서 비교하지 말 것 | `23-open-questions.md` §3 |
| 11 | **판정은 *지점*, 캡처는 *방향*이다.** 축 밖 뷰를 정확도 표에 섞지 말 것 | `23-open-questions.md` §6 |
| 12 | **빈 이웃 목록 = 로드 실패**(`neighbors_missing`). 길의 끝이 아니다 | `23-open-questions.md` §7 |
| 13 | **시작점은 화살표를 전부 묻는다.** 호출 수 = 화살표 수, 하나라도 산책로면 산책로 | `20-app-design.md` §3 |
