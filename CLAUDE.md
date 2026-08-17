# VLM_SERVING

한 레포에 **두 프로젝트**가 있다. 경계를 흐리지 않는 것이 이 레포의 제1 규칙이다.

| | 서빙 | 애플리케이션 (trailwalk) |
|---|---|---|
| 위치 | `docs/` `bench/` `configs/` `models/` `scripts/` `vendor/` | `app/` |
| 하는 일 | 이미지 1장 + 텍스트 → JSON 판정 1건. stateless | 로드뷰 수집 · 탐색 루프 · 프롬프트 내용 · 평가 |

## 작업 분리 — 반대편 컨텍스트를 읽지 않는다

app 작업을 할 때 서빙 내부 문서·코드를 열지 않는다. 그 반대도 같다.
둘 사이의 계약은 **`docs/10-client-guide.md` 하나**다 (OpenAI 호환 HTTP).

- app 작업에 필요한 서빙 지식은 `10-client-guide.md` 에 다 있어야 한다.
  부족하면 그 문서를 보강하는 것이 맞지, 서빙 내부 문서를 뒤지는 것이 아니다.
- 서빙 관심사(큐잉 · 동시성 · 게이트웨이 · 스트리밍)를 app 쪽에서 설계하지 않는다.
  필요가 생기면 `docs/02-open-questions.md` §7 에 **요구사항만** 넘긴다.
- 반대 방향도 같다: 서빙 작업에서 프롬프트 내용 · 판정 의미 · 탐색 로직을 열지 않는다.

**양쪽 모두에 해당하는 예외 둘:** `docs/10-client-guide.md`(계약)와
`docs/12-harness.md`(레포 공통 하네스)는 어느 쪽 작업에서든 읽는다.

훅으로 강제하지 않는 이유: 경계 판단에는 예외가 있고(계약 문서 갱신 등),
과잉 차단은 사람이 훅을 끄게 만든다 — `block-secret-reads` 1판에서 이미 치른
수업료다 (`docs/12-harness.md` §4).

## 하네스

```
.venv/bin/python -m pytest    # 전부 오프라인 · 1초 미만
.venv/bin/ruff check .
```

`git commit` 하면 훅이 ruff + pytest + 리뷰 에이전트를 돌린다 (→ `docs/12-harness.md` §4).
커밋은 작게 나눈다 — 40KB 를 넘는 diff 는 에이전트 리뷰를 받지 못한다.
