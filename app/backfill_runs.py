#!/usr/bin/env python
"""옛 런로그 JSONL → SQLite 정본. 유지보수 도구다.

    python app/backfill_runs.py --dry-run     # 무엇이 들어갈지만 본다
    python app/backfill_runs.py               # app/runs/*.jsonl 전부
    python app/backfill_runs.py --force       # 내용이 바뀐 파일을 다시 넣는다

런 진입점이 아니라서 인자가 `--config` 하나가 아니다 — check_*.py 와 같은
진단·유지보수 예외다 (→ app/CLAUDE.md 규칙 2).

무엇을 고르나 — **파일 이름으로 판단하지 않는다:**
  1. 경로는 app/runs/*.jsonl 로 한정한다. labels/ 의 samples.jsonl 이
     run_start/event/run_end 어휘를 흉내내므로 경로 제한이 1차 방어선이다.
  2. 첫 줄이 type=run_start 이고 provider=kakao 여야 한다. fixture 런은
     합성 이미지라 지도에 올릴 장소가 없다 — 제외.
  3. labels_path 가 있으면 eval 런이다 — eval 경로는 아직 JSONL 을 쓰므로
     건너뛴다 (eval 이관 때 함께 온다).

헤더는 세대가 셋이다(mode 없음 → config_path 없음 → 현행). config 딕트의
키 집합이 세대마다 겹치지 않으므로 **해석하지 않고** header_json 에 통째로
보관한다 — 열로 풀면 절반이 NULL 인 컬럼이 열 개 생긴다.

멱등성: run.name(파일 stem, UNIQUE) + source_sha256. 같은 이름·같은 해시는
건너뛰고, 해시가 다르면(파일이 이어 쓰였다) --force 없이는 거부한다.
파일 하나 = 트랜잭션 하나 — 깨진 줄을 만나면 그 파일만 통째로 롤백한다.
"""
import argparse
import contextlib
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import settings, store
from trailwalk import warn as warn_mod

APP = Path(__file__).resolve().parent


def parse_runlog(path: Path) -> dict | None:
    """파일 하나를 분해한다. 대상이 아니면 None, 깨졌으면 ValueError."""
    lines = path.read_text(encoding="utf-8").splitlines()
    first = next((line for line in lines if line.strip()), None)
    if first is None:
        return None
    header = json.loads(first)
    if header.get("type") != "run_start":
        return None                       # 런로그가 아니다 (metrics 파일 등)
    if header.get("provider") != "kakao":
        return None                       # fixture — 지도에 올릴 장소가 없다
    if "labels_path" in header:
        return None                       # eval — 아직 JSONL 경로를 쓴다

    probes, warnings, events, run_end = [], [], [], None
    for line in lines[1:]:
        if not line.strip():
            continue
        d = json.loads(line)              # 깨진 줄 → ValueError → 파일 롤백
        t = d.get("type")
        if t == "probe":
            probes.append(d)
        elif t == "warning":
            warnings.append(d)
        elif t == "event":
            events.append(d)
        elif t == "run_end":
            run_end = d
    return {"header": header, "probes": probes, "warnings": warnings,
            "events": events, "run_end": run_end}


def import_file(conn, path: Path, *, force: bool) -> tuple[str, dict | None]:
    """(결과, 요약). 결과 ∈ {imported, skipped, refused, not_runlog}."""
    parsed = parse_runlog(path)      # 대상 판정을 먼저 — 아래 DELETE 보다 앞이어야
    if parsed is None:               # --force 가 엉뚱한 런을 지우지 않는다
        return "not_runlog", None
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    old = conn.execute("SELECT run_id, source_sha256 FROM run WHERE name = ?",
                       (path.stem,)).fetchone()
    if old is not None:
        if old["source_sha256"] == sha and not force:
            return "skipped", None
        if not force:
            # 해시가 다르다 = 파일이 이어 쓰였다. 조용히 덮으면 어느 쪽이
            # 정본인지 알 수 없게 된다 — 사람이 --force 로 정하게 한다
            return "refused", None
        conn.execute("DELETE FROM run WHERE run_id = ?", (old["run_id"],))
    h = parsed["header"]
    prompt = h.get("prompt") or {}
    started = h.get("ts") or "1970-01-01T00:00:00+00:00"

    run_id = store.insert_run(
        conn, name=path.stem,
        # 옛 walk 런로그에는 mode 가 없다 (RunWriter 와 같은 규칙)
        kind=h.get("mode") or "walk",
        provider=h["provider"], source="backfill", source_sha256=sha,
        started_at=started,
        start_lat=(h.get("start") or [None, None])[0],
        start_lng=(h.get("start") or [None, None])[1],
        start_bearing=h.get("start_bearing"),
        prompt_version=prompt.get("system_version"),
        prompt_sha256=prompt.get("system_sha256"),
        schema_name=h.get("schema"), vlm_url=h.get("url"),
        header_json=json.dumps(h, ensure_ascii=False))

    img_dir = APP / "runs" / "images" / path.stem
    for d in parsed["probes"]:
        store.upsert_pano(conn, d["pano_id"], d["lat"], d["lng"], now=started)
        image_path = None
        if d.get("image") and (img_dir / d["image"]).exists():
            # 파일이 실제로 있을 때만 — 없는 경로를 적으면 웹이 404 를
            # "이미지 없음" 과 구분 못 한다
            image_path = f"{path.stem}/{d['image']}"
        store.insert_verdict(
            conn, run_id=run_id, pano_id=d["pano_id"], heading=d["heading"],
            step=d.get("step"), is_trail=bool(d["is_trail"]),
            confidence=d.get("confidence"),
            camera_surface=d.get("camera_surface"),
            nature_level=d.get("nature_level"), footway=d.get("footway"),
            prompt_tokens=d.get("prompt_tokens"),
            cached_tokens=d.get("cached_tokens"),
            completion_tokens=d.get("completion_tokens"),
            latency_ms=d.get("latency_ms"), src_format=d.get("src_format"),
            image_path=image_path, sample_id=d.get("sample_id"),
            label=d.get("label"),
            # probe 줄에는 시각이 없다(run_start 에만 ts) — 시작 시각으로
            # 근사한다. 근사라는 사실은 source='backfill' 이 말해 준다
            created_at=started)

    immediate = set()
    for d in parsed["warnings"]:
        immediate.add(d.get("code"))
        store.insert_warning(conn, run_id=run_id, kind="once", code=d["code"],
                             message=d.get("message", d["code"]),
                             count=d.get("count"), detail=d.get("detail"),
                             created_at=started)
    for d in parsed["events"]:
        payload = {k: v for k, v in d.items() if k not in ("type", "kind")}
        store.insert_event(conn, run_id=run_id, kind=d.get("kind", "?"),
                           payload=payload, created_at=started)

    end = parsed["run_end"]
    if end is not None:
        # run_end.warnings 에는 즉시(once) 줄과 집계형이 함께 실린다 —
        # 즉시 줄은 위에서 이미 넣었으니 나머지가 집계형이다
        for w in end.get("warnings", []):
            if w.get("code") in immediate:
                continue
            store.insert_warning(conn, run_id=run_id, kind="tally",
                                 code=w["code"],
                                 message=w.get("message", w["code"]),
                                 count=w.get("count"), detail=w.get("detail"),
                                 created_at=started)
        wall = float(end.get("wall_s") or 0.0)
        # run_end 에는 시각이 없다 — 시작 + wall_s 로 복원한다.
        # ts 가 파싱 불가면 finished 는 NULL 로 남는다 (지어내지 않는다)
        finished = None
        with contextlib.suppress(ValueError):
            finished = (datetime.fromisoformat(started)
                        + timedelta(seconds=wall)).isoformat(timespec="seconds")
        summary = {k: v for k, v in end.items()
                   if k not in ("type", "stop_reason", "wall_s", "warnings")}
        store.finish_run(conn, run_id, wall_s=wall, finished_at=finished,
                         stop_reason=end.get("stop_reason"),
                         summary=summary or None)
    else:
        # 중간에 죽은 런. 실패가 아니다 — 기록된 판정까지는 유효하고,
        # finished_at IS NULL 이 그 표시다. 경고로도 남긴다
        w = warn_mod.make("truncated_runlog")
        store.insert_warning(conn, run_id=run_id, kind="once",
                             code="truncated_runlog", message=w["message"],
                             created_at=started)

    return "imported", {"name": path.stem, "probes": len(parsed["probes"]),
                        "version": prompt.get("system_version"),
                        "truncated": end is None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glob", default=None,
                    help="입력 파일 glob (기본: app/runs/*.jsonl)")
    ap.add_argument("--db", default=None,
                    help="대상 DB (기본: 설정 web.db)")
    ap.add_argument("--dry-run", action="store_true",
                    help="넣지 않고 무엇이 들어갈지만 보고한다")
    ap.add_argument("--force", action="store_true",
                    help="내용(sha)이 바뀐 파일을 지우고 다시 넣는다")
    a = ap.parse_args()

    files = (sorted(Path().glob(a.glob)) if a.glob
             else sorted((APP / "runs").glob("*.jsonl")))
    db = Path(a.db) if a.db else APP / settings.load(None).web.db
    conn = store.connect(db)
    store.migrate(conn)

    counts = {"imported": 0, "skipped": 0, "refused": 0, "not_runlog": 0}
    rows = []
    by_version: dict[str | None, int] = {}
    for f in files:
        try:
            result, info = import_file(conn, f, force=a.force)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            # 이 파일만 통째로 롤백한다 — 부분 임포트는 "몇 건이 들어갔나" 를
            # 영영 알 수 없게 만든다
            conn.rollback()
            print(f"✗ {f.name}: 깨진 줄 — 롤백함 ({type(e).__name__}: {e})",
                  file=sys.stderr)
            continue
        counts[result] += 1
        if result == "refused":
            print(f"✗ {f.name}: 이름은 같은데 내용(sha)이 다르다 — 파일이 "
                  f"이어 쓰였다는 뜻. 다시 넣으려면 --force", file=sys.stderr)
        if info:
            rows.append(info)
            by_version[info["version"]] = \
                by_version.get(info["version"], 0) + info["probes"]
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()

    label = "들어갈 것" if a.dry_run else "들어감"
    print(f"\n파일 {len(files)}개 — {label} {counts['imported']} · "
          f"이미 있음 {counts['skipped']} · 거부 {counts['refused']} · "
          f"런로그 아님 {counts['not_runlog']}")
    for r in rows:
        note = " ⚠ run_end 없음" if r["truncated"] else ""
        print(f"  {r['name']}: 판정 {r['probes']} ({r['version']}){note}")
    if by_version:
        print("\n프롬프트 버전별 판정 수:")
        for v, n in sorted(by_version.items(), key=lambda kv: str(kv[0])):
            print(f"  {v}: {n}")
    if not a.dry_run:
        total = conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0]
        panos = conn.execute("SELECT COUNT(*) FROM pano").fetchone()[0]
        print(f"\nDB 총계: 판정 {total} · pano {panos} → {db}")
    conn.close()
    return 1 if counts["refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
