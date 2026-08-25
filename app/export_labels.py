#!/usr/bin/env python
"""사람 라벨 내보내기/복원. DB 가 gitignore 라 이 파일이 라벨의 유일한 백업이다.

    python app/export_labels.py --out  ../bench-runs/web_labels.jsonl
    python app/export_labels.py --restore ../bench-runs/web_labels.jsonl

내보낸 파일에는 pano **좌표가 들어간다** — public 저장소에 커밋하지 말 것
(레포 밖 bench-runs/ 가 정위치다. gil.seoul 라벨 대장을 gitignore 하는 것과
같은 이유 → 루트 .gitignore).

양방향이 한 파일에 있는 이유: 레코드 형식이 하나라서다. 나누면 그 형식이
두 곳에 적힌다 — 형식의 정본은 store.iter_labels() 하나고, 웹의
/api/labels/export 도 같은 것을 쓴다.

유지보수 도구라 인자가 --config 하나가 아니다 (backfill_runs.py 와 같은 예외).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import settings, store

APP = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", help="라벨을 JSONL 로 내보낼 경로")
    g.add_argument("--restore", help="내보낸 JSONL 에서 되살린다")
    ap.add_argument("--db", default=None, help="대상 DB (기본: 설정 web.db)")
    a = ap.parse_args()

    db = Path(a.db) if a.db else APP / settings.load(None).web.db
    conn = store.connect(db)
    store.migrate(conn)

    if a.out:
        out = Path(a.out)
        n = 0
        with out.open("w", encoding="utf-8") as f:
            for rec in store.iter_labels(conn):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        print(f"라벨 {n}건 → {out}")
        if n == 0:
            print("⚠  내보낼 라벨이 없다 — 빈 파일이다", file=sys.stderr)
    else:
        counts = {"restored": 0, "kept": 0, "skipped": 0}
        for line in Path(a.restore).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") != "web_label":
                # labels/ 파이프라인의 labels.jsonl 등 다른 파일이 잘못
                # 들어와도 조용히 먹지 않는다 — 건수로 보고한다
                counts["skipped"] += 1
                continue
            counts[store.restore_label(conn, rec)] += 1
        print(f"되살림 {counts['restored']} · 이미 더 최신이라 유지 {counts['kept']}"
              + (f" · web_label 아님 {counts['skipped']}" if counts["skipped"] else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
