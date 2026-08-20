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
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# 훅은 스크립트로 실행되므로 보통은 sys.path[0] 이 이 디렉터리다. 다만 테스트가
# importlib 로 불러올 때는 아니라서, 어느 쪽에서든 되도록 직접 넣는다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _shell import SEPARATORS, strip_heredocs

# 재귀 방지. 리뷰 에이전트도 이 프로젝트 설정을 물려받으므로, 그 안에서 커밋을
# 시도하면 또 리뷰가 돌 수 있다. 부모가 표식을 심고 자식은 그걸 보고 비켜난다.
GUARD = "TRAILWALK_IN_REVIEW"

# `git commit` 찾기. git 과 commit 사이에는 전역 옵션이 낀다.
#
# 처음엔 `git(?:\s+-\S+)*\s+commit` 이었는데 **`git -C /path commit` 을 놓쳤다.**
# `-C` 는 값을 따로 받는 옵션이라 `/path` 가 `-` 로 시작하지 않기 때문이다.
# 놓치면 리뷰가 조용히 안 돈다 — 이 훅에서 가장 나쁜 실패 방식이다.
#
# `git .* commit` 처럼 헐겁게 잡지는 않는다. `git log --grep=commit` 같은 것까지
# 걸려서 엉뚱한 명령마다 테스트가 돌게 된다. 값을 받는 전역 옵션만 나열한다.
# 구분자 사이에는 역슬래시 줄바꿈이 낄 수 있다: `git -C /repo \<개행> commit`.
# `\s` 는 역슬래시를 안 먹으므로 따로 넣어준다.
_SP = r"[\s\\]+"


def _git_cmd(sub: str) -> re.Pattern:
    return re.compile(
        r"(?<![\w./-])git"
        rf"(?:{_SP}(?:-[cC]{_SP}\S+"
        rf"|--(?:git-dir|work-tree|namespace|exec-path|config-env)(?:=\S+|{_SP}\S+)"
        r"|-\S+))*"
        rf"{_SP}(?:{sub})(?![\w-])")


GIT_COMMIT = _git_cmd("commit")
# 스테이징 명령. commit 과 한 명령에 묶였는지 볼 때 쓴다 (→ stages_before_commit).
GIT_STAGE = _git_cmd("add|rm|mv")

# 실측(2026-08-17): 10KB diff → 230~290초. 도구를 전부 빼도 230초라 병목은 탐색이
# 아니라 생성이다. 그래서 40KB 를 넘으면 리뷰를 건너뛴다 — 95KB 를 넣었더니 240초를
# 통째로 태우고 결국 아무 결과도 못 냈다. 못 할 일이면 빨리 포기하는 편이 낫다.
MAX_DIFF_BYTES = 40_000
AGENT_TIMEOUT_S = 480

# `git commit` 의 짧은 플래그 중 **값을 받지 않는** 것들. 묶음 플래그를 해석할 때
# 쓴다 (→ _has_short). 값을 받는 것(-m -F -c -C -t -u)이 앞에 오면 그 뒤는
# 플래그가 아니라 값이다.
NO_ARG_SHORT = set("aeinopqsvz")

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


def commit_flags(cmd: str) -> list[str]:
    """`git commit ...` 이 있는 **그 줄**의 토큰들.

    heredoc 본문은 다음 줄부터라 자연히 빠진다. 이 훅의 모든 플래그 판정이
    지나는 단일 출구다 — 여러 곳에서 각자 문자열을 훑으면 각자 다르게 틀린다.

    ⚠️ 역슬래시 줄바꿈은 따라간다. 그냥 첫 줄만 자르면 이렇게 쓴 플래그를 놓친다:

        git commit -m "메시지" \\
            --no-verify

    놓치면 `-a` 를 못 봐서 diff 를 빈 것으로 오판하고 검사가 통째로 스킵된다.
    (리뷰 에이전트가 이 회귀를 잡았다 — 첫 줄만 보게 바꾸면서 새로 생긴 것이다.)
    """
    m = GIT_COMMIT.search(cmd)
    if not m:
        return []
    parts: list[str] = []
    for raw in cmd[m.start():].split("\n"):
        # rstrip 하고 판단하면 안 된다. bash 는 역슬래시가 **개행 바로 앞**에 있을
        # 때만 줄을 잇는다. `\` 뒤에 공백이 있으면 그건 이어지는 줄이 아니라
        # 이스케이프된 공백이고, 그런데도 다음 줄을 끌어오면 실제로는 전달되지
        # 않은 플래그를 읽게 된다 — 그 방향의 실패가 '조용한 스킵' 이다.
        cont = raw.endswith("\\")
        parts.append(raw[:-1] if cont else raw)
        if not cont:
            break                       # 이어지지 않는다 — heredoc 본문은 여기서 끊긴다
    line = _cut_at_separator(" ".join(parts))
    try:
        return shlex.split(line, comments=False)
    except ValueError:
        return line.split()             # 따옴표가 안 닫힌 줄 — 대충이라도 본다


def _cut_at_separator(line: str) -> str:
    """뒤에 붙은 다른 명령을 잘라낸다.

    안 자르면 **뒤 명령의 플래그가 앞 명령 판정에 섞인다** — 실패 시 재시도하는
    흔한 패턴에서 바로 터진다:

        git commit -m x || git commit --no-verify -m x
                           ^^^^^^^^^^^^ 이게 앞 커밋까지 스킵시킨다

    토큰으로 나눈 뒤 자르면 안 된다. **공백 없이 붙은 `x||git` 을 shlex 가 한
    토큰으로 합쳐버려** 구분자를 못 찾는다 (리뷰 에이전트가 잡았다). 그래서
    문자열 단계에서 따옴표 상태를 보며 직접 훑는다 — 따옴표 안의 `||` 는
    구분자가 아니다.
    """
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"':
                i += 1                  # 큰따옴표 안의 이스케이프
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1                      # 따옴표 밖의 이스케이프
        else:
            for sep in SEPARATORS:
                if line.startswith(sep, i):
                    return line[:i]
        i += 1
    return line


def _has_short(tokens: list[str], letter: str) -> bool:
    """묶인 짧은 플래그에서 letter 를 찾는다 (`-am` 의 a).

    ⚠️ 단순히 `letter in token` 으로 보면 **`-uno` 가 `-n` 으로 오판된다.**
    `-uno` 는 `--untracked-files=no` 의 축약이고 'n' 은 값의 일부다. 그러면
    멀쩡한 커밋에서 리뷰가 통째로 건너뛰어진다 — 리뷰 에이전트가 이걸 잡았다.

    그래서 letter **앞의 글자가 전부 값을 안 받는 플래그일 때만** 인정한다.
    값을 받는 플래그가 앞에 있으면 그 뒤는 플래그가 아니라 값이기 때문이다.
    """
    for t in tokens:
        if not re.fullmatch(r"-[a-zA-Z]+", t):
            continue
        body = t[1:]
        i = body.find(letter)
        if i >= 0 and all(c in NO_ARG_SHORT for c in body[:i]):
            return True
    return False


def skips_verify(cmd: str) -> bool:
    """`--no-verify` 가 **플래그로** 주어졌는가.

    ⚠️ 실전에서 뚫린 지점이다. 처음엔 `"--no-verify" in cmd` 로 봤는데,
    커밋 메시지 본문에 그 문자열이 들어 있으면(이 훅을 설명하는 커밋이 딱
    그랬다) 리뷰가 통째로 건너뛰어졌다. 명령이 heredoc 을 쓰면 메시지 본문이
    command 문자열 안에 통째로 들어온다.

    이 실수는 `block-secret-reads.py` 가 겪은 것과 같은 종류다 — 명령줄을
    **파싱하지 않고 문자열로 훑으면** 언급과 실제 사용이 구분되지 않는다.
    다만 방향이 반대라 더 위험하다: 저쪽은 과잉 차단이라 즉시 눈에 띄지만,
    이쪽은 **조용히 통과**시킨다. 이 레포가 계속 당해온 그 유형이다.

    그래서 `git commit` 이 있는 **그 줄만** 잘라서 토큰으로 본다.
    heredoc 본문은 다음 줄부터이므로 자연히 빠진다.
    """
    tokens = commit_flags(cmd)
    return "--no-verify" in tokens or _has_short(tokens, "n")


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
    r"""무엇이 커밋되려는가.

    `git commit -a` 는 커밋 시점에 추적 중인 파일을 스테이징하므로, --cached 만
    보면 실제로 커밋될 것보다 좁다. 그래서 -a/--all 이면 `git diff HEAD` 를 본다.

    ⚠️ 처음엔 **--cached 가 비었을 때만** 폴백했고, -a 감지도 `\s-\w*a` 정규식이라
    `--all` 을 못 잡았다. 둘이 겹치면 리뷰가 통째로 건너뛰어진다: --cached 가
    비어 있는데 --all 이 안 잡히면 diff 가 빈 문자열이 되고, 그러면 main() 이
    "커밋할 게 없다" 로 오인해 조용히 통과시킨다. 리뷰 에이전트가 이걸 잡았다.
    """
    tokens = commit_flags(cmd)
    if "--all" in tokens or _has_short(tokens, "a"):
        return run(["git", "diff", "HEAD"], root).stdout
    return run(["git", "diff", "--cached"], root).stdout


def stages_before_commit(cmd: str) -> bool:
    """commit **앞 구간**에 스테이징 명령(git add/rm/mv)이 있는가.

    ⚠️ 실전에서 뚫린 지점이다 (2026-08-18). `git add X && git commit` 을 한 Bash
    명령으로 묶으면 훅이 도는 PreToolUse 시점엔 스테이지가 아직 비어 있다.
    --cached 만 보던 main() 은 "커밋할 게 없다" 로 오인해 조용히 통과시켰고,
    커밋 4개가 ruff·pytest 게이트조차 없이 들어갔다. 이 훅에서 가장 나쁜
    실패 방식 — 조용한 통과 — 이 또 이 유형이다.

    commit **앞**만 보는 이유: 커밋 메시지 본문(-m, heredoc)의 "git add" 언급은
    전부 commit 뒤에 오므로 자연히 빠진다. `--no-verify` 가 겪은 언급-오인을
    같은 방식으로 피한다. 반대로 echo 등에 낀 언급이 앞에 있으면 잡히는데,
    그 방향의 오류는 리뷰가 한 번 더 도는 것뿐이라 안전하다 — 애매하면
    리뷰를 돌리는 쪽으로 틀린다.
    """
    m = GIT_COMMIT.search(cmd)
    return bool(m and GIT_STAGE.search(cmd[:m.start()]))


def worktree_diff(root: Path) -> str:
    """스테이지가 아직 비어 있을 때의 best-effort diff.

    추적 파일의 변경(`git diff HEAD`)에 미추적·비무시 파일의 전문을 이어붙인다.
    새 파일이 `git add` 되는 경우가 흔한데 diff HEAD 에는 안 보이기 때문이다.

    **과대근사다** — 이번 커밋에 안 들어갈 파일까지 섞일 수 있다. 리뷰 목적에는
    넓은 쪽이 안전하다: 좁으면 조용히 스킵되고, 넓으면 기껏해야 리뷰어가
    남는 파일을 몇 개 더 본다.
    """
    parts = [run(["git", "diff", "HEAD"], root).stdout]
    others = run(["git", "ls-files", "--others", "--exclude-standard"], root).stdout
    for f in others.splitlines():
        if f:
            parts.append(run(["git", "diff", "--no-index", "--", "/dev/null", f], root).stdout)
    return "".join(parts)


def gate_lint_and_tests(root: Path, py: str) -> str | None:
    """실패 사유를 돌려준다. 통과면 None.

    도구가 아예 없으면 통과시킨다 — 여기서 막으면 새 체크아웃에서 아무도
    커밋을 못 한다.
    """
    for label, cmd in (("ruff", [py, "-m", "ruff", "check", "."]),
                       ("pytest", [py, "-m", "pytest", "-q"])):
        try:
            r = run(cmd, root)
        except OSError:
            continue                        # 인터프리터 자체가 없다
        except subprocess.TimeoutExpired:
            return f"{label} 이(가) 시간 안에 안 끝났다"
        # ⚠️ `python -m ruff` 는 ruff 가 없어도 OSError 를 던지지 않는다. 인터프리터
        # 실행은 성공하고 "No module named ruff" 와 함께 종료코드만 비정상이 된다.
        # 이걸 실패로 세면 도구를 안 깐 새 체크아웃에서 아무도 커밋을 못 한다 —
        # 바로 위 docstring 이 약속한 것과 정반대다. 리뷰 에이전트가 잡았다.
        if f"No module named {label}" in r.stderr:
            continue
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

    return parse_findings(r.stdout)


def parse_findings(text: str) -> tuple[list[dict], str | None]:
    """에이전트 출력에서 findings 를 뽑는다.

    ⚠️ 처음엔 `\\{...findings.*\\}` 를 DOTALL 로 썼는데 **greedy 라 뒤쪽 `}` 까지
    통째로 삼켰다.** 에이전트가 JSON 뒤에 중괄호 섞인 설명을 붙이면 파싱이
    실패하고, 이 경로는 fail-open 이라 **blocking 지적이 있어도 조용히 통과**한다.
    리뷰는 돌았는데 결과만 버려지는 셈이라 가장 나쁜 실패다. 리뷰 에이전트가 잡았다.

    그래서 정규식으로 끝을 추측하지 않고 `raw_decode` 로 **JSON 이 실제로 끝나는
    지점까지만** 읽는다. 뒤에 무엇이 붙어 있든 무관해진다.
    """
    dec = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            found = obj["findings"]         # 여러 개면 마지막 것
    if found is None:
        return [], "리뷰 결과를 해석하지 못했다"
    return found, None


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
    # 히어독 **본문**은 명령이 아니라 데이터다 (→ _shell.py). 지우지 않으면
    # "이 훅은 git commit 을 가로챈다" 라고 적은 **무관한 명령**에서 정규식이
    # 걸리고, commit_flags 가 그 산문을 플래그로 토큰화한 채 게이트가 돈다.
    # 낭비로 끝나지 않는다 — 그 뒤 리뷰가 지금 스테이지된 diff 로 돌아서
    # 관계없는 명령을 차단하고, 그 명령에 묶여 있던 git add 까지 함께 죽는다
    # (PreToolUse 는 명령 **전체**를 막는다). 실제로 그렇게 당했다.
    #
    # 여기서 한 번만 지우고 아래는 전부 이걸 쓴다. 각 함수가 알아서 지우게
    # 하면 한 곳만 빠진 상태가 생긴다.
    cmd = strip_heredocs(data.get("tool_input", {}).get("command", "") or "")
    if not GIT_COMMIT.search(cmd):
        emit()
    if os.environ.get(GUARD):
        emit()                              # 리뷰 에이전트 자신의 커밋 — 재귀 차단
    if skips_verify(cmd):
        emit(context="리뷰 훅을 --no-verify 로 건너뛰었다.")

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    py = str(root / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable

    diff = staged_diff(root, cmd)
    if not diff.strip() and stages_before_commit(cmd):
        # `git add X && git commit` — 훅 시점엔 스테이지가 비어 있지만 커밋할 게
        # 없는 것이 아니다. 여기서 emit() 하면 검사가 통째로 조용히 스킵된다.
        diff = worktree_diff(root)
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
