# app/ — trailwalk

루트 `CLAUDE.md` 가 레포 전체의 규칙(경계·설정 정본·하네스)이다. 이 파일은
**app 안에서만 통하는 것** 두 가지다: 파일이 어디 있는가, 그리고 문서를 코드와
어떻게 붙여 두는가.

## 파일 구조 — 여기가 정본이다

문서 안에 트리를 다시 그리지 않는다. 한때 `docs/20-app-design.md` §2 가
트리를 들고 있었는데, 파일이 12개 늘어나는 동안 아무도 안 고쳐서 실제 구조와
크게 어긋났다. 구조를 알고 싶으면 이 파일을 본다.

```
app/
  run_explore.py         # CLI 진입점 — 시작점에서 뻗는 산책로를 전부 마킹 (BFS)
  run_eval.py            # CLI 진입점 — 라벨 세트로 정확도를 잰다
  check_kakao.py         # 진단 — 로드뷰가 잡히고 실제로 그려지는지 (VLM 불필요)
  check_fov.py           # 진단 — 화각·화살표 측정 (VLM 불필요)

  config/trailwalk.yaml  # ★ 기본값의 유일한 정본. 값마다 근거 주석
  prompts/system_v*.txt  # ★ 판정 기준의 유일한 진실. 바이트 고정 (v1·v2 도 보존)
  .env / .env.example    # 비밀값. gitignore + 훅으로 읽기 차단

  trailwalk/             # 라이브러리 — 여기만 app 밖에서 import 될 수 있다
    explore.py           # 탐색 루프(BFS) + 후보 생성 `_candidates`
    settings.py          # trailwalk.yaml → dataclass. 모르는 키는 터뜨린다
    prompt.py            # 프롬프트 로드 + 해시 핀 + 출력 스키마
    imaging.py           # ★ 서버로 나가는 모든 바이트가 지나는 단일 출구
    vlm.py               # 1턴 호출 + 조용한 실패 감지 + 서킷브레이커
    geo.py               # 거리·각도. 이동을 만들지 않는다
    runlog.py            # JSONL
    config.py            # .env → os.environ. 값을 절대 출력하지 않는다
    providers/
      base.py            # Pano · Neighbor · RoadviewProvider 프로토콜
      fixture.py         # 오프라인. API 키 없이 전체 배선 확인
      kakao.py           # Playwright + JS SDK. 이웃 그래프 + 프레임 안정화

  labels/                # 라벨 데이터 수집 파이프라인 (→ docs/22-labels.md)
    fetch_gil_seoul.py     # gil.seoul.go.kr → trails.json (150개 산책로)
    adapt_gil_seoul.py     # trails.json → seoul/courses.json
    fetch_jongno.py        # 자치구 페이지 → jongno/courses.json
    geocode_waypoints.py   # 경유지 이름 → 좌표
    fetch_walk_routes.py   # 카카오 도보 길찾기 → 코스 폴리라인 (→ docs/24)
    probe_coverage.py      # 코스별 로드뷰 커버리지
    make_samples.py        # 폴리라인 리샘플 → pano 캡처 → samples.jsonl
    apply_review.py        # 사람 검수 결과 → labels.jsonl
    funnel.py              # 단계별 잔존 수 집계
    dataset.py             # 데이터셋 경로 한 곳 (jongno / seoul)
    trails.json
    <데이터셋>/            # courses·waypoints·routes·samples·labels·images·svg

  eval/
    report_eval.py       # run_eval 산출물 → 정확도 리포트
    plot_explore.py      # explore dump → SVG
    plot_course.py       # 코스 폴리라인 + 샘플 → SVG (검수용)

  tests/                 # 전부 오프라인 · 1초 미만
  docs/                  # 20 설계 · 21 provider · 22 라벨 · 23 열린 질문 · 24 코스
  runs/                  # 런로그 (gitignore)
```

`trailwalk/` 만 라이브러리다. `run_*.py`·`check_*.py`·`labels/`·`eval/` 은
전부 스크립트이고 서로 import 하지 않는다.

## 문서를 코드와 붙여 두는 규칙

이 레포 문서의 실패 방식은 "틀린 말을 적는 것" 이 아니라 **"맞았던 말이
그대로 남는 것"** 이다. 폐기된 설계, 옛 기본값, 없어진 CLI 플래그가 현재형
문장으로 남아서, 문서를 읽고 그대로 따라 하면 안 되는 상태가 됐다.

1. **설정 기본값을 문서에 숫자로 적지 않는다.** 키 이름과 *왜 그런 값인가*만
   적고, 값은 `config/trailwalk.yaml` 을 가리킨다. 근거 주석도 yaml 쪽에 둔다.
   (문서에 `miss_tolerance` 2회라고 적혀 있는 동안 실제 값은 20이었다.)
2. **런 진입점(`run_*.py`)의 CLI 인자는 `--config` 하나다.** 문서·도크스트링·
   에러 메시지에 다른 플래그를 적지 않는다. `--provider` `--start` `--steps`
   `--prompt` `--save-images` `--dump` 는 전부 한때 문서에만 존재하던 것들이고,
   실제 손잡이는 같은 이름의 yaml 키다.
   진단 스크립트(`check_kakao.py` `check_fov.py`)는 예외다 — 런이 아니라
   사람이 눈으로 보는 도구라 `--headed` 같은 인자를 갖는다.
3. **코드 동작을 바꾸면 그 동작을 서술한 문서를 같은 커밋에서 고친다.**
   문서 커밋으로 미루면 근거가 뒤집힌 문장이 중간 커밋에 남는다.
4. **폐기한 설계는 지운다.** 남길 값어치가 있으면 "폐기됨 + 왜" 한 줄로
   격하한다. 폐기 경위를 현재형 설명으로 남기지 않는다.
5. **실측 숫자에는 날짜와 조건을 붙인다.** 조건이 바뀌면 숫자는 기록이지
   기대값이 아니다.
6. **app 코드가 서빙 레포 문서(`docs/…`)를 참조하지 않는다.** 필요한 계약은
   `../../docs/10-client-guide.md` 에 있어야 한다 (→ 루트 `CLAUDE.md`).

## 문서 지도

| 문서 | 무엇 |
|---|---|
| `docs/20-app-design.md` | 설계와 그 근거. 왜 이렇게 됐는가 |
| `docs/21-roadview-providers.md` | 로드뷰 provider 조사·실측. Kakao SDK 내부 |
| `docs/22-labels.md` | 라벨 데이터셋 — 무엇을 모았고 무엇이 없는가 |
| `docs/23-open-questions.md` | 아직 안 정한 것들. 진단 절차 |
| `docs/24-course-routes.md` | 코스 폴리라인 — 도보 길찾기 엔드포인트 관찰 기록 |
