"""浏览器端联调校验:用 CDP 驱动无头浏览器,在真实页面里走完一次问答。

覆盖的是纯接口测试测不到的部分:Vue 是否真的挂载、SSE 是否真的流式渲染、
回答里的 [n] 角标能否点击并联动引用卡片。

前置条件:
  1. 后端已在 127.0.0.1:8000 运行
  2. 已执行过 `npm run build`(后端会托管 frontend/dist)
  3. 机器上装有 Edge 或 Chrome

用法:  .venv/Scripts/python.exe backend/scripts/browser_check.py
"""

from __future__ import annotations

import sys

# Windows 控制台默认 GBK,模型回答里可能带 emoji,直接 print 会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import httpx
import websocket

BASE = "http://127.0.0.1:8000"
QUESTION = "屏幕的尺寸和刷新率分别是多少?"
PORT = 9333

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "frontend" / "dist"
PROBE = DIST / "__probe.html"
PROFILE = ROOT / ".browser-check-profile"

_CANDIDATES = [
    os.environ.get("BROWSER_PATH", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '失败'}] {name}  {'' if ok else detail}")


def find_browser() -> str | None:
    for path in _CANDIDATES:
        if path and Path(path).exists():
            return path
    return None


class Page:
    """极简 CDP 客户端,只用到 Runtime.evaluate。"""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=180)
        self.seq = 0
        self.send("Runtime.enable")
        self.send("Page.enable")

    def send(self, method: str, params: dict | None = None) -> int:
        self.seq += 1
        self.ws.send(
            json.dumps({"id": self.seq, "method": method, "params": params or {}})
        )
        return self.seq

    def evaluate(self, expr: str, timeout: float = 30.0):
        """执行 JS 并等待返回值(自动等待 Promise)。"""
        mid = self.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError(msg["error"])
            res = msg["result"]
            if res.get("exceptionDetails"):
                raise RuntimeError(res["exceptionDetails"].get("text", "JS 异常"))
            return res.get("result", {}).get("value")
        raise TimeoutError(f"等待 JS 结果超时: {expr[:60]}")

    def wait_for(self, expr: str, timeout: float = 60.0, interval: float = 1.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expr):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})


def wait_for_devtools(port: int, timeout: float = 20.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=2
            ) as resp:
                targets = json.load(resp)
            page = next((t for t in targets if t["type"] == "page"), None)
            if page:
                return page["webSocketDebuggerUrl"]
        except Exception:
            continue
    return None


def port_in_use(port: int) -> bool:
    """调试端口上是否已有人在监听。"""
    import socket

    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_port_owner(port: int) -> None:
    """杀掉占用该端口的进程。

    只在 Windows 上走 netstat + taskkill;拿不到就静默跳过,让后面的
    启动流程自己去报错,不让清理逻辑本身变成失败原因。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return
    pids = {
        parts[-1]
        for line in out.splitlines()
        if f":{port} " in line and "LISTENING" in line
        for parts in [line.split()]
        if parts and parts[-1].isdigit()
    }
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", pid],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass


def write_probe(token: str, user: dict, route: str) -> None:
    """同源探针页:写入登录态后跳转到目标路由。

    route 需自带前导斜杠(如 "/chat/12")。这里刻意不再补斜杠 ——
    补了会拼出 `#//chat/12`,路由匹配不上会落到 catch-all 被重定向到 /chat,
    表现为「前端把它当成新对话」,而错误现场很难看出来。

    注意这里会写入一个真实 JWT,脚本结束必须删除,不能留在 dist 里。
    """
    assert route.startswith("/"), f"route 需以 / 开头: {route!r}"
    PROBE.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><script>\n"
        f"localStorage.setItem('rag_token', {json.dumps(token)});\n"
        f"localStorage.setItem('rag_user', {json.dumps(json.dumps(user))});\n"
        f"location.replace('/index.html#{route}');\n"
        "</script></body></html>",
        encoding="utf-8",
    )


def main() -> int:
    browser = find_browser()
    if browser is None:
        print("未找到 Edge / Chrome,跳过浏览器校验(可用 BROWSER_PATH 指定)")
        return 0

    if not DIST.is_dir():
        print(f"未找到前端构建产物 {DIST},请先执行 npm run build")
        return 1

    client = httpx.Client(timeout=60)
    login = client.post(
        f"{BASE}/api/auth/login", json={"username": "admin", "password": "123456"}
    ).json()
    token, user = login["access_token"], login["user"]
    headers = {"Authorization": f"Bearer {token}"}

    kbs = client.get(f"{BASE}/api/knowledge-bases", headers=headers).json()
    # 问答检索全部知识库,但全库都空的话必然没有引用 —— 那看着像前端坏了,
    # 其实是没资料可检索,先在这里拦下来。
    usable = [k for k in kbs if k.get("chunk_count", 0) > 0]
    if not usable:
        print("没有已入库(分块数 > 0)的知识库,请先用 admin 上传一份文档")
        return 1
    total_chunks = sum(k["chunk_count"] for k in usable)
    print(f"可检索知识库 {len(usable)} 个,共 {total_chunks} 块")

    conv = client.post(
        f"{BASE}/api/conversations",
        headers=headers,
        json={"title": "浏览器联调"},
    ).json()
    conv_id = conv["id"]
    print(f"已准备会话 {conv_id}")
    # 会话不再绑定知识库:检索范围是全部库,由打分排序决定引用谁
    check("新建会话不再绑定知识库", conv.get("knowledge_base_id") is None,
          f"实际={conv.get('knowledge_base_id')}")

    write_probe(token, user, f"/chat/{conv_id}")

    # 上一次异常退出可能留下无头浏览器仍占着调试端口和 profile 目录。此时新实例
    # 绑不上端口,脚本却会连到那个残留实例上,navigate 永远不生效 —— 表现为
    # 「页面挂载超时」这种完全指错方向的报错。所以在启动前先把残留清干净。
    if port_in_use(PORT):
        print(f"调试端口 {PORT} 被占用,可能有上次残留的浏览器实例")
        kill_port_owner(PORT)
        time.sleep(1.5)
    shutil.rmtree(PROFILE, ignore_errors=True)

    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={PROFILE}",
            f"--remote-debugging-port={PORT}",
            # 不加这个,CDP 会以 Origin 不可信为由拒绝 WebSocket 握手
            "--remote-allow-origins=*",
            # 必须显式给桌面尺寸:无头窗口默认只有 754px 宽,而 main.css 在 900px
            # 以下会把 .chat-sidebar 整个 display:none,侧栏相关的断言就全成了
            # 对零尺寸元素取值 —— 恒等于 0,写什么条件都「通过」。
            "--window-size=1440,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ws_url = wait_for_devtools(PORT)
        if ws_url is None:
            print("无法连上浏览器调试端口")
            return 1
        p = Page(ws_url)

        print("\n=== 页面加载 ===")
        p.navigate(f"{BASE}/__probe.html")
        ok = p.wait_for("!!document.querySelector('.chat-textarea textarea')", timeout=45)
        check("聊天页在真实浏览器中挂载", ok)
        if not ok:
            return 1
        check("新会话没有历史消息",
              p.evaluate("document.querySelectorAll('.msg-row').length") == 0)
        # 顶栏改为静态提示:检索跨全部知识库,页面不该再出现任何选库控件
        topbar_ok = p.wait_for(
            "(document.querySelector('.chat-topbar')?.textContent||'')"
            ".includes('自动检索全部知识库')", timeout=20)
        check("顶栏声明自动检索全部知识库", topbar_ok)
        if not topbar_ok:
            # 这里失败通常意味着前端根本没进入 /chat/{id},而是当成新对话了
            print(f"      诊断 address={p.evaluate('location.href')}")
            print(f"      诊断 顶栏={p.evaluate('document.querySelector(`.chat-topbar`)?.textContent?.trim()')}")
            print(f"      诊断 侧栏会话数={p.evaluate('document.querySelectorAll(`.chat-conv-item`).length')}")
        # 用户无从判断资料分在哪个库,所以选库控件必须彻底消失,而不是禁用
        check("页面已无知识库下拉框",
              not p.evaluate(
                  "!!document.querySelector('.chat-topbar .el-select')"
                  " || !!document.querySelector('.chat-topbar select')"))

        # 会话条目的「三个点」曾经被甩到卡片左边缘外、一半被裁掉:el-dropdown 会给
        # 自己套一个 0×0 的包裹层,绝对定位的 right 是相对它解析的;那个包裹层还
        # 在流里占掉一行高度,把条目从 55px 撑到 74px。这里量几何而不是看类名,
        # 因为坏掉的时候类名和 DOM 结构都是对的,只有位置不对。
        conv_geo = p.evaluate("""(() => {
          const item = document.querySelector('.chat-conv-item');
          const more = item && item.querySelector('.conv-more');
          if (!more) return {found: false, item: !!item};
          const a = item.getBoundingClientRect();
          const b = more.getBoundingClientRect();
          const num = v => Math.round(v * 10) / 10;
          return {
            found: true,
            item: {l: num(a.left), r: num(a.right), w: num(a.width), h: num(a.height)},
            more: {l: num(b.left), r: num(b.right), w: num(b.width), h: num(b.height)},
            dy: num(Math.abs((b.top + b.bottom) / 2 - (a.top + a.bottom) / 2)),
            viewport: innerWidth + 'x' + innerHeight,
          };
        })()""")
        check("会话条目的三个点在条目内部、靠右且垂直居中",
              bool(conv_geo and conv_geo.get("found")
                   and conv_geo["more"]["w"] > 0 and conv_geo["more"]["h"] > 0
                   and conv_geo["more"]["l"] >= conv_geo["item"]["l"] - 0.5
                   and conv_geo["more"]["r"] <= conv_geo["item"]["r"] + 0.5
                   and (conv_geo["more"]["l"] + conv_geo["more"]["r"]) / 2
                       > (conv_geo["item"]["l"] + conv_geo["item"]["r"]) / 2
                   and conv_geo["dy"] <= 4),
              f"实测={conv_geo}")
        # 条目高度只该容下标题 + 时间 + 上下内边距,多出来的空白说明有元素在流里占位。
        # 要求高度非零:元素被响应式隐藏时 getBoundingClientRect() 全是 0,
        # 不卡这一条的话 0 <= 64 会恒真,断言就白写了。
        item_h = p.evaluate(
            "document.querySelector('.chat-conv-item')?.getBoundingClientRect().height")
        check("会话条目没有多余空白(0 < 高度 <= 64px)",
              item_h is not None and 0 < item_h <= 64, f"实际高度={item_h}")

        # ---------- 输入并发送 ----------
        print("\n=== 发送提问 ===")
        typed = p.evaluate(
            "(() => {"
            "  const ta = document.querySelector('.chat-textarea textarea');"
            "  const setter = Object.getOwnPropertyDescriptor("
            "    window.HTMLTextAreaElement.prototype, 'value').set;"
            f"  setter.call(ta, {json.dumps(QUESTION)});"
            "  ta.dispatchEvent(new Event('input', { bubbles: true }));"
            "  return ta.value;"
            "})()"
        )
        check("输入框接受问题", typed == QUESTION, f"实际={typed!r}")

        # 必须与上一步分开:同一次执行里 DOM 还没 patch,按钮仍是 disabled,点击无效
        time.sleep(1)
        clicked = p.evaluate(
            "[...document.querySelectorAll('.chat-input-actions button')]"
            ".find(b => b.textContent.includes('发送'))"
            "?.click() ?? 'clicked'"
        )
        check("点击发送按钮", clicked == "clicked")

        check("用户提问立即上屏",
              p.wait_for(
                  "document.querySelector('.msg-row.user .msg-bubble')"
                  "?.textContent.includes('屏幕')",
                  timeout=15))

        # ---------- 等待流式回答 ----------
        print("\n=== 等待流式回答 ===")
        # 命中语义缓存时回答可能很短,这里只要求出现内容
        grew = p.wait_for(
            "(document.querySelector('.msg-row.assistant .msg-bubble')"
            "?.textContent||'').length > 0",
            timeout=120)
        check("助手气泡出现内容(流式渲染)", grew)
        if not grew:
            return 1

        check("引用卡片渲染出来",
              p.wait_for("document.querySelectorAll('.citation-item').length > 0",
                         timeout=30))
        check("生成结束后不再有流式光标",
              p.wait_for("!document.querySelector('.cursor-blink') "
                         "&& !document.querySelector('.thinking')", timeout=120))

        # ---------- 渲染内容 ----------
        print("\n=== 渲染内容 ===")
        answer = p.evaluate(
            "document.querySelector('.msg-row.assistant .msg-bubble').textContent")
        print(f"  回答片段: {answer[:120]}")

        check("回答正文里有可点击的 [n] 角标",
              p.evaluate("document.querySelectorAll('.msg-bubble .citation-ref').length") > 0)
        check("引用卡片已渲染",
              p.evaluate("document.querySelectorAll('.citation-item').length") > 0)
        # 库名标签排在文件名标签之前,所以不能只看第一个 el-tag,要在整组里找
        check("引用卡片带来源文件名",
              p.evaluate("[...document.querySelectorAll('.citation-item .el-tag')]"
                         ".some(t => t.textContent.includes('.md'))"))
        # 跨库检索后,只报文件名无法核对内容出自哪个库,库名必须一起显示
        kb_names = [k["name"] for k in usable]
        check("引用卡片标注来源知识库",
              p.evaluate(
                  "(() => {"
                  f"  const names = {json.dumps(kb_names)};"
                  "  const tags = [...document.querySelectorAll('.citation-item .el-tag')]"
                  "    .map(t => t.textContent.trim());"
                  "  return tags.some(t => names.includes(t));"
                  "})()"),
              f"候选库名={kb_names}")
        check("引用卡片显示相关度分数",
              p.evaluate("[...document.querySelectorAll('.citation-item .el-tag')]"
                         ".some(t => t.textContent.includes('相关度'))"))
        check("回答下方显示耗时", p.evaluate("!!document.querySelector('.msg-meta')"))
        check("没有越界的引用角标(否则会被置灰)",
              p.evaluate("document.querySelectorAll('.citation-ref.invalid').length") == 0)
        check("没有走「未找到」兜底", "未找到" not in answer,
              f"回答={answer[:50]}")

        # ---------- 引用交互 ----------
        print("\n=== 引用交互 ===")
        has_ref = p.evaluate("!!document.querySelector('.msg-bubble .citation-ref')")
        has_card = p.evaluate("!!document.querySelector('.citation-item button')")
        if has_ref and has_card:
            p.evaluate("document.querySelector('.msg-bubble .citation-ref').click()")
            time.sleep(1.0)
            check("点击角标后对应引用卡片高亮",
                  p.evaluate("!!document.querySelector('.citation-item.is-active')"))

            p.evaluate("document.querySelectorAll('.citation-item button')[0].click()")
            time.sleep(0.6)
            check("引用原文可展开",
                  p.evaluate("!document.querySelector('.citation-body')"
                             ".classList.contains('clamped')"))
        else:
            # 没有角标就没有可交互的对象,如实记为失败,不能让脚本崩在半路
            check("点击角标后对应引用卡片高亮", False, "回答里没有引用角标")
            check("引用原文可展开", False, "没有引用卡片")

        # ---------- 落库校验 ----------
        print("\n=== 落库校验 ===")
        time.sleep(2)
        detail = client.get(
            f"{BASE}/api/conversations/{conv_id}", headers=headers
        ).json()
        roles = [m["role"] for m in detail["messages"]]
        check("问答已持久化(2 条消息)", roles == ["user", "assistant"], f"{roles}")
        if len(detail["messages"]) > 1:
            saved = detail["messages"][1]
            check("引用快照已落库", bool(saved["references"]))
            # 页面上 [n] 被渲染成角标元素,textContent 里没有方括号,比对前先归一化
            norm = lambda s: re.sub(r"[\s\[\]]+", "", s)  # noqa: E731
            check("落库内容与页面显示一致",
                  norm(saved["content"])[:24] == norm(answer)[:24],
                  f"库={saved['content'][:40]!r} 页={answer[:40]!r}")
    finally:
        # Edge 会派生一堆子进程,只 terminate 父进程的话它们会继续占着调试端口和
        # profile 目录 —— 下次运行就会静默连到这些残留实例上,报出「页面挂载超时」
        # 这种和真实原因毫不相干的错。所以整棵树一起杀。
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass
        kill_port_owner(PORT)
        # 探针页里含真实 JWT,绝不能留在 dist 目录里
        PROBE.unlink(missing_ok=True)
        # Windows 释放文件句柄有延迟,杀完进程立刻删可能还是被占,重试几次
        for attempt in range(5):
            shutil.rmtree(PROFILE, ignore_errors=True)
            if not PROFILE.exists():
                break
            time.sleep(1.0)
        if PROFILE.exists():
            print(f"      警告:临时配置目录未能删除,请手动清理 {PROFILE}")
        try:
            client.delete(f"{BASE}/api/conversations/{conv_id}", headers=headers)
        except Exception:
            pass
        print("\n已清理:探针页、浏览器临时配置、联调会话")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{'=' * 46}")
    print(f"浏览器联调:{len(results) - len(failed)} 通过 / {len(failed)} 失败")
    for name in failed:
        print(f"  未通过: {name}")
    print("=" * 46)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
