"""100 并发压测:每个虚拟用户持独立 token,打完整的 SSE 问答链路。

用法(由 run_all.py 调用,也可手动跑):
    .venv/Scripts/python.exe -m locust -f backend/scripts/stress/locustfile.py \
        --headless -u 100 -r 100 -t 60s --host http://127.0.0.1:8000 --csv report

## 为什么不用 self.client 的自动计时

对 `StreamingResponse`(SSE)来说,Locust 内置的计时是**错的**:它在收到响应对象时
就记下耗时,而 `stream=True` 时"收到响应对象"只代表**响应头到了**,整段回答还在
后面流。实测同一次请求:

    响应头到达      0.173s   <- Locust 报的就是这个量级(8ms 那次是缓存冷的差异)
    done 事件       4.642s   <- 用户真正等待的时间

差 20 倍以上。拿响应头时间当延迟,压测结论会完全失真(看起来"很快",其实用户等了
好几秒;并发一高先崩的是连接池,而响应头时间根本反映不出来)。

所以这里自己管 `requests.Session`、自己对整个流的读取计时,再用
`events.request.fire()` 手动上报。Locust 的统计图表照常工作,但数字是真实端到端耗时。

## 其它设计

- **每人一个 token**。限流按 `user:{id}` 计数,100 个账号各自独立 30 次/分钟;若共用
  一个 token,第 31 个请求起全是 429,测到的是限流不是性能。
- **每人提问错开**。语料按用户序号偏移取用,避免全员同一时刻问同一句 —— 那会命中
  语义缓存(阈值 0.95)跳过整条链路,测不到真实负载。
- **每个问题新开会话**(不传 conversation_id)。让每次请求工作量一致:无历史就不会
  触发 rewrite_query 改写,少一个 LLM 调用这个额外变量。
- **失败按原因分类**。429=限流、500=多半连接池超时、无 done=流被掐断,三者含义
  完全不同,混成一个 error 率就没法归因。
"""

from __future__ import annotations

import csv
import itertools
import json
import time
from pathlib import Path

import requests
from locust import HttpUser, between, events, task

from questions import take

CSV_PATH = Path(__file__).resolve().parent / "stress_users.csv"
TIMEOUT = 180  # 与前端一致:等待整段回答生成完毕

QUESTIONS = take(100)

# 每个虚拟用户在 on_start 里领一个不重复的 token。Locust 跑在 gevent 下是协作式
# 调度,next(counter) 不会被切走,无需加锁。
_token_counter = itertools.count()

# 统计(gevent 协作式调度,普通 dict/list 操作安全)
_fail_reasons: dict[str, int] = {}
_cache_hits = 0
_cache_total = 0
_server_latencies: list[float] = []  # 服务端 done 事件里的 latency_ms,用作交叉验证


def _load_tokens() -> list[tuple[int, str]]:
    """读 prepare_users.py 生成的 CSV,返回 [(user_id, token)]。"""
    if not CSV_PATH.exists():
        raise SystemExit(
            f"缺少 {CSV_PATH.name}。请先跑:\n"
            f"  .venv/Scripts/python.exe backend/scripts/stress/prepare_users.py -n 100"
        )
    with CSV_PATH.open(encoding="utf-8") as f:
        rows = [(int(r["user_id"]), r["token"]) for r in csv.DictReader(f)]
    if not rows:
        raise SystemExit(f"{CSV_PATH.name} 是空的,请重新生成")
    return rows


USERS = _load_tokens()
STREAM_PATH = "/api/chat/stream"


def _fire(response_time_ms: float, exc: str | None, length: int = 0) -> None:
    """手动上报一次请求 —— 绕开 Locust 对 SSE 的错误计时。"""
    events.request.fire(
        request_type="POST",
        name=STREAM_PATH,
        response_time=response_time_ms,
        response_length=length,
        exception=Exception(exc) if exc else None,
        context={},
    )


class RAGUser(HttpUser):
    """一个模拟用户:领 token → 反复提问 → 消费完整 SSE 流。"""

    # 真实用户读完答案会停顿一下再问下一句
    wait_time = between(1.0, 2.5)

    def on_start(self) -> None:
        idx = next(_token_counter)
        self.user_id, self.token = USERS[idx % len(USERS)]
        # 提问起点按用户序号错开,避免同一时刻全员问同一句
        self.q_offset = idx % len(QUESTIONS)
        self.n = 0
        # 自己管会话,不复用 Locust 的 self.client(它的计时不适用于流式响应)
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    def on_stop(self) -> None:
        self.session.close()

    @task
    def ask(self) -> None:
        global _cache_hits, _cache_total
        question = QUESTIONS[(self.q_offset + self.n) % len(QUESTIONS)]
        self.n += 1
        url = f"{self.host}{STREAM_PATH}"

        done_seen = False
        error_seen = ""
        cache_hit = False
        chars = 0
        t0 = time.perf_counter()

        try:
            with self.session.post(
                url, json={"question": question}, stream=True, timeout=TIMEOUT
            ) as resp:
                if resp.status_code != 200:
                    # 必须读完 body 才能释放连接
                    body = resp.text[:120].replace("\n", " ")
                    reason = {
                        429: "429 限流",
                        500: "500 服务端错误(多为连接池超时)",
                        502: "502 网关",
                        503: "503 不可用",
                    }.get(resp.status_code, f"HTTP {resp.status_code}")
                    _fail_reasons[reason] = _fail_reasons.get(reason, 0) + 1
                    _fire((time.perf_counter() - t0) * 1000, f"{reason}: {body}")
                    return

                # 一直读到流结束 —— 这段才是用户真正等待的时间
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(raw[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "token":
                        chars += len(ev.get("content") or "")
                    elif t == "done":
                        done_seen = True
                        cache_hit = bool(ev.get("cache_hit"))
                        if ev.get("latency_ms"):
                            _server_latencies.append(float(ev["latency_ms"]))
                    elif t == "error":
                        error_seen = str(
                            ev.get("message") or ev.get("detail") or "error 事件"
                        )

            elapsed_ms = (time.perf_counter() - t0) * 1000

            if error_seen:
                key = "流内 error 事件"
                _fail_reasons[key] = _fail_reasons.get(key, 0) + 1
                _fire(elapsed_ms, f"服务端事件报错: {error_seen[:80]}", chars)
            elif not done_seen:
                # 连接池耗尽时最典型的表现就是流被中途掐断
                key = "流中断(无 done 事件)"
                _fail_reasons[key] = _fail_reasons.get(key, 0) + 1
                _fire(elapsed_ms, "流结束但没有 done 事件", chars)
            else:
                _cache_total += 1
                if cache_hit:
                    _cache_hits += 1
                _fire(elapsed_ms, None, chars)

        except Exception as e:
            key = f"{type(e).__name__}: {str(e)[:60]}"
            _fail_reasons[key] = _fail_reasons.get(key, 0) + 1
            _fire((time.perf_counter() - t0) * 1000, key)


@events.quitting.add_listener
def _report(environment, **_kwargs) -> None:
    """结束时打印失败归因、缓存命中率与服务端自报延迟。

    这几项决定了主表怎么解读:命中语义缓存的请求跳过了检索与生成,不计入真实链路
    负载;服务端 latency_ms 与客户端实测的差值则反映了排队与网络开销。
    """
    print("\n" + "=" * 62)
    print("失败归因")
    print("=" * 62)
    if not _fail_reasons:
        print("  无失败")
    else:
        for reason, cnt in sorted(_fail_reasons.items(), key=lambda x: -x[1]):
            print(f"  {cnt:5d}  {reason}")

    print("\n缓存命中(命中语义缓存的请求跳过了检索与生成,不计入真实链路负载)")
    if _cache_total:
        print(f"  命中 {_cache_hits} / 成功 {_cache_total} = {_cache_hits / _cache_total:.1%}")
    else:
        print("  无成功请求")

    if _server_latencies:
        s = sorted(_server_latencies)
        n = len(s)
        print(f"\n服务端自报 latency_ms(交叉验证,n={n})")
        print(
            f"  均值 {sum(s) / n:.0f}ms  p50 {s[n // 2]:.0f}ms  "
            f"p95 {s[int(n * 0.95)]:.0f}ms  max {s[-1]:.0f}ms"
        )
    print()
