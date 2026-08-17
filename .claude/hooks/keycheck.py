#!/usr/bin/env python3
"""app/.env 을 값 없이 점검한다.

    python3 .claude/hooks/keycheck.py

block-secret-reads.py 훅이 .env 를 읽는 모든 경로를 막기 때문에, 그래도 답해야 할
질문들("키가 있긴 한가", "이름을 잘못 썼나", "커밋될 위험은 없나")에 답하는 창구다.

**값은 어떤 경우에도 찍지 않는다.** 대신:

  · 길이       — 붙여넣다 잘렸는지 알 수 있다
  · sha256[:8] — 같은 키인지 다른 키인지 비교할 수 있다. 원문 복원은 불가능하다
  · gitignore  — 커밋될 위험이 있는지

키가 유효한지(살아 있는지)는 여기서 알 수 없다. 그건 실제로 API 를 한 번
불러봐야 안다.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def ignored(path: Path) -> bool:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def report(path: Path) -> int:
    rel = path.relative_to(ROOT)
    if not path.exists():
        print(f"✗ {rel} 없음")
        return 1

    ok = ignored(path)
    mode = oct(path.stat().st_mode & 0o777)[2:]
    print(f"{rel}  ({path.stat().st_size} bytes, mode {mode})")
    print(f"  gitignore: {'✓ 무시됨' if ok else '✗ 추적 위험 — .gitignore 확인할 것'}")

    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = LINE.match(raw)
        if not m:
            print("  ⚠ 파싱 불가한 줄이 있다 (KEY=VALUE 형식이 아님)")
            continue
        name, value = m.group(1), unquote(m.group(2))
        n += 1
        if not value:
            print(f"  {name:24} ✗ 비어 있음")
            continue
        fp = hashlib.sha256(value.encode()).hexdigest()[:8]
        print(f"  {name:24} {len(value):3d}자  sha256:{fp}")

    if n == 0:
        print("  ✗ 키가 하나도 없다")
        return 1
    # 파일 끝 개행이 없어도 대부분의 파서는 괜찮지만, 사람이 편집하다 다음 키를
    # 같은 줄에 붙이는 사고가 있어 한 번 알려준다.
    if not path.read_bytes().endswith(b"\n"):
        print("  · 파일 끝에 개행이 없다 (동작에는 지장 없음)")
    return 0


if __name__ == "__main__":
    targets = [Path(a) for a in sys.argv[1:]] or [ROOT / "app" / ".env"]
    raise SystemExit(max(report(t.resolve()) for t in targets))
