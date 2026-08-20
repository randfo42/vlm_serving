#!/usr/bin/env python3
"""PR 생성 직전 검증 훅 — 본문만 읽고 맥락을 알 수 있는가.

    gh pr create
        │
        └─ 별도 에이전트 (claude -p, 읽기 전용)
             │  받는 것: PR 제목·본문 + 브랜치 diff
             │  묻는 것: "이 레포를 처음 보는 사람이 이 본문만으로 알 수 있나"
             │
             ├─ blocking 지적  → PR 생성 차단
             └─ advisory 지적  → 통과시키되 본문에 붙여 보여준다

### 왜 이 훅이 있나

PR 본문은 **대화 맥락이 없는 사람**이 읽는다. 작성자에게는 자명한 것이
읽는 사람에게는 허공을 가리키는 참조가 된다 — "다른 브랜치에 참조 구현이
있다"(어느 브랜치?), "§9"(어느 문서의?), "이번에 지웠다"(무엇을, 언제?).
`review-on-commit.py` 가 코드의 조용한 실패를 잡는다면, 이 훅은 **설명의
조용한 실패**를 잡는다. 둘 다 "에러 없이 틀린 것" 이라는 점이 같다.

### 무엇으로 차단하나 — 주관을 차단 사유로 삼지 않는다

`block-secret-reads.py` 1판의 교훈이 여기에도 적용된다 (→ docs/12-harness.md §4).
"읽기 쉬운가" 로 막으면 취향 싸움이 되고, 그런 훅은 곧 꺼진다.

그래서 blocking 은 **확인 가능한 두 가지**뿐이다:

  1. 해석 불가능한 참조 — 가리키는 대상이 본문에도 diff 에도 없다
  2. 본문과 diff 의 불일치 — 안 한 일을 했다고 하거나, 큰 변경을 안 적었다

"더 친절하게 써라" 는 전부 advisory 다. 그리고 리뷰어가 죽으면 통과시킨다 —
검증 인프라 고장으로 PR 이 막히면 사람이 훅부터 지운다.

`--web` 은 본문이 브라우저에서 작성되므로 검사할 것이 없다. 건너뛴다.
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

# 재귀 방지. 검증 에이전트도 이 프로젝트 설정을 물려받는다.
GUARD = "TRAILWALK_IN_PR_REVIEW"

# `gh pr create`. gh 와 pr 사이에 전역 옵션이 낄 수 있다 (--repo 등).
GH_PR_CREATE = re.compile(
    r"(?<![\w./-])gh(?:\s+(?:--repo(?:=\S+|\s+\S+)|-R(?:=\S+|\s+\S+)|-\S+))*"
    r"\s+pr\s+create(?![\w-])")

# parse_pr 이 "이건 우리 일이 아니다" 를 말하는 값. 사람에게 알릴 것이 없다.
SILENT = "\0silent"

# --base 를 안 주면 gh 는 저장소 기본 브랜치를 쓴다. 여기서는 그 흔한 값.
DEFAULT_BASE = "main"

MAX_DIFF_BYTES = 40_000

# 히어독 본문 `<<'TAG' … TAG`. **명령이 아니라 데이터**다.
# 커밋 메시지에 "gh pr create 를 가로챈다" 라고 적으면 그 글자가 명령 문자열에
# 그대로 들어오고, shlex 는 히어독을 모르므로 gh·pr·create 를 각각 토큰으로
# 쪼갠다 — 무관한 커밋마다 PR 검증이 돈다. 이 훅이 자기 커밋에서 오발동해서
# 발견했다. 매칭은 본문을 지운 문자열로 하고, 본문 추출은 원본에서 한다.
HEREDOC_BODY = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\s*\n.*?\n\1(?=\s|$)",
                          re.DOTALL)


# 지운 자리에 남기는 표식. **평범한 단어를 쓰면 안 된다** — 본문에 그 단어가
# 리터럴로 들어 있으면 히어독이 없는데도 있다고 판단해 본문이 빈 문자열이 되고,
# "본문이 비어 있다" 로 검증이 조용히 건너뛰어진다. 하필 이 훅을 설명하는 PR 이
# 그런 본문을 갖는다. NUL 은 셸 명령 문자열에 들어올 수 없다. (리뷰 에이전트 지적)
STRIPPED = "\0heredoc\0"


def strip_heredocs(cmd: str) -> str:
    return HEREDOC_BODY.sub("<<" + STRIPPED, cmd)


# 셸 명령 구분자. 이 뒤는 **다른 명령**이라 이 명령의 플래그로 세면 안 된다
# (→ review-on-commit.py 의 _cut_at_separator 도 같은 이유로 있다).
SEPARATORS = (";", "&&", "||", "|", "&")
AGENT_TIMEOUT_S = 480

PROMPT = """\
아래는 곧 열릴 PR 의 제목·본문과, 그 브랜치가 base 에 대해 만드는 diff 다.

**너는 이 저장소를 처음 보는 사람이다.** 작성자와 나눈 대화도, 이 작업의 배경도
모른다. 그 상태에서 이 PR 본문을 읽고 "무엇이 왜 바뀌는가" 를 알 수 있는지 봐라.

리뷰가 아니다. 코드 품질·설계는 보지 마라 (그건 커밋 훅이 이미 했다).
**설명이 자립하는가**만 봐라.

다음 두 가지만 `blocking` 이다. 확인 가능한 것들이다:

1. **해석 불가능한 참조** — 본문이 가리키는 대상을 읽는 사람이 찾아갈 수 없다.
   예: 문서 이름 없는 "§9", 이름 없는 "다른 브랜치", 무엇인지 안 밝힌 "그 키",
   맥락 없는 "이번에 지웠다"/"아까 정한 대로". 저장소 안의 파일 경로를 명시한
   참조는 찾아갈 수 있으므로 해당 없다.
2. **본문과 diff 의 불일치** — 본문이 말한 변경이 diff 에 없거나, diff 의 큰
   변경이 본문에 없다. 필요하면 Read/Grep 으로 확인해라. 추측으로 지적하지 마라.

나머지는 전부 `advisory` 다. 특히 아래는 **절대 blocking 이 아니다**:
- 문장이 길다 / 구조를 바꿔라 / 더 친절하게 써라
- 한국어 표현·말투·용어 선택
- "테스트를 더 짜라", "스크린샷을 넣어라" 같은 일반 권고

용어가 이 저장소 고유(pano, frontier, explore, 판정 등)라도, 본문이나 diff 나
저장소 파일에서 뜻을 찾을 수 있으면 blocking 이 아니다. 정말로 찾을 데가
없는 것만 지적해라.

마지막 줄에 **JSON 만** 출력해라. 다른 텍스트를 뒤에 붙이지 마라:

{"findings": [{"severity": "blocking|advisory", "what": "본문의 어느 대목인가",
"summary": "한 문장", "why": "처음 보는 사람이 왜 못 알아보는가"}]}

지적할 게 없으면 {"findings": []} 를 출력해라.

--- PR 제목 ---
%s

--- PR 본문 ---
%s

--- diff (base...HEAD) ---
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
    env = dict(env or os.environ, NO_COLOR="1", FORCE_COLOR="0")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


# `--body "$(cat <<'EOF' ... EOF )"` — 본문을 여러 줄로 쓰는 가장 흔한 형태다.
# shlex 는 명령치환을 풀지 않으므로 리터럴 문자열이 그대로 남는다. 실행하지 않고
# (임의 실행은 훅이 할 일이 아니다) 히어독 표식 사이만 꺼낸다.
HEREDOC = re.compile(r"""\$\(\s*cat\s*<<-?\s*['"]?(\w+)['"]?\s*\n(.*?)\n\1\s*\)""",
                     re.DOTALL)


def heredoc_from(cmd: str) -> str:
    """원본 명령의 첫 히어독 본문. `--body "$(cat <<'EOF' … EOF)"` 용."""
    m = HEREDOC_BODY.search(cmd)
    if not m:
        return ""
    inner = m.group(0)
    return inner.split("\n", 1)[1].rsplit("\n", 1)[0]


def unwrap(value: str) -> str:
    m = HEREDOC.search(value)
    return m.group(2) if m else value


# gh 의 전역 옵션 중 **값을 따로 받는** 것. 뒤 토큰이 인자지 부명령이 아니다.
GLOBAL_WITH_VALUE = ("--repo", "-R")


def find_create(tokens: list[str]) -> int | None:
    """`gh [전역옵션…] pr create` 의 create **다음** 인덱스. 없으면 None.

    gh 와 pr 사이에는 전역 옵션이 낀다 (`gh --repo o/r pr create`). 정규식은
    그 형태를 허용하는데 토큰 쪽에서 인접만 보면 놓치고, 놓치면 SILENT 로
    빠져 **검증이 아무 말 없이 통째로 스킵된다.** (리뷰 에이전트 지적)
    """
    for k, t in enumerate(tokens):
        if not t.endswith("gh"):
            continue
        j = k + 1
        while j < len(tokens) and tokens[j].startswith("-"):
            j += 2 if tokens[j] in GLOBAL_WITH_VALUE else 1
        if tokens[j:j + 2] == ["pr", "create"]:
            return j + 2
    return None


def parse_pr(cmd: str, root: Path) -> tuple[str, str, str, str | None]:
    """(제목, 본문, base, 건너뛸사유). 파싱 실패는 건너뛴다 — 훅이 gh 보다 엄격하면 안 된다.

    base 도 **여기서** 뽑는다. 한때 별도 정규식(`base_ref`)이 원본 문자열을 훑었는데,
    그것만 토큰화도 구분자 컷도 못 받아서 본문에 적힌 "--base X" 라는 **글자**나
    `&& git checkout --base X` 의 뒤쪽 값을 집어왔다. 그러면 없는 브랜치를 base 로
    잡아 diff 가 비고, no_diff 폴백에 빠져 본문-diff 대조가 조용히 사라진다.
    같은 것을 두 경로로 읽으면 한쪽만 보호가 빠진다. (리뷰 에이전트 지적)
    """
    try:
        # 토큰화도 히어독을 지운 것으로 한다 (→ strip_heredocs). 본문 값은
        # unwrap()/heredoc_from() 이 원본에서 꺼내므로 여기서 지워도 잃는 것이 없다.
        #
        # punctuation_chars 로 `&& || ; |` 를 **따로 떼어낸다.** 평범한 split 은
        # 공백이 없으면 `본문&&gh` 를 한 토큰으로 묶어서, 뒤 명령의 플래그를
        # 이 명령 것으로 세게 된다. 따옴표 안의 구두점은 그대로 보존되므로
        # 본문에 "&&" 가 들어 있어도 쪼개지지 않는다. (리뷰 에이전트 지적)
        lex = shlex.shlex(strip_heredocs(cmd), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return "", "", DEFAULT_BASE, "명령을 해석하지 못했다"

    after = find_create(tokens)
    if after is None:
        # 정규식은 걸렸는데 토큰으로는 아니다 = 따옴표 안의 문자열이었다
        # (`git commit -m "... gh pr create ..."`). 무관한 명령이므로 조용히 통과한다.
        return "", "", DEFAULT_BASE, SILENT
    args = tokens[after:]
    # 구분자에서 끊는다. 끝까지 훑으면 뒤에 이어붙은 다른 명령의 플래그를
    # 이 명령의 것으로 센다 — `… && gh pr view --web` 에서 뒤쪽 --web 을 보고
    # 이미 읽은 본문을 버린 채 "브라우저에서 쓴다" 며 건너뛴다. (리뷰 에이전트 지적)
    for k, t in enumerate(args):
        if t in SEPARATORS:
            args = args[:k]
            break

    title = body = ""
    base = DEFAULT_BASE
    fill = False
    for j, t in enumerate(args):
        nxt = args[j + 1] if j + 1 < len(args) else ""
        if t in ("--web", "-w"):
            return "", "", DEFAULT_BASE, "--web 은 본문을 브라우저에서 쓴다"
        if t in ("--fill", "--fill-first", "--fill-verbose"):
            fill = True
        elif t in ("--base", "-B"):
            base = nxt
        elif t.startswith("--base="):
            base = t.split("=", 1)[1]
        elif t in ("--title", "-t"):
            title = nxt
        elif t.startswith("--title="):
            title = t.split("=", 1)[1]
        elif t in ("--body", "-b"):
            body = heredoc_from(cmd) if STRIPPED in nxt else unwrap(nxt)
        elif t.startswith("--body="):
            v = t.split("=", 1)[1]
            body = heredoc_from(cmd) if STRIPPED in v else unwrap(v)
        elif t in ("--body-file", "-F"):
            try:
                body = (root / nxt).read_text(encoding="utf-8") if not \
                    Path(nxt).is_absolute() else Path(nxt).read_text(encoding="utf-8")
            except OSError as e:
                return "", "", DEFAULT_BASE, f"--body-file 을 읽지 못했다: {e}"

    if fill and not body:
        # gh 가 커밋 메시지로 본문을 채운다. 그럼 커밋 메시지가 곧 검증 대상이다.
        body = commits_since(root, base)
        if not body.strip():
            return "", "", DEFAULT_BASE, "--fill 인데 커밋 메시지를 읽지 못했다"
    if not body.strip():
        return "", "", DEFAULT_BASE, "본문이 비어 있다 (gh 가 에디터를 열 것이다)"
    return title, body, base, None


def resolve_base(root: Path, base: str) -> str | None:
    """실제로 있는 ref 이름. 원격 쪽을 먼저 본다. 없으면 None.

    base 해석을 **여기 하나로** 모은다. diff 와 --fill 이 각자 ref 를 고르면
    한쪽만 틀리는 상태가 생기는데, 이 파일은 이미 그걸로 두 번 데였다.
    """
    for ref in (f"origin/{base}", base):
        r = run(["git", "rev-parse", "--verify", "--quiet", ref], root)
        if r.returncode == 0 and r.stdout.strip():
            return ref
    return None


def branch_diff(root: Path, base: str) -> str:
    ref = resolve_base(root, base)
    if ref is None:
        return ""
    r = run(["git", "diff", f"{ref}...HEAD"], root)
    return r.stdout if r.returncode == 0 else ""


def commits_since(root: Path, base: str) -> str:
    """base 이후 커밋 메시지. `--fill` 이 본문으로 쓰는 것과 같은 범위.

    한때 `@{u}..HEAD` 를 썼는데, **push 한 뒤에는 @{u} 가 HEAD 라 범위가 늘
    비었다.** 본문이 실제로는 있는데도 "커밋 메시지를 읽지 못했다" 로 검증이
    스킵돼, --fill 을 쓰는 표준 흐름에서 훅이 사실상 안 돌았다. (리뷰 에이전트 지적)
    """
    ref = resolve_base(root, base)
    if ref is None:
        return ""
    r = run(["git", "log", "--reverse", "--pretty=%s%n%n%b", f"{ref}..HEAD"], root)
    return r.stdout if r.returncode == 0 else ""


def verify(root: Path, title: str, body: str, diff: str) -> tuple[list[dict], str | None]:
    claude = shutil.which("claude")
    if not claude:
        return [], "claude CLI 를 찾지 못했다"
    env = dict(os.environ, **{GUARD: "1"})
    try:
        r = run([claude, "-p", PROMPT % (title or "(제목 없음)", body, diff),
                 "--permission-mode", "plan",
                 "--allowedTools", "Read", "Grep", "Glob",
                 "--model", "sonnet"],
                root, timeout=AGENT_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return [], f"검증 에이전트가 {AGENT_TIMEOUT_S}초 안에 안 끝났다"
    if r.returncode != 0:
        return [], f"검증 에이전트 실패 (exit {r.returncode})"
    return parse_findings(r.stdout)


def parse_findings(text: str) -> tuple[list[dict], str | None]:
    """JSON 이 실제로 끝나는 지점까지만 읽는다 (→ review-on-commit.py 의 같은 함수).

    정규식으로 끝을 추측하면 greedy 하게 뒤쪽 중괄호까지 삼켜서, 지적이 있는데도
    파싱 실패로 조용히 통과한다 — 이 훅에서 가장 나쁜 실패 방식이다.
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
            found = obj["findings"]
    if found is None:
        return [], "검증 결과를 해석하지 못했다"
    return found, None


def fmt(findings: list[dict]) -> str:
    return "\n".join(
        f"  [{f.get('severity', '?')}] {f.get('what', '?')}\n"
        f"    {f.get('summary', '')}\n"
        f"    → {f.get('why', '')}"
        for f in findings)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        emit()

    if data.get("tool_name") != "Bash":
        emit()
    cmd = data.get("tool_input", {}).get("command", "") or ""
    if not GH_PR_CREATE.search(strip_heredocs(cmd)):
        emit()
    if os.environ.get(GUARD):
        emit()

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    title, body, base, skip = parse_pr(cmd, root)
    if skip == SILENT:
        emit()
    if skip:
        emit(context=f"PR 본문 검증을 건너뛰었다: {skip}")

    diff = branch_diff(root, base)
    if len(diff) > MAX_DIFF_BYTES:
        diff = diff[:MAX_DIFF_BYTES] + "\n\n(diff 가 길어 잘렸다)"

    # diff 를 못 얻었는데 그냥 넘기면 "본문과 diff 의 불일치" 검사가 통째로
    # 빠진 채 통과 판정이 나간다 — 검사가 안 돈 것과 통과한 것이 구분되지
    # 않는다. 에이전트에게도 알리고, 사람에게도 알린다. (리뷰 에이전트 지적)
    no_diff = not diff.strip()
    if no_diff:
        diff = ("(diff 를 얻지 못했다 — base 가 없거나 브랜치가 비어 있다. "
                "본문과 diff 의 불일치는 판단하지 말고, 해석 불가능한 참조만 봐라.)")

    findings, err = verify(root, title, body, diff)
    if err:
        emit(context=f"PR 본문 검증은 돌지 못했다: {err}")

    blocking = [f for f in findings if f.get("severity") == "blocking"]
    if blocking:
        emit("deny",
             "PR 본문만으로는 맥락을 알 수 없다는 지적이다.\n\n" + fmt(blocking)
             + "\n\n본문을 고쳐 다시 열 것. 동의하지 않으면 그대로 다시 실행하되,"
               " 이 훅을 끄려면 .claude/settings.json 에서 뺄 것.")
    caveat = ("\n⚠️ diff 를 얻지 못해 본문과 변경의 대조는 하지 못했다."
              if no_diff else "")
    if findings:
        emit(context="PR 본문 검증 통과. 참고 의견:\n" + fmt(findings) + caveat)
    emit(context="PR 본문 검증 통과 — 처음 보는 사람도 맥락을 알 수 있다." + caveat)


if __name__ == "__main__":
    main()
