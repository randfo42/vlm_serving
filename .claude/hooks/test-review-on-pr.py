#!/usr/bin/env python3
"""review-on-pr.py 의 결정적인 부분만 시험한다. 단독 실행:

    /usr/bin/python3 .claude/hooks/test-review-on-pr.py

에이전트 호출은 시험하지 않는다 — 느리고 비결정적이다. 여기서 보는 것은
**명령 파싱**이다. 훅이 본문을 못 꺼내면 검증이 조용히 안 돈다.

⚠️ 이 파일은 `gh pr create` · `git commit` 을 문자열로 담고 있다. 그래서
**Bash 히어독으로 이 파일을 쓰면 두 훅이 자기 자신에게 오발동한다.** 편집은
파일 도구로 한다. (그 오발동이 바로 아래 "히어독 안의 문자열" 케이스다.)
"""
import importlib.util
import sys
from pathlib import Path

HOOK = Path(__file__).with_name("review-on-pr.py")
spec = importlib.util.spec_from_file_location("h", HOOK)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
ROOT = Path(__file__).resolve().parents[2]

fails = []

# matches 의 세 값:
#   True     PR 생성으로 다뤄야 한다
#   "silent" 정규식은 걸려도 파싱이 조용히 비켜나야 한다
#   False    정규식조차 걸리면 안 된다 (명령이 아니라 데이터다)


def check(name, cmd, *, body=None, title=None, base=None, skip=None, matches=True):
    # main() 과 같은 경로로 본다 — 여기서 갈라지면 시험이 거짓말을 한다
    hit = h.GH_PR_CREATE.search(h.strip_heredocs(cmd)) is not None
    if matches is False:
        if hit:
            fails.append(f"{name}: 명령이 아닌데 정규식이 걸렸다")
        return
    if not hit:
        fails.append(f"{name}: 정규식이 안 걸렸다")
        return
    t, b, bs, s = h.parse_pr(cmd, ROOT)
    if matches == "silent":
        if s != h.SILENT:
            fails.append(f"{name}: 조용히 통과해야 하는데 {s!r}")
        return
    if skip is not None:
        if s is None or s == h.SILENT:
            fails.append(f"{name}: 건너뛰어야 하는데 안 건너뛰었다")
        return
    if s:
        fails.append(f"{name}: 건너뛰면 안 되는데 {s!r}")
        return
    if body is not None and body not in b:
        fails.append(f"{name}: 본문에 {body!r} 가 없다 — 얻은 것 {b[:60]!r}")
    if title is not None and t != title:
        fails.append(f"{name}: 제목이 {t!r}")
    if base is not None and bs != base:
        fails.append(f"{name}: base 가 {bs!r}, 기대 {base!r}")


# 가장 흔한 형태. 여러 줄 본문은 대개 이렇게 쓴다.
check("히어독", '''gh pr create --title "T" --body "$(cat <<'EOF'
## 무엇
본문 줄
EOF
)"''', body="## 무엇\n본문 줄", title="T")

check("리터럴", 'gh pr create --title "T" --body "한 줄"', body="한 줄", title="T")

# 히어독 표식으로 쓰는 말이 본문에 리터럴로 있어도 흔들리면 안 된다. 평범한
# 단어를 표식으로 쓰면 여기서 본문이 통째로 사라지고 "본문이 비어 있다" 로
# 검증이 조용히 건너뛰어진다 — 이 훅을 설명하는 PR 이 정확히 그런 본문이다.
check("표식과 같은 말이 본문에 있어도",
      'gh pr create -t T --body "히어독을 지운 자리에 STRIPPED 표식을 남긴다"',
      body="STRIPPED 표식을 남긴다", title="T")
check("등호", 'gh pr create --title=T --body=본문', body="본문", title="T")
check("-b 짧은 형태", 'gh pr create -t T -b 본문', body="본문", title="T")
check("--base 뒤에 와도", 'gh pr create --base main -t T -b 본문', body="본문")

# gh 와 pr 사이의 전역 옵션. 정규식은 허용하는데 토큰 쪽에서 인접만 보면
# 놓치고, 놓치면 SILENT 로 빠져 검증이 아무 말 없이 통째로 스킵된다.
check("--repo 가 사이에 껴도", 'gh --repo owner/repo pr create -t T -b 본문',
      body="본문", title="T")
check("-R 가 사이에 껴도", 'gh -R owner/repo pr create -t T -b 본문', body="본문")
check("값 없는 전역 옵션이 껴도", 'gh --verbose pr create -t T -b 본문', body="본문")
# 절대경로로 부른 형태(`/opt/homebrew/bin/gh …`)는 일부러 안 잡는다 — 정규식의
# (?<![\w./-]) 가 막는다. review-on-commit.py 의 git 패턴도 같다. 놓쳐도
# fail-open 이라 검증이 안 돌 뿐 잘못 막지는 않는다.

check("--web 은 건너뛴다", 'gh pr create --web -t T', skip=True)
check("본문 없으면 건너뛴다", 'gh pr create --title "T"', skip=True)
# stdin 은 훅이 볼 수 없다 (읽으면 gh 가 받을 것이 없어진다). 건너뛰되 사유는
# 정확해야 한다 — "파일이 없다" 로 남으면 원인을 못 찾는다.
check("--body-file - 은 stdin", 'gh pr create -t T --body-file -', skip=True)

# 저장소 안의 파일을 본문으로 주는 형태. 상대경로는 저장소 루트 기준이다.
check("--body-file 상대경로", 'gh pr create -t T -F CLAUDE.md', body="VLM_SERVING")

# 구분자 뒤는 다른 명령이다. 여기 넘어가면 뒤쪽 --web 을 보고 이미 읽은 본문을
# 버린 채 건너뛴다 — 생성하고 바로 브라우저로 여는 흔한 형태에서 검증이 통째로
# 빠진다.
check("구분자 뒤의 플래그는 남의 것", 'gh pr create -t T -b 본문 && gh pr view --web',
      body="본문", title="T")
check("구분자 뒤의 -b 가 본문을 오염시키지 않는다",
      'gh pr create -t T -b 진짜본문 ; other -b 가짜본문', body="진짜본문")
# 공백 없는 구분자. 토큰화 뒤에 자르면 `본문&&gh` 가 한 토큰이라 빠져나간다.
check("공백 없는 구분자", 'gh pr create -t T -b 본문&&gh pr view --web',
      body="본문", title="T")
# 반대로 따옴표 안의 구두점은 본문의 일부다. 여기서 쪼개면 본문이 잘린다.
check("따옴표 안의 && 는 본문이다", 'gh pr create -t T -b "a && b | c"',
      body="a && b | c", title="T")

# 따옴표 안의 문자열. 정규식은 헐거워서 걸리지만 토큰으로는 아니다.
check("따옴표 안의 문자열", 'git commit -m "gh pr create 라고 적었을 뿐"',
      matches="silent")

# 히어독 본문 안의 문자열. **이 훅이 자기 커밋에서 실제로 오발동했던 경우다** —
# 커밋 메시지에 "gh pr create 를 가로챈다" 라고 적었더니 shlex 가 히어독을
# 모르는 탓에 gh·pr·create 를 각각 토큰으로 쪼갰다. 여기는 따옴표 케이스와
# 달라서 SILENT 로도 부족하다: 정규식 단계에서 걸러야 한다.
check("히어독 안의 문자열", '''git commit -F - <<'EOF'
훅: PR 본문을 검증한다

gh pr create 를 가로채 별도 에이전트에게 넘긴다.
EOF''', matches=False)

# base 추출. 본문 값과 **같은 토큰화**에서 나와야 한다 — 별도 정규식으로 뽑던
# 시절엔 아래 셋이 전부 틀렸다. 틀리면 없는 브랜치를 base 로 잡아 diff 가 비고,
# no_diff 폴백 탓에 본문-diff 대조가 조용히 빠진다.
check("base 를 준 경우", 'gh pr create --base develop -t T -b 본문', base="develop")
check("-B 짧은 형태", 'gh pr create -B release -t T -b 본문', base="release")
check("base 를 안 준 경우", 'gh pr create -t T -b 본문', base="main")
check("본문 안의 --base 는 글자다", 'gh pr create -t T -b "--base develop 처럼 준다"',
      base="main")
check("구분자 뒤의 --base 는 남의 것",
      'gh pr create -t T -b 본문 && git checkout --base develop', base="main")
check("히어독 본문 안의 --base", '''gh pr create -t T --body "$(cat <<'EOF'
base 를 고르려면 --base develop 처럼 준다.
EOF
)"''', base="main")

# 히어독이 아닌 값은 그대로 나와야 한다 (unwrap 이 멀쩡한 본문을 먹지 않는지)
if h.unwrap("그냥 본문") != "그냥 본문":
    fails.append("unwrap 이 평범한 본문을 바꿨다")

# base 해석은 diff 와 --fill 이 **같은 함수**를 써야 한다. 각자 고르면 한쪽만
# 틀리는 상태가 생긴다 (실제로 두 번 그랬다).
if h.resolve_base(ROOT, "main") is None:
    fails.append("main 을 못 찾았다")
if h.resolve_base(ROOT, "없는브랜치이름xyz") is not None:
    fails.append("없는 브랜치를 찾았다고 한다")

# 없는 base 면 diff 가 빈 문자열이다. main() 은 이걸 보고 캐비앗을 붙여야 한다 —
# 빈 채로 넘어가면 "본문과 diff 불일치" 검사가 빠진 줄 모르고 통과 판정이 나간다.
if h.branch_diff(ROOT, "없는브랜치이름xyz").strip():
    fails.append("없는 base 인데 diff 가 나왔다")
if h.commits_since(ROOT, "없는브랜치이름xyz").strip():
    fails.append("없는 base 인데 커밋 메시지가 나왔다")

if fails:
    print("실패:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("전부 통과")
