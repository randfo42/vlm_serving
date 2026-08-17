#!/usr/bin/env python
"""동시성 스케일링 — docs/02-open-questions.md §1c, 제약 (C) 검증.

예측: 비전 인코딩이 원자적·직렬이므로 -np 를 올려도
aggregate throughput 이 거의 오르지 않아야 한다.
"""
import base64, json, statistics, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _paths import ROOT, LLAMA_BIN, MODEL, MMPROJ, build_id, check

IMAGES = sorted((ROOT / "bench" / "images").glob("gen_*.jpg"))
URL = "http://127.0.0.1:8080"
SYSTEM = ("You are a walking-trail assessor. Judge whether the scene is a pleasant walking trail. "
          "Answer only with the requested JSON. Be decisive.")
SCHEMA = {"type": "object", "properties": {"is_trail": {"type": "boolean"}},
          "required": ["is_trail"], "additionalProperties": False}


def start_server(np_slots, budget=280, ctx_per_slot=4096):
    log = open(f"/tmp/llama_conc_{np_slots}.log", "w")
    p = subprocess.Popen([
        str(LLAMA_BIN), "--model", str(MODEL), "--mmproj", str(MMPROJ), "--jinja",
        "-ngl", "99", "--ctx-size", str(ctx_per_slot * np_slots), "--parallel", str(np_slots),
        "--ubatch-size", "2048", "--batch-size", "2048",
        "--image-min-tokens", str(budget), "--image-max-tokens", str(budget),
        "--cache-ram", "0", "--reasoning", "off", "--reasoning-budget", "0",
        "--host", "127.0.0.1", "--port", "8080",
    ], stdout=log, stderr=subprocess.STDOUT)
    for _ in range(180):
        if p.poll() is not None:
            raise RuntimeError(f"server died (np={np_slots}) — see /tmp/llama_conc_{np_slots}.log")
        try:
            urllib.request.urlopen(f"{URL}/health", timeout=1).read(); return p
        except Exception:
            time.sleep(1)
    p.kill(); raise RuntimeError("unhealthy")


def ask(img):
    b64 = base64.b64encode(img.read_bytes()).decode()
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": [
                             {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                             {"type": "text", "text": "Assess this scene."}]}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "t", "schema": SCHEMA, "strict": True}},
            "max_tokens": 60, "temperature": 0}
    t = time.time()
    urllib.request.urlopen(urllib.request.Request(
        f"{URL}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=600).read()
    return time.time() - t


def main():
    check()
    slots = [int(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1 else [1, 2, 4])]
    print(f"build={build_id()}\n")
    hdr =f"{'-np':>4} {'reqs':>5} {'wall_s':>8} {'img/s':>7} {'lat_p50':>8} {'lat_max':>8}"
    print(hdr); print("-" * len(hdr))
    for n in slots:
        srv = start_server(n)
        try:
            ask(IMAGES[0])  # warmup
            work = list(IMAGES[1:])
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=n) as ex:
                lats = list(ex.map(ask, work))
            wall = time.time() - t0
            print(f"{n:>4} {len(work):>5} {wall:>8.2f} {len(work)/wall:>7.3f} "
                  f"{statistics.median(lats):>8.2f} {max(lats):>8.2f}")
        finally:
            srv.terminate(); srv.wait(timeout=30); time.sleep(2)


if __name__ == "__main__":
    main()
