#!/bin/bash
# block-secret-reads.py 회귀 테스트.
#
#   bash .claude/hooks/test-block-secret-reads.sh
#
# 훅의 판정은 정규식 몇 개에 달려 있고, 실패 방향이 둘 다 나쁘다 —
# 못 막으면 키가 새고, 과하게 막으면 사람이 훅을 꺼버린다. 그래서 양쪽을 다 잰다.
#
# 페이로드를 파일 안에서 조립하는 이유: 테스트 명령줄에 비밀 파일명이 그대로
# 들어가면 이 테스트를 실행하는 것 자체가 훅에 막힌다.
H="${1:-$(cd "$(dirname "$0")" && pwd)/block-secret-reads.py}"
D=$'\x2eenv'          # 리터럴을 피해 조립한다

pass=0; fail=0
t() { # t <기대: block|allow> <설명> <json>
  local want="$1" desc="$2" json="$3" got
  if [ -z "$(printf '%s' "$json" | /usr/bin/python3 "$H")" ]; then got=allow; else got=block; fi
  if [ "$got" = "$want" ]; then printf '  ok   %-8s %s\n' "$got" "$desc"; pass=$((pass+1))
  else printf '  FAIL want=%s got=%s  %s\n' "$want" "$got" "$desc"; fail=$((fail+1)); fi
}

echo "── 경로 필드 도구 (엄격) ──"
t block "Read app/$D"          "{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"app/$D\"}}"
t block "Read $D.local"        "{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$D.local\"}}"
t block "Write app/$D"         "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"app/$D\"}}"
t block "Edit app/$D"          "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"app/$D\"}}"
t block "Grep path=app/$D"     "{\"tool_name\":\"Grep\",\"tool_input\":{\"pattern\":\"K\",\"path\":\"app/$D\"}}"
t block "Glob **/$D"           "{\"tool_name\":\"Glob\",\"tool_input\":{\"pattern\":\"**/$D\"}}"
t allow "Read $D.example"      "{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"app/$D.example\"}}"
t allow "Read .environment"    "{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"docs/.environment\"}}"
t allow "Read 일반 파일"        "{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"app/trailwalk/vlm.py\"}}"

echo "── Bash: 내용을 꺼내는 것 (차단) ──"
t block "cat"                  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cat app/$D\"}}"
t block "grep"                 "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"grep KAKAO app/$D\"}}"
t block "source && curl"       "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"source app/$D && curl -s x\"}}"
t block "python 으로 열기"      "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"python3 -c \\\"print(open('app/$D').read())\\\"\"}}"
t block "cp 로 복사"            "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cp app/$D /tmp/x\"}}"
t block "git add"              "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git add app/$D\"}}"
t block "파이프 뒤 구획"         "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo hi | cat app/$D\"}}"
t block "덮어쓰기 리다이렉트"     "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > app/$D\"}}"

echo "── Bash: 언급일 뿐인 것 (허용) ──"
t allow "커밋 메시지 힙독"       "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"\$(cat <<'EOF'\n비밀값 정리\n\n- .gitignore: $D 차단, $D.example 만 예외\n- 훅이 $D 읽기를 막는다\nEOF\n)\\\"\"}}"
t allow "echo 로 설명"          "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo '키는 app/$D 에 둔다'\"}}"
t allow "keycheck 헬퍼"         "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"python3 .claude/hooks/keycheck.py\"}}"
t allow "git status"           "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git status --short\"}}"
t allow "environment 단어"      "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo my environment is ready\"}}"
t allow "categories(cat 오탐)"  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo categories for $D\"}}"
t allow "$D.example 읽기"       "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cat app/$D.example\"}}"

echo
echo "통과 $pass · 실패 $fail"
[ "$fail" -eq 0 ]
