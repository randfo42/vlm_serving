"""웹 API — 지도 조회·라벨링의 서버 쪽 (로컬 전용).

### 이 모듈이 임포트하지 않는 것이 계약이다

`trailwalk.store`·`settings`·`config` 만 쓴다. **`trailwalk.runner` 와
`providers` 는 임포트하지 않는다** — runner 는 providers 를 최상단에서 물고,
웹 프로세스에 Playwright 가 들어오는 순간 "웹은 브라우저를 안 만진다" 는
결정이 깨진다. 탐색 실행은 별도 워커 프로세스의 일이다.
`tests/test_web_api.py` 가 서브프로세스로 이 불변식을 고정한다.

### 커넥션은 요청마다 연다

리더가 커넥션을 오래 잡으면 스냅샷 때문에 WAL 체크포인트가 못 돌아,
6시간 탐색이 도는 동안 WAL 파일이 계속 자란다. SQLite 커넥션 오픈은
마이크로초라 요청마다 열고 닫는 비용은 없는 셈이다.

### 인증이 없다

로컬 전용(127.0.0.1)이라는 전제 위에 서 있다. 루프백 밖에 바인드하면
run_web.py 가 경고를 찍는다 — 수집한 pano 좌표가 LAN 에 노출되는 것이고,
배포로 범위를 넓히려면 약관(docs/23 §2)부터 다시 봐야 한다.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trailwalk import config as secrets
from trailwalk import settings as settings_mod
from trailwalk import store

APP_DIR = Path(__file__).resolve().parent.parent          # app/
IMAGES_ROOT = APP_DIR / "runs" / "images"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class LabelIn(BaseModel):
    pano_id: str
    is_trail: bool
    note: str | None = None
    heading: float | None = None      # None = 이 pano 전체 (기본)


class JobIn(BaseModel):
    lat: float
    lng: float
    bearing: float = 0.0
    radius_m: float                   # 기본값은 프론트가 /api/config 로 받아 채운다
    max_seconds: float | None = None  # None = 설정(budget.max_seconds)
    config_path: str | None = None    # 레포 밖 절대경로 허용 — vlm.url 이 든 설정은
    #                                   public 레포 밖(bench-runs/)에 산다


def get_conn(request: Request):
    # cross_thread: FastAPI 가 이 제너레이터의 finally 를 다른 스레드에서
    # 돌릴 수 있다 (→ store.connect 도크스트링)
    conn = store.connect(request.app.state.db, cross_thread=True)
    try:
        yield conn
    finally:
        conn.close()


def create_app(st: settings_mod.Settings, db: Path) -> FastAPI:
    app = FastAPI(title="trailwalk", docs_url=None, redoc_url=None)
    app.state.db = Path(db)
    app.state.settings = st
    conn = store.connect(db)
    store.migrate(conn)
    conn.close()

    @app.middleware("http")
    async def no_store_header(request, call_next):
        # 판정·라벨은 계속 바뀐다. 브라우저 캐시가 옛 지도를 보여주면
        # "라벨을 달았는데 안 보인다" 류의 가짜 버그를 만든다
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/api/config")
    def api_config(request: Request):
        st = request.app.state.settings
        # JS 키는 브라우저로 나가야 SDK 를 로드할 수 있다 — JS 키의 정상
        # 사용 방식이고, 보호는 값이 아니라 Kakao 콘솔의 도메인 등록이 한다.
        # (REST 키는 절대 여기로 내보내지 않는다 → trailwalk/config.py)
        try:
            key, key_error = secrets.kakao_appkey(), None
        except RuntimeError as e:
            # 키가 없어도 서버는 뜬다 — 프론트가 이 문구를 그대로 보여주는
            # 것이 traceback 보다 낫다 (안내문에 해결 방법이 들어 있다)
            key, key_error = None, str(e)
        return {"kakao_js_key": key, "kakao_key_error": key_error,
                "center": list(st.run.start),
                "prompt_version": st.vlm.prompt_version,
                "min_nature_level": st.vlm.min_nature_level,
                "require_footway": st.vlm.require_footway,
                # 잡 폼의 기본값 — 정본은 설정이고 프론트는 채워 보여줄 뿐
                "defaults": {"radius_m": st.budget.max_distance_m,
                             "max_seconds": st.budget.max_seconds}}

    @app.get("/api/versions")
    def api_versions(conn=Depends(get_conn)):
        return {"versions": store.prompt_versions(conn)}

    @app.get("/api/panos")
    def api_panos(request: Request,
                  s: float, w: float, n: float, e: float,
                  prompt_version: str | None = None,
                  run_id: int | None = None,
                  headings: bool = False,
                  limit: int = Query(3000, ge=1, le=10000),
                  conn=Depends(get_conn)):
        if not (s < n and w < e):
            raise HTTPException(422, "bbox 가 뒤집혀 있다 — s<n, w<e 여야 한다")
        st = request.app.state.settings
        # 기본 필터 = 현재 프롬프트 버전. MAX 는 한 pano 안 방위들 사이의
        # 규칙이라, 버전을 가로질러 걸면 폐기된 버전의 오탐 하나가 그 점을
        # 영원히 초록으로 만든다 (→ store.run_ids_for)
        version = None if run_id else (prompt_version or st.vlm.prompt_version)
        ids = store.run_ids_for(conn, prompt_version=version, run_id=run_id)
        if not ids:
            # 모르는 버전/런이 조용히 빈 지도가 되면 "이 지역엔 산책로가
            # 없다" 와 구분이 안 된다 — 파라미터 오류는 에러로 말한다
            what = f"run_id {run_id}" if run_id else f"버전 {version!r}"
            raise HTTPException(
                404, f"판정이 하나도 없는 {what} 다. "
                     f"/api/versions · /api/runs 에서 있는 것을 확인할 것")
        rows, truncated = store.viewport(conn, s=s, w=w, n=n, e=e, run_ids=ids,
                                         limit=limit, with_headings=headings)
        return {"panos": rows, "truncated": truncated}

    @app.get("/api/pano/{pano_id}")
    def api_pano(pano_id: str, conn=Depends(get_conn)):
        d = store.pano_detail(conn, pano_id)
        if d is None:
            raise HTTPException(404, "모르는 pano 다")
        return d

    @app.get("/api/image/{verdict_id}")
    def api_image(verdict_id: int, conn=Depends(get_conn)):
        # 경로를 URL 로 받지 않는다 — verdict_id 로 DB 에서 찾고, runs/images
        # 밖을 가리키면(조작된 행이라도) 존재 여부와 무관하게 404 다
        rel = store.image_path_of(conn, verdict_id)
        detail = {"reason": "no_image",
                  "message": "이 판정은 이미지를 저장하지 않았다 — "
                             "run.save_images 가 꺼진 런이다 (옛 런 전부)"}
        if rel is None:
            raise HTTPException(404, detail)
        full = (IMAGES_ROOT / rel).resolve()
        if not full.is_relative_to(IMAGES_ROOT.resolve()) or not full.is_file():
            raise HTTPException(404, detail)
        return FileResponse(full)

    @app.get("/api/runs")
    def api_runs(limit: int = Query(100, ge=1, le=1000), conn=Depends(get_conn)):
        return {"runs": store.runs_list(conn, limit)}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: int, conn=Depends(get_conn)):
        d = store.run_detail(conn, run_id)
        if d is None:
            raise HTTPException(404, "모르는 run 이다")
        return d

    @app.put("/api/labels")
    def api_put_label(body: LabelIn, conn=Depends(get_conn)):
        try:
            return store.put_label(conn, pano_id=body.pano_id,
                                   is_trail=body.is_trail, note=body.note,
                                   heading=body.heading, author="web")
        except sqlite3.IntegrityError as e:
            # FK — 지도에 없는 pano. 오타이지 데이터가 아니다
            raise HTTPException(404, f"모르는 pano 다: {body.pano_id}") from e

    @app.delete("/api/labels/{label_id}", status_code=204)
    def api_delete_label(label_id: int, conn=Depends(get_conn)):
        if not store.delete_label(conn, label_id):
            raise HTTPException(404, "모르는 라벨이다")

    @app.get("/api/labels/export")
    def api_labels_export(conn=Depends(get_conn)):
        # 제너레이터로 스트리밍하면 의존성 정리(conn.close)가 먼저 돌아
        # 닫힌 커넥션을 읽게 된다. 라벨은 사람이 단 수백 건 규모라
        # 통째로 만들어 보낸다. 형식의 정본은 store.iter_labels 하나다
        lines = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                        for r in store.iter_labels(conn))
        return Response(content=lines, media_type="application/x-ndjson",
                        headers={"Content-Disposition":
                                 'attachment; filename="web_labels.jsonl"'})

    @app.post("/api/jobs", status_code=201)
    def api_post_job(request: Request, body: JobIn, conn=Depends(get_conn)):
        st = request.app.state.settings
        if not (33.0 < body.lat < 39.5 and 124.0 < body.lng < 132.0):
            raise HTTPException(422, "좌표가 한반도 밖이다 — 위경도 순서를 확인")
        if not (0 < body.radius_m <= 10000):
            raise HTTPException(422, "반경은 0~10km 사이여야 한다")
        # config 미지정이면 web.job_config — 정본의 provider 가 fixture 라,
        # 이 기본값 없이는 웹의 "여기서 탐색" 이 합성 격자를 돌게 된다
        cfg_path = body.config_path or (
            str(APP_DIR / st.web.job_config) if st.web.job_config else None)
        return store.enqueue_job(
            conn, start_lat=body.lat, start_lng=body.lng, bearing=body.bearing,
            radius_m=body.radius_m,
            max_seconds=body.max_seconds or st.budget.max_seconds,
            config_path=cfg_path)

    @app.get("/api/jobs")
    def api_jobs(limit: int = Query(50, ge=1, le=500), conn=Depends(get_conn)):
        return {"jobs": store.jobs_list(conn, limit)}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int, conn=Depends(get_conn)):
        j = store.job_row(conn, job_id)
        if j is None:
            raise HTTPException(404, "모르는 잡이다")
        return j

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel_job(job_id: int, conn=Depends(get_conn)):
        j = store.request_cancel(conn, job_id)
        if j is None:
            raise HTTPException(404, "모르는 잡이다")
        return j

    @app.get("/api/health")
    def api_health(request: Request, conn=Depends(get_conn)):
        return {"db": str(request.app.state.db), "counts": store.counts(conn)}

    if STATIC_DIR.exists():
        # html=True 라 GET / 이 index.html 이 된다
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
