# 로드뷰 제공자 조사 — Kakao / Naver / Google

조사: 2026-08-17 · 방법: 공식 문서 + 개발자 포럼 웹 조사
상태: **문서 기반. 실측 아님** — 아직 어느 제공자도 실제로 호출해보지 못했다

이 문서의 목적 하나: **"좌표와 방위를 주면 이미지를 돌려주는 창구가 있는가"** 에
답하는 것. 결론부터 말하면 **없다.**

---

## 0. 한 줄 결론

| | 서버사이드 REST 이미지 | 이웃 pano 목록 | 서울 산책로 커버리지 | 이미지 저장 허용 |
|---|---|---|---|---|
| **Kakao 로드뷰** | ❌ JS SDK 전용 | ❌ 문서에 없음 | 🟡 도보·세그웨이 촬영 정황 (미확인) | ❌ 운영정책상 금지 |
| **Naver 파노라마** | ❌ JS SDK 전용 | ❌ 문서에 없음 | 🟡 불명 | ❓ 약관 확인 못 함 |
| **Google Street View** | ✅ Static + Metadata | 🟡 JS SDK 에만 `links[]` | ❌ **2021.09 한국 실외 이미지 대부분 삭제** | ❌ ToS상 ML 사용 금지 |

**채택: Kakao + headless 브라우저.** 유일하게 커버리지가 있을 법한 선택지이고,
그 대가로 REST 대신 Playwright 를 몰아야 한다. → `../trailwalk/providers/kakao.py`

---

## 1. Kakao 로드뷰

### 1.1 REST 가 없다

Kakao 의 REST API 목록에 로드뷰가 없다. 로컬 검색, 좌표 변환, 주소 검색은 REST 인데
로드뷰만 없다. 로드뷰는 `kakao.maps.Roadview` 가 **브라우저에서 WebGL 로 그리는 것**
으로만 존재한다. Google 의 Street View Static API 같은 정적 이미지 URL 패턴은
문서·포럼 어디에서도 확인되지 않았다.

### 1.2 쓸 수 있는 JS 표면

| 호출 | 주는 것 |
|---|---|
| `RoadviewClient.getNearestPanoId(pos, radius, cb)` | **panoId 숫자 하나뿐.** 좌표도, 방위도, 이웃도 없음 |
| `Roadview.setPanoId(panoId, position)` | 화면을 그 pano 로 |
| `Roadview.getPosition()` | 현재 pano 의 실제 좌표 |
| `Roadview.setViewpoint({pan, tilt, zoom})` | `pan` 0~360(0=북), `tilt` −90~90, `zoom` −3~3 |
| `event: 'init'` | pano 가 바뀌어 렌더가 끝났을 때 |

두 가지가 설계를 결정한다.

**(a) `getNearestPanoId` 는 좌표를 돌려주지 않는다.** 스냅된 실제 위치를 알려면
로드뷰에 올려놓고 `getPosition()` 을 읽어야 한다. 요청 좌표를 그대로 다음 스텝의
기준으로 쓰면 스냅 오차가 누적되어 경로가 실제 길에서 밀려난다.
→ `kakao.py` 의 `nearest()` 가 pano 를 올려놓고 좌표를 읽는 이유.

**(b) `zoom` 은 각도가 아니라 −3~3 의 이산 배율이다.** "화각 90도" 를 지정할 방법이
없다. `zoom 0` 의 기본 화각을 그대로 쓴다. 그 화각이 실제로 몇 도인지는 미확인이다
(→ `23-open-questions.md` §3).

### 1.3 이웃 pano 목록이 없다

문서에 인접 파노라마를 열거하는 메서드가 없다. 위젯에서는 화면 위 도로 오버레이를
클릭해 이동하는데, 그 링크 정보를 얻는 공개 API 가 보이지 않는다.

**이게 탐색 설계를 바꾼다.** 그래프 순회를 포기하고 좌표를 직접 민다:

```
현재 pano 좌표 → heading 방향으로 STEP_M 전진한 좌표 계산 → getNearestPanoId 로 스냅
```

`geo.destination()` + `provider.nearest()` 조합이 이것이다. 결과적으로 제공자를
갈아끼우기는 오히려 쉬워졌다 — 어느 제공자든 "좌표 → pano" 만 있으면 된다.

### 1.4 커버리지 — 가장 중요한 미확인 항목

Kakao 는 일반 도로를 차량으로 찍고, **공원·좁은 산책로·관광지는 배낭형 파노라마
장비로 도보나 세그웨이로 찍는다**는 서술이 국내 기술 글들에 있다. 사실이라면 서울
테마산책길에 실제 커버리지가 있다는 뜻이라 이 프로젝트의 성립 여부가 걸린 항목이다.

**그런데 Kakao 공식 문서에서는 확인하지 못했다.** 2차 자료뿐이다.
확인 방법은 하나뿐이다 — 실제 산책로 좌표를 넣고 pano 가 잡히는지 보는 것.
→ `23-open-questions.md` §1

### 1.5 정책

- Kakao 운영정책 제5조는 **Kakao 로부터 받은 데이터의 캐싱을 원칙적으로 금지**하고,
  "인앱 UX 개선 목적이며 최신 상태를 유지하는 경우" 만 예외로 둔다. Kakao 담당자가
  포럼에서 정적 지도 이미지 저장이 이 조항 위반이라고 직접 답한 사례가 있다.
- 로드뷰 이미지를 공익 조사에 쓰겠다는 문의에는 일반론적 답변만 달렸고, **연구·ML
  목적의 명시적 예외는 확인되지 않았다.**
- 접근은 공식 SDK(JS/iOS/Android)를 통해서만 허용된다.
- 요청 한도: SDK 종류별 일 30만 / 합계 월 300만.

→ 이 프로젝트의 로컬 연구 범위를 넘기기 전에 `23-open-questions.md` §2 를 볼 것.

---

## 2. Naver 파노라마 (NCP Maps)

NCP Maps 제품 목록은 Web/Mobile Dynamic Map, **Static Map**, Directions, Geocoding,
Reverse Geocoding 이다. 파노라마는 **Web Dynamic Map JS SDK 의 하위 모듈**로만 있고
Static Map 에 대응하는 REST 파노라마가 없다.

`naver.maps.Panorama` 는 오히려 Kakao 보다 다루기 좋은 면이 있다:

- `pov = {pan(−180~180, 정북 기준), tilt(−90~90), **fov(20~100)**}` — **화각을 각도로
  지정할 수 있다.** Kakao 의 이산 zoom 과 대비된다.
- `getLocation()` 이 `{panoId, title, address, coord, **photodate**}` 를 준다.
  촬영일자가 나오는 건 판정 결과를 해석할 때 쓸모가 있다.
- `position` 을 주면 기본 300m 반경에서 자동 스냅.
- 이벤트: `init`, `pano_status`(OK/ERROR), `pano_changed`, `pov_changed`.

**이웃 pano 목록은 여기도 없다.** 그리고:

- 파노라마 단독 과금/무료 한도를 문서에서 찾지 못했다 (Web Dynamic Map 에 묶여 있음).
- 파노라마 이미지 캐싱에 관한 약관 조항을 확인하지 못했다.

→ 둘 다 미확인. Kakao 커버리지가 나쁘게 나오면 그때 Naver 를 같은 방식으로 조사한다.
`fov` 를 각도로 줄 수 있다는 점만으로도 대안 가치가 있다.

---

## 3. Google Street View — 유일하게 REST 가 있지만 못 쓴다

REST 는 깔끔하다:

```
https://maps.googleapis.com/maps/api/streetview
  ?location=lat,lng | pano=<id>
  &size=640x640          # 최대 640×640 — 우리 목표 1280×720 보다 작다
  &heading=0..360 &pitch=-90..90 &fov=1..120 &radius=50
  &source=default|outdoor &key=...
```

메타데이터 엔드포인트(`/streetview/metadata`)는 **무료·무제한**이고 pano 존재 여부를
알려준다. 이미지는 월 1만 장 무료, 이후 1000장당 $7.00 부터.
이웃 링크(`links[]` = `{heading, pano, ...}`)는 REST 가 아니라 JS
`StreetViewService.getPanorama()` 에만 있다.

**그런데 두 가지가 각각 단독으로 이 선택지를 죽인다:**

1. **커버리지.** Google 은 2012년 한국 서비스를 시작했으나 **2021년 9월 한국 실외
   스트리트뷰 이미지를 사실상 전량 내렸다.** 남은 것은 실내와 사용자 제출 이미지다.
   차도조차 성기고 오래됐으며, 공원·산책로는 애초에 의미 있게 찍힌 적이 없다.
2. **약관.** Maps Platform 약관상 **`pano_id` 외에는 캐싱·저장·색인이 금지**되고,
   콘텐츠를 **ML 모델의 학습·개발·개선에 사용하는 것이 명시적으로 금지**된다.
   커버리지가 있었더라도 이 파이프라인에는 쓸 수 없다.

`size` 상한 640×640 도 부수적 문제다 — 목표 해상도(긴 변 1024px 이상)에 못 미쳐
서버가 다운스케일하지 않고 토큰 수가 떨어진다(`../../docs/10-client-guide.md` §2.5).

---

## 4. 그래서 어떻게 만들었나

```
Kakao JS SDK 를 얹은 한 페이지짜리 로컬 HTTP 서버 (127.0.0.1)
   └ Playwright(chromium) 가 로드
       ├ __nearest(lat,lng,r) → getNearestPanoId → panoId
       ├ __show(panoId, pan)  → setPanoId + setViewpoint + 렌더 대기 → 실제 좌표
       └ #rv 엘리먼트 스크린샷 (1280×720 PNG)
                    ↓
            imaging.view_to_data_uri  ← JPEG 강제, 종횡비 고정
                    ↓
            vlm.assess  ← 1턴, is_trail 하나
                    ↓
            walk.py 가 다음 좌표를 계산 (그래프 없이)
```

설계에 박힌 제약 네 가지 (1·4 는 실측으로 확인됨):

1. **로컬 HTTP 서버가 필요하다.** Kakao 앱키는 도메인 등록제라 `file://` 로는 SDK 가
   거절한다. `http://127.0.0.1:8731` 을 개발자 콘솔에 등록해야 한다.
   **`127.0.0.1` 과 `localhost` 는 다른 도메인으로 취급된다** — 실측으로 확인했다
   (127.0.0.1 은 통과, localhost 는 `domain mismatched!`). provider 가 IP 로
   고정하는 이유다.
2. **완전 headless 에서 WebGL 이 안 그려질 수 있다.** 검은 화면이 찍히면 이게 첫 번째
   의심 대상이다. `--headed` 로 확인한다. `--use-angle=swiftshader` 를 기본으로 걸어뒀다.
3. **렌더 완료를 기다려야 한다.** `init` 이벤트 뒤 `requestAnimationFrame` 두 번 +
   250ms 를 준다. 이걸 빼면 이전 프레임이나 검은 화면을 찍는다.
4. **SDK 인증 실패는 브라우저 안에서 보이지 않는다.** 인증에 실패하면 Kakao 가 JS
   대신 JSON 에러를 돌려주는데, 크롬은 그 응답을 `<script>` 로 받기를 거부하고
   `net::ERR_BLOCKED_BY_ORB` 로 통째로 막는다 — **에러 내용이 사라지고** SDK 가
   그냥 안 뜬다.

   그래서 `diagnose_sdk()` 가 같은 URL 을 같은 Referer 로 **파이썬에서 한 번 더
   받아** 본문을 읽는다. 실측으로 나온 것들:

   | 응답 | 뜻 | 조치 |
   |---|---|---|
   | 403 `disabled OPEN_MAP_AND_LOCAL service` | 앱에서 카카오맵 제품이 꺼져 있음 | 제품 설정 > 카카오맵 활성화 |
   | 401 `domain mismatched! caller=...` | 그 origin 이 미등록 | 플랫폼 > Web 도메인 등록 |

   이 구분이 없으면 "로드뷰가 안 뜬다" 하나로 뭉뚱그려져 커버리지 문제로 오인한다.

### 처리량

호출 하나가 2.1~2.5초이고 브라우저 렌더가 0.3~1초 더 붙는다. 스텝당 1~3호출이므로
**스텝당 대략 3~8초**를 예상한다. 병렬화는 여기서 풀 문제가 아니다 — 서버가 `-np 1`
이고 비전 인코딩이 직렬이라 동시 요청은 줄만 선다(`../../docs/04-b1-results.md` §4).

---

## 5. 재확인이 필요한 것

이 문서는 전부 웹 조사다. 하중이 걸리기 전에 직접 확인할 것:

| # | 항목 | 확인 방법 |
|---|---|---|
| 1 | Kakao 가 서울 산책로를 실제로 찍었는가 | 실제 좌표로 `getNearestPanoId` 호출 |
| 2 | Kakao 공식 자료의 도보 촬영 언급 | Kakao 공식 채널 |
| 3 | NCP 파노라마 약관의 캐싱 조항 | NCP 이용약관 원문 |
| 4 | NCP 파노라마 과금 | NCP 콘솔 요금 계산기 |
| 5 | Kakao `zoom 0` 의 실제 화각(도) | 렌더 이미지에서 역산 |
