"""PR 본문 검증 훅의 명령 파싱.

에이전트 호출은 시험하지 않는다 — 느리고 비결정적이다. 여기서 보는 것은
**명령을 어떻게 읽는가**다. 본문을 못 꺼내면 검증이 조용히 안 돌고, 그게
이 훅에서 가장 나쁜 실패다 (→ `docs/12-harness.md` §4 의 과잉 통과).

`test_review_hook.py`(커밋 훅)와 같은 이유로 있고, 실제로 여기 있는 경우들은
**대부분 그 훅의 리뷰 에이전트가 이 훅에서 잡아낸 것들**이다. 하나는 이 훅이
자기 커밋에서 오발동해서 찾았다.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "review-on-pr.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("review_on_pr", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse(hook, cmd):
    """훅이 main() 에서 하는 것과 **같은 순서**로 본다.

    여기서 갈라지면 시험이 거짓말을 한다 — 정규식만 보고 통과시키면
    실제로는 파싱에서 비켜나는 경우를 못 잡는다.
    """
    if hook.GH_PR_CREATE.search(hook.strip_heredocs(cmd)) is None:
        return None
    return hook.parse_pr(cmd, ROOT)


# ── 본문을 꺼낸다 ───────────────────────────────────────────────────────────

def test_히어독_본문을_꺼낸다(hook):
    # 여러 줄 본문의 가장 흔한 형태. 명령치환이라 shlex 가 풀지 못한다.
    t, b, _, skip = parse(hook, '''gh pr create --title "T" --body "$(cat <<'EOF'
## 무엇
본문 줄
EOF
)"''')
    assert skip is None
    assert t == "T"
    assert b == "## 무엇\n본문 줄"


@pytest.mark.parametrize("cmd", [
    'gh pr create --title "T" --body 본문',
    'gh pr create --title=T --body=본문',
    'gh pr create -t T -b 본문',
    'gh pr create --base main -t T -b 본문',
])
def test_평범한_형태들(hook, cmd):
    _, b, _, skip = parse(hook, cmd)
    assert skip is None
    assert b == "본문"


def test_body_file_상대경로는_저장소_루트_기준(hook):
    _, b, _, skip = parse(hook, 'gh pr create -t T -F CLAUDE.md')
    assert skip is None
    assert "VLM_SERVING" in b


def test_히어독이_아닌_본문은_그대로_둔다(hook):
    # unwrap 은 `$(cat <<TAG … TAG)` 안쪽만 꺼내는 함수다. 평범한 본문을
    # 건드리기 시작하면 본문이 소리 없이 달라진다.
    assert hook.unwrap("그냥 본문") == "그냥 본문"


def test_표식과_같은_말이_본문에_있어도_흔들리지_않는다(hook):
    # 히어독을 지운 자리의 표식이 평범한 단어였을 때, 본문에 그 단어가
    # 리터럴로 있으면 본문이 통째로 사라졌다. 그러면 "본문이 비어 있다" 로
    # 검증이 조용히 스킵된다 — 하필 이 훅을 설명하는 PR 이 그런 본문이다.
    _, b, _, skip = parse(
        hook, 'gh pr create -t T --body "지운 자리에 STRIPPED 표식을 남긴다"')
    assert skip is None
    assert "STRIPPED 표식을 남긴다" in b


# ── 건너뛸 때는 사유가 정확해야 한다 ─────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    'gh pr create --web -t T',                  # 본문을 브라우저에서 쓴다
    'gh pr create --title "T"',                 # gh 가 에디터를 연다
    'gh pr create -t T --body-file -',          # 본문이 stdin 으로 온다
    'gh pr create -t T --body-file $S/pr.md',   # 셸이 펼칠 경로
])
def test_검사할_본문이_없으면_건너뛴다(hook, cmd):
    _, _, _, skip = parse(hook, cmd)
    assert skip and skip != hook.SILENT


def test_셸_변수_경로는_사유를_정확히_남긴다(hook):
    # "파일이 없다" 로 남기면 검증이 안 돈 진짜 이유가 가려진다. 실제로 이
    # 훅을 넣는 PR 이 그렇게 조용히 검증을 건너뛰었다.
    _, _, _, skip = parse(hook, 'gh pr create -t T --body-file $S/pr.md')
    assert "셸 변수" in skip


# ── 명령이 아닌 것을 명령으로 읽지 않는다 ────────────────────────────────────

def test_따옴표_안의_문자열은_조용히_비켜난다(hook):
    _, _, _, skip = parse(hook, 'git commit -m "gh pr create 라고 적었을 뿐"')
    assert skip == hook.SILENT


def test_히어독_안의_문자열은_정규식에서_걸러진다(hook):
    # ⚠️ 이 훅이 **자기 커밋에서 오발동해서** 찾은 경우다. 커밋 메시지에
    # 이 훅을 설명하는 문장을 적었더니 그 글자가 명령 문자열에 들어왔고,
    # shlex 는 히어독을 모르므로 gh·pr·create 를 각각 토큰으로 쪼갰다.
    # 따옴표 케이스와 달라서 SILENT 로는 부족하다 — 정규식에서 걸러야 한다.
    assert parse(hook, '''git commit -F - <<'EOF'
훅: PR 본문을 검증한다

gh pr create 를 가로채 별도 에이전트에게 넘긴다.
EOF''') is None


# ── 이 명령의 인자만 센다 ───────────────────────────────────────────────────

def test_구분자_뒤의_플래그는_남의_것이다(hook):
    # 생성하고 바로 브라우저로 여는 흔한 형태. 끝까지 훑으면 뒤쪽 --web 을
    # 보고 이미 읽은 본문을 버린 채 건너뛴다.
    _, b, _, skip = parse(hook, 'gh pr create -t T -b 본문 && gh pr view --web')
    assert skip is None
    assert b == "본문"


def test_구분자_뒤의_body가_본문을_덮어쓰지_않는다(hook):
    # 위 케이스는 뒤 명령에 경쟁하는 -b 가 없어서 이쪽을 못 본다. 끊지 않으면
    # 뒤 명령의 -b 가 이미 읽은 본문을 조용히 갈아치운다.
    _, b, _, skip = parse(hook, 'gh pr create -t T -b 진짜본문 ; other -b 가짜본문')
    assert skip is None
    assert b == "진짜본문"


def test_공백_없는_구분자도_끊는다(hook):
    # 토큰화 **뒤에** 자르면 `본문&&gh` 가 한 토큰이라 빠져나간다.
    _, b, _, skip = parse(hook, 'gh pr create -t T -b 본문&&gh pr view --web')
    assert skip is None
    assert b == "본문"


def test_따옴표_안의_구두점은_본문의_일부다(hook):
    # 위를 고치면서 여기까지 쪼개면 본문이 잘린다.
    _, b, _, skip = parse(hook, 'gh pr create -t T -b "a && b | c"')
    assert skip is None
    assert b == "a && b | c"


@pytest.mark.parametrize("cmd", [
    'gh --repo owner/repo pr create -t T -b 본문',   # 값을 받는 전역 옵션
    'gh -R owner/repo pr create -t T -b 본문',
    'gh --verbose pr create -t T -b 본문',           # 값 없는 전역 옵션
])
def test_gh_와_pr_사이의_전역_옵션(hook, cmd):
    # 정규식은 이 형태를 허용하는데 토큰 쪽에서 인접만 보면 놓친다. 놓치면
    # SILENT 로 빠져 **검증이 아무 말 없이 통째로 스킵된다.**
    _, b, _, skip = parse(hook, cmd)
    assert skip is None
    assert b == "본문"


# ── base 를 어디서 읽는가 ───────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,want", [
    ('gh pr create --base develop -t T -b 본문', "develop"),
    ('gh pr create -B release -t T -b 본문', "release"),
    ('gh pr create -t T -b 본문', "main"),
    # 아래 셋은 base 를 별도 정규식으로 원본 문자열에서 뽑던 시절에 전부 틀렸다.
    # 틀리면 없는 브랜치를 base 로 잡아 diff 가 비고, 그러면 "본문과 diff 의
    # 불일치" 검사가 조용히 빠진 채 통과 판정이 나간다.
    ('gh pr create -t T -b "--base develop 처럼 준다"', "main"),
    ('gh pr create -t T -b 본문 && git checkout --base develop', "main"),
])
def test_base_는_본문과_같은_토큰화에서_나온다(hook, cmd, want):
    _, _, base, skip = parse(hook, cmd)
    assert skip is None
    assert base == want


def test_히어독_본문_안의_base_는_글자다(hook):
    _, _, base, skip = parse(hook, '''gh pr create -t T --body "$(cat <<'EOF'
base 를 고르려면 --base develop 처럼 준다.
EOF
)"''')
    assert skip is None
    assert base == "main"


# ── ref 해석은 한 곳에서 ────────────────────────────────────────────────────

def test_없는_base_는_None_이다(hook):
    assert hook.resolve_base(ROOT, "없는브랜치이름xyz") is None


def test_있는_base_는_찾는다(hook):
    assert hook.resolve_base(ROOT, "main") is not None


@pytest.mark.parametrize("fn", ["branch_diff", "commits_since"])
def test_없는_base_면_빈_문자열이다(hook, fn):
    # main() 은 이걸 보고 캐비앗을 붙여야 한다. 빈 채로 넘어가면 본문-diff
    # 대조가 빠진 줄 모르고 "통과" 가 나간다.
    assert getattr(hook, fn)(ROOT, "없는브랜치이름xyz").strip() == ""
