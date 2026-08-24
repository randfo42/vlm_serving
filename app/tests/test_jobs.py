"""잡 큐 — 원자적 claim 과 죽은 워커, 취소의 단조성.

VLM 도 브라우저도 없이 돈다. 지키는 것:

- claim 은 단일 UPDATE 라 두 워커가 동시에 불러도 하나만 얻는다
  (데몬이 하나라는 운영 전제를 믿지 않는다 — 재시작 중첩은 흔하다)
- 죽은 워커의 잡은 failed 로 접되 **자동 재큐잉하지 않는다** — 6시간짜리가
  절반에서 조용히 다시 시작되면 판정이 두 벌 쌓인다
- 취소: queued 는 즉시, 실행 중은 cancel_requested 로 — 실제 중단은
  explore 의 후보 경계에서. 워커의 결말 매핑(done/failed/canceled)
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from trailwalk import store
from trailwalk.runner import RunOutcome

_spec = importlib.util.spec_from_file_location(
    "run_worker", Path(__file__).resolve().parent.parent / "run_worker.py")
run_worker = importlib.util.module_from_spec(_spec)
sys.modules["run_worker"] = run_worker
_spec.loader.exec_module(run_worker)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    c = store.connect(p)
    store.migrate(c)
    c.close()
    return p


def enqueue(conn, **kw):
    base = dict(start_lat=37.5, start_lng=127.0, bearing=0.0,
                radius_m=300.0, max_seconds=600.0)
    return store.enqueue_job(conn, **{**base, **kw})


def test_claim은_두_커넥션_중_하나만_얻는다(db):
    c1, c2 = store.connect(db), store.connect(db)
    enqueue(c1)
    j1 = store.claim_job(c1, "w1")
    j2 = store.claim_job(c2, "w2")
    got = [j for j in (j1, j2) if j]
    assert len(got) == 1, "같은 잡을 둘이 집었다"
    assert got[0]["state"] == "claimed" and got[0]["worker_id"] in ("w1", "w2")
    c1.close(), c2.close()


def test_claim은_먼저_넣은_잡부터(db):
    c = store.connect(db)
    a, b = enqueue(c), enqueue(c)
    assert store.claim_job(c, "w")["job_id"] == a["job_id"]
    assert store.claim_job(c, "w")["job_id"] == b["job_id"]
    assert store.claim_job(c, "w") is None
    c.close()


def test_죽은_워커의_잡은_failed로_접고_재큐잉하지_않는다(db):
    c = store.connect(db)
    j = enqueue(c)
    store.claim_job(c, "w1")
    # 하트비트를 과거로 밀어 죽은 워커를 흉내낸다
    c.execute("UPDATE job SET heartbeat_at = '2020-01-01T00:00:00+00:00' "
              "WHERE job_id = ?", (j["job_id"],))
    c.commit()
    assert store.reap_stale_jobs(c, lease_s=180.0) == 1
    row = store.job_row(c, j["job_id"])
    assert (row["state"], row["error"]) == ("failed", "worker_lost")
    assert store.claim_job(c, "w2") is None, "죽은 잡이 다시 큐에 들어갔다"
    c.close()


def test_살아있는_잡은_안_접는다(db):
    c = store.connect(db)
    enqueue(c)
    store.claim_job(c, "w1")           # 하트비트 = 방금
    assert store.reap_stale_jobs(c, lease_s=180.0) == 0
    c.close()


def test_취소_queued는_즉시_running은_요청만(db):
    c = store.connect(db)
    a, b = enqueue(c), enqueue(c)
    assert store.request_cancel(c, a["job_id"])["state"] == "canceled"
    store.claim_job(c, "w")            # b 를 집는다 (a 는 canceled 라 건너뜀)
    r = store.request_cancel(c, b["job_id"])
    assert r["state"] == "claimed" and r["cancel_requested"] == 1
    assert store.cancel_requested(c, b["job_id"])
    c.close()


def test_취소된_queued_잡은_claim_안_된다(db):
    c = store.connect(db)
    j = enqueue(c)
    store.request_cancel(c, j["job_id"])
    assert store.claim_job(c, "w") is None
    c.close()


def test_RunWriter가_잡_진행률을_판정과_같은_커밋에_싣는다(db, tmp_path):
    from tests.test_store import HEADER, FakeVerdict
    c = store.connect(db)
    j = enqueue(c)
    w = store.RunWriter(c, dict(HEADER), name="r-job", job_id=j["job_id"])
    w.probe(step=0, pano_id="P1", lat=37.5, lng=127.0, heading=0.0,
            verdict=FakeVerdict(), src_format="PNG")
    w.probe(step=1, pano_id="P2", lat=37.5, lng=127.0, heading=90.0,
            verdict=FakeVerdict(), src_format="PNG")
    c2 = store.connect(db, read_only=True)     # 다른 커넥션(=웹)에서 보인다
    row = store.job_row(c2, j["job_id"])
    assert '"verdicts": 2' in row["progress_json"]
    c2.close(), c.close()


def test_make_cancel은_단조다(db):
    c = store.connect(db)
    j = enqueue(c)
    store.claim_job(c, "w")      # 워커는 집은 뒤에야 cancel 콜백을 만든다
    cancel = run_worker.make_cancel(c, j["job_id"], every_s=0.0)
    assert cancel() is False
    store.request_cancel(c, j["job_id"])
    assert cancel() is True
    # DB 가 어찌 되든 한 번 참이면 계속 참 — 루프 도중 되살아나면 안 된다
    c.execute("UPDATE job SET cancel_requested = 0")
    c.commit()
    assert cancel() is True
    c.close()


def _outcome(run_id, **kw):
    base = dict(ok=True, stop_reason="exhausted", warnings=[], run_id=run_id,
                verdicts=5, wall_s=1.0)
    return RunOutcome(**{**base, **kw})


@pytest.mark.parametrize("kw,state,err", [
    ({}, "done", None),
    (dict(ok=True, stop_reason="canceled"), "canceled", None),
    (dict(ok=False, stop_reason="vlm_error",
          warnings=[{"code": "vlm_error", "message": "서버에 닿지 못했다"}]),
     "failed", "서버에 닿지 못했다"),
])
def test_워커의_결말_매핑(db, monkeypatch, kw, state, err):
    c = store.connect(db)
    # job.run_id 는 run FK 다 — 진짜 run 행을 만들어 쓴다
    rid = store.insert_run(c, name="r", kind="explore", provider="kakao",
                           source="live", started_at="2026-08-24T00:00:00+00:00",
                           header_json="{}")
    c.commit()
    enqueue(c)
    job = store.claim_job(c, "w")
    outcome = _outcome(rid, **kw)
    monkeypatch.setattr(run_worker, "run_explore", lambda *a, **k: outcome)
    run_worker.process(c, db, job, "w")
    row = store.job_row(c, job["job_id"])
    assert (row["state"], row["stop_reason"], row["run_id"]) == \
        (state, outcome.stop_reason, rid)
    assert row["error"] == err
    c.close()


def test_워커가_터져도_잡은_열린_채_남지_않는다(db, monkeypatch):
    c = store.connect(db)
    enqueue(c)
    job = store.claim_job(c, "w")

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_worker, "run_explore", boom)
    with pytest.raises(KeyboardInterrupt):
        run_worker.process(c, db, job, "w")
    row = store.job_row(c, job["job_id"])
    assert (row["state"], row["error"]) == ("failed", "worker_stopped")
    c.close()
