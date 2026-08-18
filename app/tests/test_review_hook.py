"""커밋 전 리뷰 훅의 판정 로직.

### 왜 훅에도 테스트가 필요한가

훅이 틀리는 방향은 둘인데 **위험도가 다르다.**

  과잉 차단 → 즉시 눈에 띈다. 사람이 짜증내고, 결국 훅을 꺼버린다
  과잉 통과 → 아무 일도 안 일어난다. 검사가 안 돌고 있다는 걸 아무도 모른다

이 레포는 둘 다 겪었다. `block-secret-reads.py` 는 `.env` 를 **언급**만 해도
막아서 커밋 메시지를 못 쓰게 했고(과잉 차단), `review-on-commit.py` 는 커밋
메시지 본문에 `--no-verify` 라는 문자열이 들어 있다는 이유로 리뷰를 통째로
건너뛰었다(과잉 통과). **원인은 같다 — 명령줄을 파싱하지 않고 문자열로 훑었다.**
"""
import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "review-on-commit.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("review_on_commit", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── git commit 인식 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "git commit -m 'hi'",
    "git commit",
    "git -C /some/path commit -m x",       # ⚠️ 값을 받는 전역 옵션 — 한 번 놓쳤다
    "git --git-dir=/x/.git commit -m y",
    "git -c user.name=x commit -m y",
    "git -C /repo \\\n    commit -m x",    # ⚠️ git 과 commit 사이의 역슬래시 줄바꿈
    "git commit --amend",
    "cd /tmp && git commit -m x",
])
def test_커밋_명령을_알아본다(hook, cmd):
    assert hook.GIT_COMMIT.search(cmd)


@pytest.mark.parametrize("cmd", [
    "git status",
    "git log --oneline -3",
    "git add -A",
    "echo 'git commit 하는 법'",          # 언급일 뿐이지만 여기선 통과해도 무해하다
    "ls -la",
    "gitcommit",
    "git commitment",
    "git log --grep=commit",               # 헐겁게 잡으면 여기 걸린다
])
def test_커밋이_아닌_것에는_안_걸린다(hook, cmd):
    if "echo" in cmd:
        pytest.skip("언급은 잡혀도 무해하다 — diff 가 비면 어차피 통과한다")
    assert not hook.GIT_COMMIT.search(cmd)


# ── --no-verify 판정 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "git commit --no-verify -m x",
    "git commit -m x --no-verify",
    "git commit -n -m x",
    "git commit -nm x",                    # 묶인 짧은 플래그
])
def test_플래그로_주면_건너뛴다(hook, cmd):
    assert hook.skips_verify(cmd) is True


def test_커밋_메시지_본문의_언급에는_속지_않는다(hook):
    """⚠️ 실전에서 뚫린 지점.

    이 훅을 설명하는 커밋 메시지가 정확히 이랬다. heredoc 을 쓰면 메시지
    본문이 command 문자열 안에 통째로 들어오고, 거기 `--no-verify` 라는
    말이 있으면 리뷰가 조용히 건너뛰어졌다.
    """
    cmd = ("git commit -q -F - <<'MSG'\n"
           "훅 추가\n\n"
           "--no-verify 는 언제나 존중한다. 탈출구 없는 게이트는 게이트가 아니다.\n"
           "MSG")
    assert hook.skips_verify(cmd) is False


def test_따옴표_안의_언급에도_안_속는다(hook):
    assert hook.skips_verify('git commit -m "--no-verify 를 지원한다"') is False


def test_다음_줄의_다른_명령에는_안_속는다(hook):
    cmd = "git commit -m x\necho --no-verify"
    assert hook.skips_verify(cmd) is False


def test_역슬래시로_이어진_줄의_플래그는_읽는다(hook):
    """⚠️ '그 줄만 본다' 로 바꾸면서 새로 생긴 회귀. 리뷰 에이전트가 잡았다.

    긴 git 명령은 역슬래시로 줄을 잇는 게 흔하다. 첫 줄만 자르면 뒤에 붙은
    플래그를 통째로 놓치고, 그 방향의 실패는 **조용한 통과**다.
    """
    assert hook.skips_verify('git commit -m "메시지" \\\n    --no-verify') is True
    assert hook.skips_verify('git commit \\\n    -a \\\n    -n -m x') is True


@pytest.mark.parametrize("sep", ["||", "&&", ";", "|", "&"])
@pytest.mark.parametrize("pad", [" ", ""])
def test_공백_없는_구분자에도_안_섞인다(hook, sep, pad):
    """⚠️ 토큰으로 나눈 뒤 자르면 `x||git` 이 한 토큰으로 합쳐져 구분자를 놓친다.
    그래서 문자열 단계에서 따옴표를 보며 자른다. 리뷰 에이전트가 잡았다."""
    cmd = f"git commit -m x{pad}{sep}{pad}git commit --no-verify -m x"
    assert hook.skips_verify(cmd) is False


@pytest.mark.parametrize("sep", ["||", "&&", ";", "|"])
def test_뒤에_붙은_다른_명령의_플래그가_안_섞인다(hook, sep):
    """⚠️ 실패 시 재시도하는 흔한 패턴에서 앞 커밋까지 스킵됐다.

        git commit -m x || git commit --no-verify -m x

    뒤 명령의 --no-verify 가 앞 명령 판정에 섞이면, 리뷰를 받아야 할 첫
    커밋이 조용히 통과한다. 리뷰 에이전트가 잡았다.
    """
    assert hook.skips_verify(f"git commit -m x {sep} git commit --no-verify -m x") is False


def test_따옴표_안의_구분자에는_안_끊긴다(hook):
    assert hook.skips_verify('git commit -m "a || b" --no-verify') is True


def test_역슬래시_뒤에_공백이_있으면_안_이어진다(hook):
    """bash 는 역슬래시가 **개행 바로 앞**일 때만 줄을 잇는다.

    `\\` 뒤에 공백이 있으면 이스케이프된 공백이지 줄 연결이 아니다. 그런데도
    다음 줄을 끌어오면 실제로는 git 에 전달되지 않은 플래그를 읽게 된다.
    리뷰 에이전트가 잡았다.
    """
    assert hook.skips_verify("git commit -m x \\ \n--no-verify") is False


def test_역슬래시가_없으면_다음_줄을_안_읽는다(hook):
    """이게 깨지면 heredoc 본문이 다시 플래그로 읽힌다."""
    cmd = "git commit -F - <<'MSG'\n--no-verify 를 설명하는 본문\nMSG"
    assert hook.skips_verify(cmd) is False


@pytest.mark.parametrize("cmd", [
    "git commit -m x",
    "git commit -am 'fix'",                # -a 는 있지만 -n 은 없다
    "git commit --amend --no-edit",        # --no-edit 는 --no-verify 가 아니다
    "git commit -uno -m x",                # ⚠️ --untracked-files=no. 'n' 은 값이다
    "git commit -unormal -m x",
])
def test_평범한_커밋은_검사를_받는다(hook, cmd):
    assert hook.skips_verify(cmd) is False


# ── -a / --all → 무엇을 리뷰할 것인가 ───────────────────────────────────────

@pytest.mark.parametrize(("cmd", "want_all"), [
    ("git commit -m x", False),
    ("git commit -a -m x", True),
    ("git commit -am x", True),            # 묶음
    ("git commit --all -m x", True),       # ⚠️ 롱 플래그를 놓쳐 리뷰가 통째로 스킵됐다
    ("git commit -qam x", True),           # 앞에 값 없는 플래그가 붙어도
    ("git commit -uno -m x", False),       # -u 의 값에 든 a 가 아니다
    ("git commit -Sa -m x", False),        # -S<keyid>. 'a' 는 키 이름이지 --all 이 아니다
    ("git commit -S -a -m x", True),       # 따로 쓰면 진짜 -a 다
    ("git commit -m x \\\n    --all", True),   # 역슬래시로 이어진 줄
])
def test_all_플래그를_정확히_읽는다(hook, cmd, want_all):
    tokens = hook.commit_flags(cmd)
    got = "--all" in tokens or hook._has_short(tokens, "a")
    assert got is want_all


def test_커밋_메시지_본문의_all_언급에는_안_속는다(hook):
    cmd = "git commit -F - <<'MSG'\n-a 와 --all 을 지원한다\nMSG"
    tokens = hook.commit_flags(cmd)
    assert not ("--all" in tokens or hook._has_short(tokens, "a"))


# ── add && commit 을 한 명령에 묶은 경우 ────────────────────────────────────
# ⚠️ 실전에서 뚫린 지점 (2026-08-18). 훅은 PreToolUse 에 돌므로 `git add X &&
# git commit` 에서는 스테이지가 아직 비어 있다. --cached 만 보면 "커밋할 게
# 없다" 로 오인해 ruff·pytest 게이트와 리뷰가 전부 조용히 건너뛰어진다 —
# 커밋 4개가 실제로 이 경로로 리뷰 없이 통과했다.

@pytest.mark.parametrize("cmd", [
    "git add f.py && git commit -m x",
    "git add -A; git commit -m x",
    "git rm old.py && git commit -m x",
    "git mv a b && git commit -m x",
    "git add f\ngit commit -m x",
    "git -C /repo add f && git -C /repo commit -m x",
])
def test_스테이징과_커밋을_한_명령에_묶으면_감지한다(hook, cmd):
    assert hook.stages_before_commit(cmd) is True


@pytest.mark.parametrize("cmd", [
    "git commit -m x",                     # 스테이징 없음 — 빈 스테이지면 여전히 조용히 통과
    "git commit -m x && git add f",        # 커밋 뒤의 add 는 이 커밋과 무관하다
    "git commit -F - <<'MSG'\ngit add 를 먼저 할 것\nMSG",   # 본문 언급은 commit 뒤라 안 속는다
    "git status && git commit -m x",
])
def test_스테이징이_없으면_기존_판정_그대로다(hook, cmd):
    assert hook.stages_before_commit(cmd) is False


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_미추적_새_파일이_best_effort_diff에_들어간다(hook, tmp_path):
    """새 파일 커밋이 흔한 경우인데 `git diff HEAD` 에는 안 보인다.
    빠지면 CLAUDE.md 만 추가하는 커밋이 도로 빈 diff 로 오인된다."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "a.py").write_text("x = 2\n")          # 추적 파일 수정 (미스테이지)
    (tmp_path / "new.py").write_text("y = 3\n")        # 미추적 새 파일
    diff = hook.worktree_diff(tmp_path)
    assert "new.py" in diff and "y = 3" in diff, "미추적 파일이 빠졌다"
    assert "x = 2" in diff, "추적 파일의 미스테이지 변경이 빠졌다"


def test_gitignore된_파일은_best_effort_diff에_안_들어간다(hook, tmp_path):
    """무시 파일까지 섞으면 .env 같은 비밀값이 리뷰 프롬프트로 새어 나간다."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("secret.txt\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "secret.txt").write_text("HUSH-VALUE\n")
    assert "HUSH-VALUE" not in hook.worktree_diff(tmp_path)


# ── 에이전트 응답에서 findings 뽑기 ─────────────────────────────────────────
# ⚠️ 이 경로는 실패하면 fail-open 이다. 못 읽으면 blocking 지적이 있어도
# 커밋이 통과한다 — 리뷰가 돌았는데 결과만 버려지는, 가장 나쁜 실패다.

def test_JSON만_있으면_읽는다(hook):
    got, err = hook.parse_findings('{"findings": []}')
    assert err is None and got == []


def test_앞뒤에_설명이_붙어도_읽는다(hook):
    got, err = hook.parse_findings(
        '리뷰했습니다.\n{"findings": [{"severity": "blocking"}]}\n끝.')
    assert err is None and len(got) == 1


def test_뒤에_중괄호_섞인_설명이_붙어도_읽는다(hook):
    """⚠️ greedy 정규식이 마지막 `}` 까지 삼켜 파싱에 실패하던 지점."""
    got, err = hook.parse_findings(
        '{"findings": [{"severity": "blocking", "file": "a.py"}]}\n'
        '참고: {이런 중괄호} 가 뒤에 있어도 결과를 놓치면 안 된다 {또 하나}')
    assert err is None, "설명 때문에 blocking 지적이 통째로 버려졌다"
    assert got[0]["severity"] == "blocking"


def test_JSON이_없으면_솔직히_실패한다(hook):
    got, err = hook.parse_findings("리뷰를 못 했습니다")
    assert got == [] and err


def test_findings가_아닌_JSON은_무시한다(hook):
    got, err = hook.parse_findings('{"result": "ok"}')
    assert err and got == []


def test_커밋이_아니면_판정하지_않는다(hook):
    assert hook.skips_verify("echo --no-verify") is False


# ── 에이전트 응답 파싱 ──────────────────────────────────────────────────────
# 에이전트는 비결정적이라 앞뒤에 설명을 붙이는 경우가 있다. 그래도 결과를
# 읽어낼 수 있어야 한다 — 못 읽으면 리뷰가 안 돈 것과 같다.

def test_지적_형식이_사람이_읽을_수_있다(hook):
    out = hook.fmt([{"severity": "blocking", "file": "a/b.py", "line": 12,
                     "summary": "예외를 삼킨다", "why": "500 이 와도 False 가 된다"}])
    assert "blocking" in out and "a/b.py:12" in out and "500" in out


def test_line이_없어도_형식이_깨지지_않는다(hook):
    out = hook.fmt([{"severity": "advisory", "file": "x.py", "summary": "s", "why": "w"}])
    assert "x.py" in out and ":None" not in out


# ── 훅 응답 형식 ────────────────────────────────────────────────────────────

def test_차단_응답이_올바른_형식이다(hook, capsys):
    with pytest.raises(SystemExit):
        hook.emit("deny", "이유")
    import json
    got = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert got["hookEventName"] == "PreToolUse"
    assert got["permissionDecision"] == "deny"
    assert got["permissionDecisionReason"] == "이유"


def test_통과는_아무것도_출력하지_않는다(hook, capsys):
    with pytest.raises(SystemExit):
        hook.emit()
    assert capsys.readouterr().out == ""


def test_컨텍스트만_붙이는_통과도_가능하다(hook, capsys):
    with pytest.raises(SystemExit):
        hook.emit(context="리뷰 통과")
    import json
    got = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert got["additionalContext"] == "리뷰 통과"
    assert "permissionDecision" not in got


# ── 재귀 방지 ───────────────────────────────────────────────────────────────

def test_재귀_방지_표식이_있다(hook):
    """리뷰 에이전트도 이 프로젝트 설정을 물려받는다. 표식이 없으면 리뷰 안에서
    또 리뷰가 돌 수 있다."""
    assert hook.GUARD and hook.GUARD.isupper()
