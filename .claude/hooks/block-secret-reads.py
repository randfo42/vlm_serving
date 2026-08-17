#!/usr/bin/env python3
"""PreToolUse 훅 — .env 의 내용이 대화 컨텍스트에 들어가는 것을 막는다.

비밀값은 한 번 컨텍스트에 들어가면 되돌릴 수 없다. 트랜스크립트에 남고, 요약에
섞여 들어가고, 이후 모든 요청에 딸려 간다. 그래서 "읽고 나서 조심" 이 아니라
**읽기 자체를 막는다.**

막는 것: Read / Edit / Write / Grep / Glob / Bash 가 .env 를 건드리는 경우
안 막는 것: .env.example, .env.sample, .env.template, .env.dist (값이 없는 형식 파일)

대신 값을 노출하지 않고 확인할 수단을 준다:

    python3 .claude/hooks/keycheck.py

키 이름·길이·지문·gitignore 상태를 보여주고 값은 절대 찍지 않는다.

### 이건 보안 경계가 아니다

우회하려고 들면 우회된다 (`cat app/'.'env`). 목적은 공격 방어가 아니라
**사고 방지**다 — 무심코 `cat .env` 를 실행해 키가 영구히 컨텍스트에 박히는 일을
막는 것. 진짜 보안은 키를 로테이트할 수 있게 두는 것이다.
"""
# 시스템 python3 로 돈다 (macOS 는 3.9). `str | None` 을 3.9 에서 쓰려면 필요하다 —
# 없으면 훅이 import 단계에서 죽고, 훅이 죽으면 아무것도 안 막힌다.
from __future__ import annotations

import json
import re
import sys

# .env / .env.local / myapp.env 는 잡고, .environment 나 .env.example 은 놔둔다.
# 뒤의 \b 가 ".environment" 같은 접두 오탐을 걸러낸다.
ENV_TOKEN = re.compile(r"\.env(?:\.[A-Za-z0-9_-]+)*\b")
SAFE_SUFFIXES = {"example", "sample", "template", "dist"}

# 경로 필드를 받는 도구들. 여기 .env 가 오면 의도가 명확하므로 무조건 막는다.
PATH_FIELDS = {
    "Read":         ("file_path",),
    "Edit":         ("file_path",),
    "Write":        ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep":         ("path", "glob"),
    "Glob":         ("path", "pattern"),
}

# Bash 는 다르다. 명령줄에는 파일을 **읽는** 경우와 그냥 **언급하는** 경우가 섞인다.
#   읽음:   cat app/.env          grep KEY app/.env      source app/.env
#   언급:   git commit -m "... .env 를 gitignore 에 추가 ..."
#           echo "키는 .env 에 둔다"
# 언급까지 막으면 커밋 메시지도 못 쓰고, 그러면 사람이 훅을 꺼버린다.
# 그래서 "파일 내용을 꺼낼 수 있는 명령어가 같은 구획에 있는가" 로 좁힌다.
READERS = r"""cat tac less more head tail bat nl grep egrep fgrep rg ag ack awk sed
    source eval exec xargs cp mv ln tee dd xxd od strings base64 openssl md5 shasum
    python python3 node ruby perl php jq yq sqlite3 vim vi nano emacs code open
    curl wget scp rsync ssh git docker printenv""".split()
READER_RE = re.compile(r"(?<![\w.-])(?:" + "|".join(READERS) + r")(?![\w-])")

# `> .env` / `>> .env` — 내용을 꺼내진 않지만 키를 날려버린다. 이것도 막는다.
REDIRECT_RE = re.compile(r">>?\s*\S*\.env\b")

# 셸 구획 분리자. 줄바꿈이 중요하다 — 힙독(heredoc) 본문의 .env 언급이
# 앞줄의 `cat <<EOF` 와 같은 구획으로 묶이면 안 된다.
SEGMENT_RE = re.compile(r"[;\n]|\|\||&&|\||&")


def offending(text: str) -> str | None:
    """비밀 .env 를 가리키는 토큰을 찾으면 그 토큰을, 없으면 None."""
    for m in ENV_TOKEN.finditer(text):
        token = m.group(0)
        parts = token.split(".")
        if len(parts) > 2 and parts[-1].lower() in SAFE_SUFFIXES:
            continue          # .env.example 류는 통과
        return token
    return None


def bash_offending(command: str) -> str | None:
    """읽거나 덮어쓰는 구획에서만 잡는다. 단순 언급은 통과시킨다."""
    for segment in SEGMENT_RE.split(command):
        token = offending(segment)
        if not token:
            continue
        if READER_RE.search(segment) or REDIRECT_RE.search(segment):
            return token
    return None


def deny(tool: str, token: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"{tool} 이 `{token}` 을 읽거나 덮어쓰려 했다. 비밀값이므로 막는다 — "
            f"한 번 컨텍스트에 들어가면 트랜스크립트와 이후 모든 요청에 남고 되돌릴 수 없다.\n"
            f"값 없이 확인하려면: python3 .claude/hooks/keycheck.py\n"
            f"코드에서 쓰려면 trailwalk.config 의 load_env() / require() — "
            f"값이 프로세스 안에만 머문다.\n"
            f"(형식 파일 .env.example 과 단순 언급은 막지 않는다)"),
    }}))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0              # 훅이 입력을 못 읽었다고 작업을 막지는 않는다

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            token = bash_offending(command)
            if token:
                deny(tool, token)
        return 0

    for field in PATH_FIELDS.get(tool, ()):
        value = tool_input.get(field)
        if isinstance(value, str) and (token := offending(value)):
            deny(tool, token)
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
