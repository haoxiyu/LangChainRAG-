"""阶梯加压运行器:逐档跑 Locust 并汇总成一张对比表。

每档都是一次独立的 headless 运行(--users N --spawn-rate N 一次性拉起,即该档的峰值),
跑完解析 Locust 的 CSV 拿 p50/p95/p99/吞吐/失败数,最后打印横向对比并标出拐点。

用法:
    .venv/Scripts/python.exe backend/scripts/stress/run_all.py --label default
    .venv/Scripts/python.exe backend/scripts/stress/run_all.py --label tuned --levels 10,30,60,100

报告落在 backend/scripts/stress/reports/<label>_u<N>_stats.csv。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import csv
import subprocess

STRESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = STRESS_DIR.parents[2]
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
LOCUSTFILE = STRESS_DIR / "locustfile.py"
REPORTS = STRESS_DIR / "reports"
HOST = "http://127.0.0.1:8000"


def run_level(level: int, label: str, run_time: str, show_output: bool) -> dict[str, float]:
    """跑一档并发,返回该档的核心指标。"""
    prefix = REPORTS / f"{label}_u{level}"
    cmd = [
        str(PYTHON), "-m", "locust", "-f", str(LOCUSTFILE),
        "--headless", "-u", str(level), "-r", str(level), "-t", run_time,
        "--host", HOST, "--csv", str(prefix), "--only-summary",
    ]
    print(f"\n{'=' * 62}\n[{label}] {level} 并发,持续 {run_time}\n{'=' * 62}")
    proc = subprocess.run(cmd, cwd=str(STRESS_DIR), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")

    # Locust 的失败归因与缓存命中率打在 stdout 的监听器里,原样透出
    if show_output and proc.stdout:
        tail = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        print("\n".join(tail[-24:]))
    if proc.returncode != 0:
        print(f"!! locust 退出码 {proc.returncode}")
        if proc.stderr:
            print(proc.stderr[-800:])

    return parse_stats(prefix)


def parse_stats(prefix: Path) -> dict[str, float]:
    """从 Locust 的 _stats.csv 里取 Aggregated 那一行。"""
    stats_file = prefix.with_name(prefix.name + "_stats.csv")
    if not stats_file.exists():
        return {}
    with stats_file.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Name") == "Aggregated":
                def num(key: str) -> float:
                    try:
                        return float(row.get(key) or 0)
                    except ValueError:
                        return 0.0

                count = num("Request Count")
                fails = num("Failure Count")
                return {
                    "requests": count,
                    "failures": fails,
                    "fail_rate": (fails / count) if count else 0.0,
                    "rps": num("Requests/s"),
                    "p50": num("50%"),
                    "p95": num("95%"),
                    "p99": num("99%"),
                    "max": num("Max Response Time"),
                }
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="阶梯加压")
    parser.add_argument("--label", default="run", help="报告前缀,如 default / tuned")
    parser.add_argument("--levels", default="10,30,60,100", help="并发档位,逗号分隔")
    parser.add_argument("--time", default="60s", help="每档持续时长")
    parser.add_argument("--quiet", action="store_true", help="不打印 locust 明细输出")
    parser.add_argument("--skip", default="", help="跳过已跑过的档位,如 10,30")
    args = parser.parse_args()

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    skip = {int(x) for x in args.skip.split(",") if x.strip()}
    REPORTS.mkdir(parents=True, exist_ok=True)

    results: dict[int, dict[str, float]] = {}
    for lv in levels:
        if lv in skip:
            results[lv] = parse_stats(REPORTS / f"{args.label}_u{lv}")
            print(f"\n[{args.label}] {lv} 并发: 跳过,读已有报告")
            continue
        results[lv] = run_level(lv, args.label, args.time, not args.quiet)

    # ---------- 汇总 ----------
    print("\n\n" + "=" * 78)
    print(f"汇总 [{args.label}]")
    print("=" * 78)
    header = f"{'并发':>5} {'请求数':>7} {'失败':>6} {'失败率':>8} {'吞吐/s':>8} {'p50':>8} {'p95':>9} {'p99':>9}"
    print(header)
    print("-" * 78)
    knee = None
    for lv in levels:
        r = results.get(lv) or {}
        if not r:
            print(f"{lv:>5}   无数据")
            continue
        line = (
            f"{lv:>5} {r['requests']:>7.0f} {r['failures']:>6.0f} {r['fail_rate']:>7.1%} "
            f"{r['rps']:>8.1f} {r['p50']:>7.0f}ms {r['p95']:>8.0f}ms {r['p99']:>8.0f}ms"
        )
        # 首个"出现失败"或"p95 比上一档翻倍"的档位即为拐点
        if knee is None:
            prev = results.get(levels[levels.index(lv) - 1]) if levels.index(lv) > 0 else None
            if r["failures"] > 0:
                knee = (lv, "开始出现失败")
            elif prev and prev.get("p95") and r["p95"] > prev["p95"] * 2:
                knee = (lv, f"p95 较上一档翻倍({prev['p95']:.0f}ms → {r['p95']:.0f}ms)")
        print(line + ("   <== 拐点" if knee and knee[0] == lv else ""))

    print("-" * 78)
    if knee:
        print(f"拐点: {knee[0]} 并发 —— {knee[1]}")
    else:
        print("拐点: 本组档位内未出现明显退化")
    print(f"\n明细 CSV: {REPORTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
