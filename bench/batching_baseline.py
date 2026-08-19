#!/usr/bin/env python
"""배칭 실험용 베이스라인 수집 — 요청별 원시 데이터를 JSON 으로 남긴다.

기존 벤치(sweep/concurrency)는 중앙값 표만 남겼다. 배칭을 직접 수정해
보려면 "고치기 전" 의 요청별 구간 분해가 필요하다. 세 가지를 잰다:

  serial  예산별 직렬 실행 + verbose 로그에서 구간 분해
          (clip_encode / 그래프 재예약 / 이미지 KV / decode)
  conc    -np 1,2,4 동시 실행의 요청별 원시 행 (04-b1 §4 는 집계만 남았다)
  multi   이미지 N장 1요청 — b10450 의 요청 내 배칭 API(mtmd_batch_encode)가
          이 모델 그래프에서 발동하는가, 발동하면 장당 인코딩이 줄어드는가

사용:
  .venv/bin/python bench/batching_baseline.py serial
  .venv/bin/python bench/batching_baseline.py conc
  .venv/bin/python bench/batching_baseline.py multi
  .venv/bin/python bench/batching_baseline.py all

⚠️ 재예약 시간은 로그의 "reserve took N ms" 를 쓰지 않는다. 그 값은
   예약 계산만 재고, 실제 공백(Metal 버퍼 작업으로 추정)은 그 앞
   "reserving ..." 줄과의 타임스탬프 차이에 있다. -ub 320 실측:
   reserve took 22ms 인데 벽시계 공백은 757ms.
"""
import base64
import datetime
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _paths import LLAMA_BIN, MMPROJ, MODEL, ROOT, build_id, check

IMAGES = sorted((ROOT / "bench" / "images").glob("gen_*.jpg"))
URL = "http://127.0.0.1:8080"
OUT = ROOT / "bench" / "results"
LOGDIR = Path("/tmp")

# sweep.py 의 SYSTEM 과 다르다 — concurrency.py 와 같은 짧은 판. 배칭
# 실험은 04-b1 §4(동시성) 수치와 비교하는 게 목적이므로 그쪽에 맞춘다.
SYSTEM = ("You are a walking-trail assessor. Judge whether the scene is a pleasant walking trail. "
          "Answer only with the requested JSON. Be decisive.")
SCHEMA = {"type": "object", "properties": {"is_trail": {"type": "boolean"}},
          "required": ["is_trail"], "additionalProperties": False}


def start_server(tag, budget, ubatch, np_slots=1, ctx=8192, verbose=False, extra=()):
    log_path = LOGDIR / f"llama_batchbase_{tag}.log"
    log = open(log_path, "w")  # noqa: SIM115 — Popen stdout, 프로세스와 수명을 같이한다
    args = [
        str(LLAMA_BIN), "--model", str(MODEL), "--mmproj", str(MMPROJ), "--jinja",
        "-ngl", "99", "--ctx-size", str(ctx), "--parallel", str(np_slots),
        "--ubatch-size", str(ubatch), "--batch-size", str(ubatch),
        "--image-min-tokens", str(budget), "--image-max-tokens", str(budget),
        "--cache-ram", "0", "--reasoning", "off", "--reasoning-budget", "0",
        *extra,
        "--host", "127.0.0.1", "--port", "8080",
    ]
    if verbose:
        args += ["-lv", "10"]
    p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(180):
        if p.poll() is not None:
            raise RuntimeError(f"server died ({tag}) — see {log_path}")
        try:
            urllib.request.urlopen(f"{URL}/health", timeout=1).read()
            return p, log_path
        except Exception:
            time.sleep(1)
    p.kill()
    raise RuntimeError(f"unhealthy ({tag})")


def ask(images, max_tokens=60):
    """이미지 1장 이상 + 고정 텍스트. 반환: (client_e2e_s, 응답 dict)."""
    content = []
    for img in images:
        b64 = base64.b64encode(img.read_bytes()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": "Assess this scene."})
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": content}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "t", "schema": SCHEMA, "strict": True}},
            "max_tokens": max_tokens, "temperature": 0}
    t0 = time.time()
    r = urllib.request.urlopen(urllib.request.Request(
        f"{URL}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=600)
    return time.time() - t0, json.load(r)


def row_from(timings, e2e_s):
    return {"e2e_ms": round(e2e_s * 1000, 1),
            "cache_n": timings["cache_n"], "prompt_n": timings["prompt_n"],
            "prompt_ms": round(timings["prompt_ms"], 1),
            "predicted_n": timings["predicted_n"],
            "predicted_ms": round(timings["predicted_ms"], 1)}


# ── verbose 로그 파서 ─────────────────────────────────────────────

TS = r"(\d+)\.(\d\d)\.(\d\d\d)\.(\d\d\d)"          # 분.초.밀리.마이크로


def _ms(m):
    return int(m[0]) * 60000 + int(m[1]) * 1000 + int(m[2]) + int(m[3]) / 1000


MARKS = {
    "batch": re.compile(TS + r" D mtmd_batch_encode_impl: encoding batch with (\d+) entries"
                             r" and total (\d+) tokens"),
    "clip0": re.compile(TS + r" D clip_encode: copying image \d+/\d+"),
    "clip1": re.compile(TS + r" D clip_encode: output embedding shape"),
    "causal": re.compile(TS + r" D set_causal_attn: value = (\d)"),
    "res0": re.compile(TS + r" I sched_reserve: reserving \.\.\."),
    "res1": re.compile(TS + r" I sched_reserve: reserve took"),
    "kv": re.compile(TS + r" I image decoded \(batch (\d+)/(\d+)\) in (\d+) ms"),
    "done": re.compile(TS + r" I slot print_timing: .*prompt eval time =\s+([\d.]+) ms"),
    "ntok": re.compile(TS + r" D image_tokens->nx = (\d+)"),
}


def parse_phases(log_path):
    """요청 단위로 구간을 분해한다. 반환: 요청별 dict 리스트 (로그 등장 순).

    한 요청의 마커 순서(이미지 1장):
      [batch] clip0 clip1 causal=0 res0..res1 kv causal=1 res0..res1 done
    재예약 벽시계 = res1.ts - res0.ts (reserve took 값이 아니라).
    """
    reqs = []
    cur = {"clip_ms": 0.0, "reserve_ms": [], "kv_ms": [], "batches": [],
           "image_tokens": []}
    clip_start = res_start = None
    for line in log_path.read_text(errors="replace").splitlines():
        for name, rx in MARKS.items():
            m = rx.match(line)
            if not m:
                continue
            g = m.groups()
            ts = _ms(g[:4])
            if name == "ntok":
                cur["image_tokens"].append(int(g[4]))
            elif name == "batch":
                cur["batches"].append({"entries": int(g[4]), "tokens": int(g[5])})
            elif name == "clip0":
                clip_start = ts
            elif name == "clip1" and clip_start is not None:
                cur["clip_ms"] += ts - clip_start
                clip_start = None
            elif name == "res0":
                res_start = ts
            elif name == "res1" and res_start is not None:
                cur["reserve_ms"].append(round(ts - res_start, 1))
                res_start = None
            elif name == "kv":
                cur["kv_ms"].append(int(g[6]))
            elif name == "done":
                cur["clip_ms"] = round(cur["clip_ms"], 1)
                cur["prompt_ms_log"] = float(g[4])
                reqs.append(cur)
                cur = {"clip_ms": 0.0, "reserve_ms": [], "kv_ms": [], "batches": [],
                       "image_tokens": []}
            break
    return reqs


def _meta(**cfg):
    return {"build": build_id(), "date": datetime.date.today().isoformat(),
            "host": "M4-10gpu-16GB", "system_prompt": SYSTEM,
            "schema": "tiny", **cfg}


# ── phase: serial ────────────────────────────────────────────────

# (예산, ubatch). 280/320 이 운영 설정, 280/2048 은 sweep 과 비교용,
# 70·1120 은 prefill 강도 축의 양끝.
SERIAL_CONFIGS = [(70, 2048), (280, 320), (280, 2048), (1120, 2048)]


def phase_serial():
    runs = []
    for budget, ub in SERIAL_CONFIGS:
        tag = f"serial_{budget}_{ub}"
        srv, log_path = start_server(tag, budget, ub, verbose=True)
        try:
            rows = []
            for i, img in enumerate(IMAGES):        # [0] 은 워밍업, 결과에 표시만
                e2e, d = ask([img])
                r = row_from(d["timings"], e2e)
                r["image"] = img.name
                r["warmup"] = (i == 0)
                rows.append(r)
        finally:
            srv.terminate()
            srv.wait(timeout=30)
            time.sleep(2)
        phases = parse_phases(log_path)
        if len(phases) == len(rows):
            for r, ph in zip(rows, phases, strict=True):
                # 정렬 검증: API 와 로그의 prompt_ms 가 같은 요청인가
                if abs(ph["prompt_ms_log"] - r["prompt_ms"]) > 2.0:
                    print(f"WARN {tag}: prompt_ms 불일치 "
                          f"api={r['prompt_ms']} log={ph['prompt_ms_log']}")
                r.update({k: ph[k] for k in
                          ("clip_ms", "reserve_ms", "kv_ms", "image_tokens", "batches")})
        else:
            print(f"WARN {tag}: 로그 요청 수 {len(phases)} != API 요청 수 {len(rows)}")
        runs.append({"budget": budget, "ubatch": ub, "rows": rows})
        good = [r for r in rows if not r["warmup"]]
        med = statistics.median
        print(f"{tag:>18}: prefill p50 {med(r['prompt_ms'] for r in good):7.0f} ms  "
              f"clip p50 {med(r.get('clip_ms', 0) for r in good):6.0f} ms  "
              f"reserve sum p50 {med(sum(r.get('reserve_ms', [])) for r in good):6.0f} ms")
    out = OUT / "batching_serial.json"
    out.write_text(json.dumps(_meta(what="직렬 구간 분해", configs=SERIAL_CONFIGS,
                                    runs=runs), indent=1, ensure_ascii=False))
    print(f"wrote {out}")


# ── phase: conc ──────────────────────────────────────────────────

def phase_conc():
    runs = []
    for np_slots in (1, 2, 4):
        tag = f"conc_{np_slots}"
        # ctx 슬롯당 4096 — bench/concurrency.py · 04-b1 §4 와 동일 조건
        srv, _ = start_server(tag, 280, 2048, np_slots=np_slots, ctx=4096 * np_slots)
        try:
            ask([IMAGES[0]])                        # 워밍업
            work = [IMAGES[1:][i % (len(IMAGES) - 1)] for i in range(22)]
            t0 = time.time()

            def one(img, _t0=t0):
                s = time.time() - _t0
                e2e, d = ask([img])
                r = row_from(d["timings"], e2e)
                r["image"] = img.name
                r["start_offset_s"] = round(s, 3)
                return r

            with ThreadPoolExecutor(max_workers=np_slots) as ex:
                rows = list(ex.map(one, work))
            wall = time.time() - t0
        finally:
            srv.terminate()
            srv.wait(timeout=30)
            time.sleep(2)
        agg = {"np": np_slots, "n_reqs": len(rows), "wall_s": round(wall, 2),
               "img_per_s": round(len(rows) / wall, 3),
               "lat_p50_s": round(statistics.median(r["e2e_ms"] for r in rows) / 1000, 2),
               "lat_max_s": round(max(r["e2e_ms"] for r in rows) / 1000, 2)}
        print(f"{tag}: {agg}")
        runs.append({**agg, "rows": rows})
    out = OUT / "batching_conc.json"
    out.write_text(json.dumps(_meta(what="-np 동시성 요청별 원시행", budget=280, ubatch=2048,
                                    runs=runs), indent=1, ensure_ascii=False))
    print(f"wrote {out}")


# ── phase: multi ─────────────────────────────────────────────────

def phase_multi():
    """이미지 N장을 한 요청에 — 요청 내 배칭 API 발동 여부와 장당 비용.

    batches[].entries > 1 이면 우리 clip 그래프가 support_batch 대상이고
    (b10450: gemma4v/internvl/deepseekocr 만), 1 이면 한 장씩 순차 인코딩
    이라는 뜻이다. 어느 쪽인지 자체가 배칭 실험의 사전 데이터다.
    """
    tag = "multi"
    srv, log_path = start_server(tag, 280, 2048, verbose=True,
                                 extra=("--mtmd-batch-max-tokens", "2048"))
    reqs = []
    try:
        ask([IMAGES[0]])                            # 워밍업 (로그에도 1건 남는다)
        plan = [(n, rep) for n in (1, 2, 3, 4, 6) for rep in range(3)]
        for i, (n, rep) in enumerate(plan):
            imgs = [IMAGES[1 + (i + j) % (len(IMAGES) - 1)] for j in range(n)]
            e2e, d = ask(imgs, max_tokens=60)
            r = row_from(d["timings"], e2e)
            r.update({"n_images": n, "rep": rep, "images": [p.name for p in imgs]})
            reqs.append(r)
            print(f"n={n} rep={rep}: prefill {r['prompt_ms']:7.0f} ms  "
                  f"prompt_n {r['prompt_n']}")
    finally:
        srv.terminate()
        srv.wait(timeout=30)
        time.sleep(2)
    phases = parse_phases(log_path)
    if len(phases) == len(reqs) + 1:                # +1 = 워밍업
        for r, ph in zip(reqs, phases[1:], strict=True):
            r.update({k: ph[k] for k in
                      ("clip_ms", "reserve_ms", "kv_ms", "image_tokens", "batches")})
    else:
        print(f"WARN multi: 로그 요청 수 {len(phases)} != {len(reqs) + 1}")
    out = OUT / "batching_multi.json"
    out.write_text(json.dumps(_meta(what="이미지 N장 1요청 — mtmd 배칭 API 프로브",
                                    budget=280, ubatch=2048,
                                    mtmd_batch_max_tokens=2048, rows=reqs),
                              indent=1, ensure_ascii=False))
    print(f"wrote {out}")


PHASES = {"serial": phase_serial, "conc": phase_conc, "multi": phase_multi}


def main():
    check()
    OUT.mkdir(exist_ok=True)
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(PHASES) if which == "all" else [which]
    print(f"build={build_id()}  phases={names}\n")
    for name in names:
        PHASES[name]()


if __name__ == "__main__":
    main()
