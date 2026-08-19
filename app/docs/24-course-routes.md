# 코스 폴리라인 — 도보 길찾기 엔드포인트 관찰 기록

관찰: 2026-08-19, map.kakao.com (PC 웹). 소비자: `app/labels/fetch_walk_routes.py`

## 1. 왜 이 방법인가

국내 공식 REST 중 보행자 경로+폴리라인은 TMAP 뿐이고(카카오모빌리티는 자동차만),
사용자 결정으로 **카카오맵 웹의 도보 길찾기를 가로챈다.** 로드뷰 이웃 그래프
(`21-roadview-providers.md` §1.3)와 같은 성격의 **비계약 의존**이다 — 형식이
바뀌면 파서가 터지게 만들었고, 조용히 빈 폴리라인을 내지 않는다.

## 2. 엔드포인트 (2026-08-19 관찰)

Playwright headless 로 `map.kakao.com/?target=walk&rt=<sX>,<sY>,<eX>,<eY>&rt1=&rt2=`
를 열고 XHR 을 전부 기록해 확정했다:

```
GET https://map.kakao.com/route/walkset.json
    ?sName=s&eName=e&sX=&sY=&eX=&eY=&ids=%2C          ← 이름은 안 보낸다
Referer: https://map.kakao.com/          ← 없으면 302. 이것만 있으면 브라우저 불필요
```

즉 관찰 후에는 **Playwright 없이 순수 GET** 으로 수집한다.

## 3. 응답 형식

```jsonc
{"directions": [{
   "success": true, "resultCode": "SUCCESS",
   "routeMode": "BROAD_FIRST",
   "length": 451,               // m
   "time": 482,                 // s
   "sections": [{"guideList": [{
      "guideMent": "서신문까지 32m 이동",
      "rotationCode": "STRAIGHT", "guideCode": "START",
      "x": 492807, "y": 1132282,
      "link": {"length": 32, "time": 30,
               "points": "492807.0,1132282.0|492786.0,1132292.0"}   // ← 폴리라인
   }, ...]}]
}]}
```

- 폴리라인 = 모든 `guideList[].link.points` 를 순서대로 이은 것.
  도착 가이드는 `link` 가 없다. 링크 경계에 중복점이 있어 제거한다.
- 실패 시 `resultCode != "SUCCESS"` — 파서가 ValueError 로 터뜨린다.

## 4. 좌표계 — WCongnamul

요청·응답 모두 WCongnamul: **EPSG:5181(TM 중부원점 — lat0 38, lon0 127, k0 1,
FE 200000, FN 500000, GRS80) × 2.5.**

역변환은 표준 TM 급수를 로컬 구현했고(`wcongnamul_to_wgs84`), 정확도는
- 구현 검증: 사직단 (492858, 1132253) → 37.5756533, 126.9676599 (transcoord 와 7자리 일치)
- **실행마다** REST `transcoord` 로 검증점 3개를 대조, 1m 초과 시 즉시 중단.
  변환 오차는 폴리라인 전체를 조용히 밀어 true 라벨을 통째로 오염시키기 때문이다.

## 5. 예의와 캐시

- **경유지 쌍당 요청 3회다**, 1회가 아니다: 양 끝점을 WGS84 → WCongnamul 로
  바꾸는 REST `transcoord` 2회 + walkset 1회. 정방향 변환은 로컬 구현이 없어서
  매 캐시미스마다 REST 를 친다 (역변환 `wcongnamul_to_wgs84` 만 로컬이다).
  → **TODO**: 정방향도 로컬로 만들면 요청이 1/3 로 준다.
- 요청 사이 3초 대기. 실측 규모: 종로 9코스 42구간, 서울 89코스 297구간
  (대장 150코스 중 지오코딩·게이트를 통과한 것). 캐시가 차 있으면 재실행은
  walkset 요청 0회다.
- **경유지 병합**: 50m(`MERGE_M`) 이내로 붙어 있는 연속 경유지는 하나로 친다.
  종로에서 54개 지오코딩 → 45쌍이 될 것이 42구간이 된 이유다.
- **길이 새너티**: 응답의 `length` 와 폴리라인 재계산이 15% 넘게 어긋나면
  캐시 파일에 `warn_length` 를 남긴다.
- 응답 원본은 `app/labels/<데이터셋>/routes/{course}_{좌표해시10}.json` 에 캐시 —
  키가 인덱스가 아니라 **양끝 좌표의 sha1** 이라 경유지 병합/skip 으로 순서가
  바뀌어도 엉뚱한 구간에 붙지 않는다. 재실행 시 walkset 요청은 나가지 않는다
  (좌표 역변환 검증 transcoord 3회는 매 실행 나간다). 강제 재수집 = 캐시 삭제.
- ⚠️ **요청이 실패하면 `{"_error": ...}` 가 캐시 파일로 기록된다.** 재실행해도
  재시도되지 않고 그 구간은 영구히 `status=missing` 이다. "강제 재수집 = 캐시
  삭제" 로는 이 함정이 안 보인다 — 실패한 구간을 되살리려면 그 파일을 지워야 한다.
- 캐시 키가 좌표 해시라, 경유지를 다시 지오코딩하면 옛 파일이 남는다
  (서울 `routes/` 372개 중 `courses_geom.json` 이 참조하는 것은 297개).
  정리 규칙은 아직 없다 — 안전성의 대가로 받아들이고 있다.
- `fetch_walk_routes.py` 는 `--config` 를 받지 않는다 (`probe_coverage.py` 와
  비대칭이다). 데이터셋은 `--dataset` 으로 고른다.

## 6. 알려진 한계

- 도보 라우터는 **차도 옆 인도·계단을 선호**한다. 숲길(인왕산숲길 등)에서
  실제 산책로가 아니라 인접 도로로 우회할 수 있다. 코스가 길 위를 따라가는지는
  `app/eval/plot_course.py` 가 그리는 SVG 로 눈으로 본다 — 다만 **이 눈검증은
  아직 안 했고**, 어긋난 구간을 손으로 교체하는 수단도 없다.
- 경유지 5건이 POI 미등록으로 skip 됐다(`labels/jongno/overrides.json`) —
  그 구간은 이웃 노드끼리 직접 길찾기하므로 실제 코스와 다를 수 있다.
- ~~ratio 게이트가 로드뷰 커버리지와 같은 방향으로 물어 테마 편향을 만든다~~
  **예상이 빗나갔다 (2026-08-19 실측).** 두 게이트는 사실상 독립이고 방향조차
  예상과 반대였다 — 실측과 왜 틀렸는지는 `22-labels.md` §9.
- 엔드포인트·응답 형식은 카카오가 예고 없이 바꿀 수 있다. 소비 코드는
  전부 이 문서와 `fetch_walk_routes.py` 한 쌍에 격리돼 있다.
