#!/usr/bin/env python3
"""커밋 직전 코드 리뷰 훅 — 게이트 두 개.

    git commit
        │
        ├─ 1단계  ruff + pytest        (빠르고 결정적. 여기서 걸리면 즉시 차단)
        │
        └─ 2단계  별도 리뷰 에이전트    (claude -p, 읽기 전용, 스테이지된 diff 만)
                    │
                    ├─ blocking 지적  → 커밋 차단
                    └─ advisory 지적  → 통과시키되 본문에 붙여 보여준다

### 설계 원칙 — 과잉 차단은 훅을 죽인다

이 레포에는 이미 같은 교훈이 한 번 있었다. `block-secret-reads.py` 의 첫 판은
`.env` 를 **언급**하기만 해도 막아서 커밋 메시지조차 못 쓰게 만들었다. 그런
훅은 곧 꺼지고, 꺼진 훅은 아무것도 막지 못한다.

그래서:

- 리뷰어가 죽거나(claude 없음·타임아웃·JSON 깨짐) 하면 **통과시킨다.**
  리뷰 인프라 고장으로 커밋이 막히면 사람이 훅부터 지운다
- 차단은 `blocking` 로 분류된 지적에만 건다. 취향·스타일은 절대 차단하지 않는다
- `--no-verify` 는 언제나 존중한다. 탈출구 없는 게이트는 게이트가 아니다

반대로 **1단계(ruff/pytest)는 실패하면 무조건 막는다.** 그건 판단이 아니라
사실이고, 깨진 테스트를 커밋할 이유는 없다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 재귀 방지. 리뷰 에이전트도 이 프로젝트 설정을 물려받으므로, 그 안에서 커밋을
# 시도하면 또 리뷰가 돌 수 있다. 부모가 표식을 심고 자식은 그걸 보고 비켜난다.
GUARD = "TRAILWALK_IN_REVIEW"

GIT_COMMIT = re.compile(r"(?<![\w./-])git(?:\s+-\S+)*\s+commit(?![\w-])")
MAX_DIFF_BYTES = 120_000        # 이보다 크면 리뷰어에게 통째로 주지 않는다
AGENT_TIMEOUT_S = 240

PROMPT = """\
아래는 이 저장소에서 커밋되기 직전의 스테이지된 diff 다. 코드 리뷰를 해라.

이 프로젝트의 성격을 먼저 알고 봐라. 로컬 VLM 서빙 + 로드뷰 산책로 탐색이고,
**실패가 예외 없이 조용히 일어나는 것**이 이 코드베이스의 핵심 위험이다.
과거 사고가 전부 그랬다: WEBP 를 보내면 HTTP 200 이 오는데 모델은 이미지를 못
봤고, system 프롬프트 1바이트가 바뀌면 프리픽스 캐시가 죽는데 에러는 안 났고,
덜 그려진 화면을 캡처해도 아무것도 실패하지 않고 탐색만 다른 길로 샜다.

그래서 다음을 우선순위로 봐라:

1. **조용한 실패** — 예외 없이 틀린 결과가 나오는 경로. 삼켜진 예외, 검증 없는
   성공 응답, 기본값으로 넘어가는 실패
2. **비밀값 누출** — 키 값이 로그·예외 메시지·URL·커밋 내용에 들어가는가
3. **재현성 파괴** — 같은 입력에 다른 결과가 나올 수 있는 변경
4. **명백한 버그** — 경계 조건, None, 뒤집힌 부등호, 잘못된 단위
5. 문서(docs/*.md)와 코드가 어긋나는가

하지 말아야 할 것:
- 스타일·취향·네이밍 지적 (ruff 가 이미 돌았다)
- diff 밖의 코드에 대한 일반론
- "테스트를 더 짜라" 같은 막연한 권고. 구체적인 결함만 말해라

필요하면 Read/Grep 으로 주변 코드를 확인해라. 추측으로 지적하지 마라.

마지막 줄에 **JSON 만** 출력해라. 다른 텍스트를 뒤에 붙이지 마라:

{"findings": [{"severity": "blocking|advisory", "file": "경로", "line": 정수,
"summary": "한 문장", "why": "이 입력에서 이렇게 깨진다"}]}

지적할 게 없으면 {"findings": []} 를 출력해라.
severity 는 실제로 깨지는 것만 blocking 이다. 확신이 없으면 advisory 다.

--- diff ---
%s
"""


def emit(decision: str | None = None, reason: str = "", context: str = "") -> None:
    """PreToolUse 훅 응답. decision 이 None 이면 조용히 통과."""
    out: dict = {}
    if decision:
        out["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    if context:
        out.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
        out["hookSpecificOutput"]["additionalContext"] = context
    if out:
        print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


def run(cmd: list[str], cwd: Path, timeout: int = 120, env: dict | None = None):
    # NO_COLOR: 출력이 훅 응답 JSON 을 거쳐 사람에게 그대로 보인다. ANSI 이스케이프가
    # 섞이면 정작 읽어야 할 실패 사유가 제어문자에 묻힌다.
    env = dict(env or os.environ, NO_COLOR="1", FORCE_COLOR="0")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


def staged_diff(root: Path, cmd: str) -> str:
    """무엇이 커밋되려는가.

    `git commit -a` 는 커밋 시점에 스테이징하므로 --cached 가 비어 있다.
    그 경우 추적 중인 파일의 워킹트리 변경까지 본다.
    """
    d = run(["git", "diff", "--cached"], root).stdout
    if not d.strip() and re.search(r"\s-\w*a", cmd):
        d = run(["git", "diff", "HEAD"], root).stdout
    return d


def gate_lint_and_tests(root: Path, py: str) -> str | None:
    """실패 사유를 돌려준다. 통과면 None.

    도구가 아예 없으면 통과시킨다 — 여기서 막으면 새 체크아웃에서 아무도
    커밋을 못 한다.
    """
    for label, cmd in (("ruff", [py, "-m", "ruff", "check", "."]),
                       ("pytest", [py, "-m", "pytest", "-q"])):
        try:
            r = run(cmd, root)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None if isinstance(e, OSError) else f"{label} 이(가) 시간 안에 안 끝났다"
        if r.returncode == 5 and label == "pytest":
            continue                        # 수집된 테스트 없음 — 실패가 아니다
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip().splitlines()[-25:]
            return f"{label} 실패\n\n" + "\n".join(tail)
    return None


def review(root: Path, diff: str) -> tuple[list[dict], str | None]:
    """별도 에이전트에게 리뷰를 시킨다. (지적목록, 실패사유)."""
    claude = shutil.which("claude")
    if not claude:
        return [], "claude CLI 를 찾지 못했다"

    env = dict(os.environ, **{GUARD: "1"})
    try:
        r = run([claude, "-p", PROMPT % diff,
                 "--permission-mode", "plan",          # 쓰기 금지. 리뷰는 읽기만 한다
                 "--allowedTools", "Read", "Grep", "Glob",
                 "--model", "sonnet"],                 # 리뷰는 가볍게. 판단은 사람이 한다
                root, timeout=AGENT_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return [], f"리뷰 에이전트가 {AGENT_TIMEOUT_S}초 안에 안 끝났다"
    if r.returncode != 0:
        return [], f"리뷰 에이전트 실패 (exit {r.returncode})"

    # 마지막 JSON 객체를 찾는다. 에이전트가 앞에 설명을 붙여도 견딘다.
    for m in reversed(list(re.finditer(r"\{[^{}]*\"findings\"\s*:.*\}", r.stdout, re.S))):
        try:
            got = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(got.get("findings"), list):
            return got["findings"], None
    return [], "리뷰 결과를 해석하지 못했다"


def fmt(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        loc = f.get("file", "?")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"  [{f.get('severity', '?')}] {loc}\n"
                     f"    {f.get('summary', '')}\n"
                     f"    → {f.get('why', '')}")
    return "\n".join(lines)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        emit()

    if data.get("tool_name") != "Bash":
        emit()
    cmd = data.get("tool_input", {}).get("command", "") or ""
    if not GIT_COMMIT.search(cmd):
        emit()
    if os.environ.get(GUARD):
        emit()                              # 리뷰 에이전트 자신의 커밋 — 재귀 차단
    if "--no-verify" in cmd or "-n " in f" {cmd} ":
        emit(context="리뷰 훅을 --no-verify 로 건너뛰었다.")

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    py = str(root / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable

    diff = staged_diff(root, cmd)
    if not diff.strip():
        emit()                              # 커밋할 게 없다. git 이 알아서 말한다

    failed = gate_lint_and_tests(root, py)
    if failed:
        emit("deny", f"커밋 전 검사가 실패했다.\n\n{failed}\n\n"
                     f"고치고 다시 커밋하거나, 의도한 것이면 --no-verify 로 넘길 것.")

    if len(diff) > MAX_DIFF_BYTES:
        emit(context=f"ruff·pytest 통과. diff 가 {len(diff)//1024}KB 라 "
                     f"에이전트 리뷰는 건너뛰었다.")

    findings, err = review(root, diff)
    if err:
        # 리뷰어 고장으로 커밋을 막지 않는다. 다만 리뷰가 안 돌았다는 사실은 알린다.
        emit(context=f"ruff·pytest 통과. 에이전트 리뷰는 돌지 못했다: {err}")

    blocking = [f for f in findings if f.get("severity") == "blocking"]
    if blocking:
        emit("deny", "리뷰 에이전트가 차단할 문제를 찾았다.\n\n" + fmt(blocking)
             + "\n\n고치거나, 동의하지 않으면 --no-verify 로 커밋할 것.")

    if findings:
        emit(context="ruff·pytest 통과. 리뷰 참고 의견:\n" + fmt(findings))
    emit(context="ruff·pytest 통과. 리뷰 에이전트가 지적 없음.")


if __name__ == "__main__":
    main()
