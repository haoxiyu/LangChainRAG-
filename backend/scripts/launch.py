"""一键启动脚本:给不熟悉命令行的人用(双击根目录的「启动系统.bat」)。

做三件事:
1. 把 run.py 作为子进程拉起来(不直接调 uvicorn,保证事件循环策略和手动
   启动完全一致 —— 见 README 第八节 Windows 事件循环那一段);
2. 轮询 /api/health,等服务**真正就绪**后再打开浏览器,避免"打开太快看到
   无法访问此网站";
3. 全程不退出,按 Ctrl+C 一并结束子进程。

若检测到服务已经在跑,就只打开浏览器,不重复启动(避免端口占用报错)。
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# Windows 控制台可能是 GBK,直接 print 中文/emoji 会抛 UnicodeEncodeError。
# line_buffering 让提示语即时显示 —— 输出被重定向到文件时 stdout 默认是块缓冲,
# 不刷的话"启动完成"这类关键提示要等进程结束才看得到。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

URL = "http://127.0.0.1:8000"
HEALTH = f"{URL}/api/health"

BANNER = "  " + "=" * 58

CONSOLE_TITLE = "电商商品知识库问答系统 —— 请勿关闭本窗口"


def set_console_title(title: str) -> None:
    """设置控制台窗口标题。

    不用 cmd 的 `title` 命令:那条命令走的是 OEM 代码页,中文会乱码或报错。
    这里直接调 Win32 的宽字符 API,不受代码页影响。
    """
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:  # 非 Windows 或没有控制台时静默跳过,不影响启动
        pass


def health_ok(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def wait_until_ready(timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok():
            return True
        time.sleep(1.5)
    return False


def main() -> int:
    set_console_title(CONSOLE_TITLE)

    if not PYTHON.exists():
        print(f"\n  [错误] 没找到 Python 环境:{PYTHON}")
        print("  请先按 README 第四节安装依赖。\n")
        return 1

    if health_ok():
        print("\n  系统已经在运行了,直接为你打开浏览器。")
        print(f"  地址:{URL}\n")
        webbrowser.open(URL)
        return 0

    proc = subprocess.Popen([str(PYTHON), "run.py"], cwd=str(BACKEND))

    ready = wait_until_ready()
    if ready:
        print(f"\n{BANNER}")
        print("   启动完成!浏览器正在打开……")
        print(f"   地址:{URL}")
        print("   登录账号:admin      密码:123456")
        print(BANNER)
        print("\n   ★ 这个黑窗口不要关闭 —— 关掉它就等于关闭系统。")
        print("     用完之后,回到这个窗口按 Ctrl+C 即可停止。\n")
        webbrowser.open(URL)
    else:
        print(f"\n{BANNER}")
        print("   等了 3 分钟服务仍未就绪,请把本窗口里的报错信息截图。")
        print("   若看到 'Timeout starting server',等 10 秒后重新双击本文件即可。")
        print(BANNER + "\n")

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("\n  正在停止……")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("  已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
