#!/usr/bin/env python
"""탐색 워커 데몬 — 큐(job 테이블)에서 잡을 집어 explore 를 돌린다.

    python app/run_worker.py
    python app/run_worker.py --config app/config/other.yaml

**CLI 인자는 `--config` 하나뿐이다** (→ CLAUDE.md "설정").

Playwright 는 이 프로세스에만 들어온다 — 웹(run_web.py)은 큐에 넣기만 하고
브라우저를 절대 안 만진다. provider 는 잡마다 새로 만들고 잡마다 닫는다
(runner.run_explore 안에 있으므로 저절로 그렇다): 잡을 건너 브라우저를
재사용하면 한 잡의 오염(멎은 프레임 등)이 다음 잡으로 번진다.

하트비트는 두 축이다 — 데몬 스레드가 10초마다 heartbeat_at(프로세스 생존),
RunWriter.probe 가 progress_json(진행). 합치면 "살아 있는데 안 나아가는"
상태를 표현할 수 없는데, 멎은 Playwright 호출이 정확히 그 증상이다.
"""
import argparse
import contextlib
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import settings, store
from trailwalk.runner import RunRequest, run_explore

APP = Path(__file__).resolve().parent
HEARTBEAT_S = 10.0


def make_cancel(conn, job_id: int, every_s: float = 2.0):
    """DB 폴링을 스로틀한 취소 콜백. 한 번 참이면 계속 참 — 취소는 단조다."""
    state = {"t": 0.0, "v": False}

    def cancel() -> bool:
        if state["v"]:
            return True
        now = time.monotonic()
        if now - state["t"] >= every_s:
            state["t"] = now
            state["v"] = store.cancel_requested(conn, job_id)
        return state["v"]

    return cancel


def process(conn, db: Path, job: dict, worker_id: str) -> None:
    """잡 하나. 어떤 결말이든 job 행이 닫힌다."""
    jid = job["job_id"]
    store.heartbeat_job(conn, jid, state="running")

    stop_hb = threading.Event()

    def beat():
        # 스레드는 자기 커넥션을 쓴다 — sqlite 커넥션은 스레드 간 공유가 안 된다
        c = store.connect(db)
        try:
            while not stop_hb.wait(HEARTBEAT_S):
                store.heartbeat_job(c, jid)
        finally:
            c.close()

    hb = threading.Thread(target=beat, daemon=True, name=f"hb-{jid}")
    hb.start()
    try:
        out = run_explore(
            RunRequest(start=(job["start_lat"], job["start_lng"]),
                       bearing=job["bearing"], radius_m=job["radius_m"],
                       max_seconds=job["max_seconds"],
                       config_path=job["config_path"]),
            db=db, cancel=make_cancel(conn, jid), job_id=jid)
        # canceled 는 실패가 아니다 — 부분 결과는 유효하고 run_id 도 남는다
        state = ("canceled" if out.stop_reason == "canceled"
                 else "done" if out.ok else "failed")
        error = None
        if state == "failed":
            error = next((w["message"] for w in out.warnings
                          if w["code"] == out.stop_reason), out.stop_reason)
        store.finish_job(conn, jid, state=state, run_id=out.run_id,
                         stop_reason=out.stop_reason, error=error)
        print(f"잡 {jid}: {state} ({out.stop_reason}) · 판정 {out.verdicts} · "
              f"{out.wall_s:.0f}s")
    except BaseException:
        # runner 가 예외를 안 내보내는 것이 계약이라 여기 오면 워커 버그거나
        # KeyboardInterrupt 다. 어느 쪽이든 잡을 열린 채 두지 않는다 —
        # 재큐잉하지 않는 이유는 reap_stale_jobs 와 같다
        store.finish_job(conn, jid, state="failed", error="worker_stopped")
        raise
    finally:
        stop_hb.set()
        hb.join(timeout=2)


def check_vlm(st) -> None:
    """서버 부재를 잡을 집기 **전에** 알린다 — 집고 나서 vlm_error 로 접히면
    큐가 실패 기록으로만 채워진다."""
    u = urlsplit(st.vlm.url)
    health = urlunsplit((u.scheme, u.netloc, "/health", "", ""))
    try:
        with urllib.request.urlopen(health, timeout=3) as r:
            if r.status == 200:
                return
    except OSError:
        pass
    print(f"⚠  VLM 서버({u.netloc})의 /health 에 닿지 못했다 — 이대로 잡을\n"
          f"   집으면 전부 vlm_error 로 끝난다. 서버부터 확인할 것"
          f" (./configs/smoke.sh)", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None,
                    help=f"설정 파일 경로 (기본: {settings.DEFAULT_PATH})")
    a = ap.parse_args()

    try:
        st = settings.load(a.config)
    except settings.SettingsError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    db = APP / st.web.db
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    conn = store.connect(db)
    store.migrate(conn)
    check_vlm(st)
    print(f"워커 {worker_id} — DB {db}\n큐 대기 중 (poll {st.web.worker_poll_s}s)")

    try:
        while True:
            reaped = store.reap_stale_jobs(conn, st.web.worker_lease_s)
            if reaped:
                print(f"⚠  죽은 워커의 잡 {reaped}개를 failed 로 접었다 "
                      f"(재큐잉 안 함 — 다시는 사람이 누른다)")
            job = store.claim_job(conn, worker_id)
            if job is None:
                time.sleep(st.web.worker_poll_s)
                continue
            print(f"잡 {job['job_id']} 집음: ({job['start_lat']:.5f},"
                  f"{job['start_lng']:.5f}) 반경 {job['radius_m']:.0f}m")
            process(conn, db, job, worker_id)
    except KeyboardInterrupt:
        print("\n워커 종료")
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
