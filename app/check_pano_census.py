#!/usr/bin/env python
"""진단 — 반경 안에 pano 가 몇 개고 어느 계열인지 전수 조사. VLM·브라우저 불필요.

    python app/check_pano_census.py --lat 37.5701282 --lng 127.0155282 --radius 500
    python app/check_pano_census.py --lat ... --lng ... -o /tmp/nodes.json

`check_*.py` 는 사람이 눈으로 보는 도구라 `--config` 외의 인자를 갖는다
(→ app/CLAUDE.md 규칙 2 의 예외).

### 왜 이게 필요한가

로드뷰 pano 는 **계열별로 끊긴 그래프**다. 차량 촬영과 도보 촬영이 서로
이어져 있지 않아서, 같은 좌표·같은 반경이라도 **시작점이 어느 계열에
스냅되느냐가 무엇을 모으는지를 정한다** (→ docs/23-open-questions.md §7).
탐색을 돌리기 전에 "이 반경 안에 뭐가 있나" 를 알아야 결과를 읽을 수 있다.

2026-08-23 GS25 청계천패션2점(청계천로 341) 반경 500m 에서 이걸 돌린 결과가
그 절에 있다 — 계열 24개, 하천 보행로는 4.8% 뿐이었고, 그 주소가 스냅되는
pano 는 청계천로가 아니라 창신동 골목의 차량 pano 였다.

### 캡처하지 않는다 — `spot[]` 이 곧 이웃 그래프다

노드 API(`v2/node/{id}`)가 이웃 목록을 주므로 BFS 를 순수 HTTP 로 돈다.
브라우저도 이미지도 없다. 2,500개 규모가 ~10분이고 디스크에 안 남는다.

### ⚠️ 실패는 개수를 조용히 깎는다 — 그래서 센다

조회가 실패한 노드에서 뻗었을 갈래는 통째로 빠지고, 결과는 "반경 안 N개"
한 줄이라 티가 안 난다. 실패·누락 건수를 세어 리포트와 JSON 에 남긴다.
**하나라도 있으면 그 N 은 하한이다** — 설정 주석이나 문서에 박기 전에
다시 돌릴 것.

### ⚠️ 결과는 **시드에 의존한다**

끊긴 그래프이므로 시드가 닿지 못하는 계열은 세어지지 않는다. 같은 좌표에서
시드 2개면 2,458개, 3개면 2,481개가 나왔다(하천 계열은 99 → 118). 이 절이
말하는 바로 그 성질이 census 에도 적용된다. **숫자를 인용할 때는 어느
시드로 돌린 것인지 같이 적을 것.**

기본 시드는 `--lat/--lng` 를 provider 로 스냅해서 얻는다(그것 하나로는
그 좌표가 속한 계열밖에 못 본다). 다른 계열을 넣으려면 `--seed` 를 더 준다.

### 산출물은 레포 밖에 둔다

`-o` 로 저장하는 JSON 은 지도 사업자에게서 받은 pano 좌표·주소이므로
**커밋하지 않는다** (이미지와 같은 회색지대 → docs/23-open-questions.md §2).
`app/runs/` 아래나 레포 밖 경로를 쓸 것.
"""
import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labels.pano_meta import fetch_node, is_walk

from trailwalk.geo import haversine_m


def census(center: tuple[float, float], radius_m: float, seeds: list[str],
           pause: float = 0.15, every: int = 250) -> dict:
    """반경 안 pano 를 BFS 로 전부. 반경 밖 노드는 **확장하지 않는다**
    (run_collect·explore 와 같은 규칙이라 개수를 그대로 비교할 수 있다)."""
    out: dict[str, dict] = {}
    # ⚠️ 실패를 **세어서 돌려준다.** 조회가 몇 건 실패하면 그만큼 그래프가
    # 덜 뻗어 개수가 준다. 그런데 결과는 "반경 안 N개" 한 줄이라, 세지 않으면
    # 언더카운트인지 그래프가 실제로 거기서 끊긴 것인지 구분할 수 없다 —
    # 그 N 이 설정 주석과 문서에 실측치로 박히므로 조용하면 안 된다.
    failed: list[str] = []      # 예외 (네트워크·타임아웃·응답 파싱)
    missing: list[str] = []     # 200 인데 노드가 없다 (시드 오타·삭제된 id)
    seen, q = set(seeds), list(seeds)
    t0 = time.time()
    while q:
        pid = q.pop(0)
        try:
            n = fetch_node(pid)
        except Exception as e:
            failed.append(pid)
            print(f"  ⚠ 조회 실패 {pid}: {type(e).__name__}", file=sys.stderr)
            continue
        if not n:
            missing.append(pid)
            print(f"  ⚠ 노드 없음: {pid}", file=sys.stderr)
            continue
        lat, lng = float(n["wgsy"]), float(n["wgsx"])
        d = haversine_m(center, (lat, lng))
        out[pid] = {"lat": lat, "lng": lng, "d": round(d, 1),
                    "tool": str(n.get("shot_tool")), "st_type": n.get("st_type"),
                    "st_name": n.get("st_name"), "addr": n.get("addr"),
                    "shot_date": n.get("shot_date")}
        # 반경 밖은 확장하지 않는다. **다만 쓰로틀은 건너뛰지 않는다** —
        # 여기서 continue 하면 경계 노드가 연속으로 나오는 구간에서 요청이
        # 쓰로틀 없이 몰려 나가고, 레이트리밋 실패가 하필 반경 경계에
        # 편향돼 생긴다. 이 스크립트가 막으려는 언더카운트를 스스로 만든다.
        if d <= radius_m:
            for sp in (n.get("spot") or []):
                sid = str(sp["id"])
                if sid not in seen:
                    seen.add(sid)
                    q.append(sid)
        if len(out) % every == 0:
            print(f"  {len(out)}개 · {time.time() - t0:.0f}s", flush=True)
        time.sleep(pause)
    return {"center": list(center), "radius_m": radius_m, "seeds": seeds,
            "fetched": len(out), "failed": failed, "missing": missing,
            "elapsed_s": round(time.time() - t0, 1), "nodes": out}


def report(c: dict) -> None:
    R = c["radius_m"]
    ins = {k: v for k, v in c["nodes"].items() if v["d"] <= R}
    if not ins:
        print("반경 안에 pano 가 없다 — 시드가 반경 밖이거나 커버리지가 없다")
        return
    # 도보 판정은 반드시 is_walk 로. 여기서 다시 구현하면 그쪽만
    # 고쳤을 때 이 스크립트가 조용히 안 따라온다 (→ pano_meta 독스트링)
    walk = sum(1 for v in ins.values() if is_walk(v["tool"]))
    print(f"\n시드 {len(c['seeds'])}개 · 조회 {c['fetched']} · "
          f"반경 {R:.0f}m 안 **{len(ins)}개** ({c['elapsed_s']:.0f}s)")
    print(f"  도보 {walk} ({100 * walk / len(ins):.1f}%) · 차량 {len(ins) - walk}")

    # 실패가 있으면 이 개수는 **하한**이다. 인용하기 전에 다시 돌릴 것
    nf, nm = len(c.get("failed", [])), len(c.get("missing", []))
    if nf or nm:
        print(f"\n⚠️  조회 실패 {nf}건 · 노드 없음 {nm}건 — **개수가 하한이다.**")
        print("    실패한 노드에서 뻗었을 갈래가 통째로 빠졌다. 그래프가 거기서")
        print("    끊긴 것인지 조회가 실패한 것인지는 이 결과로 구분할 수 없다.")
        for pid in (c.get("failed", []) + c.get("missing", []))[:5]:
            print(f"      {pid}")
        if nf + nm > 5:
            print(f"      … 외 {nf + nm - 5}건")

    fam = collections.Counter(k[:6] for k in ins)
    print(f"\n계열 {len(fam)}개 (pano id 앞 6자리):")
    for pre, n in fam.most_common():
        tool = next(v["tool"] for k, v in ins.items() if k.startswith(pre))
        kind = "도보" if is_walk(tool) else "차량"
        print(f"  {pre}…  {kind}  tool {tool:>4}  {n:>5}개  "
              f"({100 * n / len(ins):4.1f}%)")

    for field, title in (("st_type", "st_type"), ("st_name", "도로명")):
        cnt = collections.Counter(v[field] for v in ins.values())
        print(f"\n{title}:")
        for k, n in cnt.most_common(8):
            print(f"  {k!s:16s} {n:>5}개")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--radius", type=float, required=True, help="미터")
    ap.add_argument("--seed", action="append", default=[],
                    help="추가 시드 pano id. 끊긴 계열을 넣으려면 필요하다 (여러 번)")
    ap.add_argument("--snap-radius", type=float, default=150.0,
                    help="좌표에서 시드 pano 를 찾을 반경(m)")
    ap.add_argument("-o", "--out", default=None,
                    help="JSON 저장 경로. **커밋하지 말 것** (독스트링 참고)")
    a = ap.parse_args()

    seeds = list(dict.fromkeys(a.seed))
    if not seeds:
        # 시드를 안 주면 좌표를 스냅해서 하나 만든다. 브라우저가 필요한 건
        # 여기뿐이라 시드를 직접 주면 완전히 HTTP 만으로 돈다
        from trailwalk import providers, settings
        prov = providers.make("kakao", settings=settings.load())
        try:
            p = prov.nearest(a.lat, a.lng, a.snap_radius)
        finally:
            prov.close()
        if p is None:
            print(f"✗ {a.snap_radius:.0f}m 안에 로드뷰가 없다", file=sys.stderr)
            return 2
        print(f"시드(좌표 스냅): {p.pano_id}")
        seeds = [p.pano_id]

    c = census((a.lat, a.lng), a.radius, seeds)
    report(c)
    if a.out:
        Path(a.out).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        print(f"\n→ {a.out}  (커밋하지 말 것)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
