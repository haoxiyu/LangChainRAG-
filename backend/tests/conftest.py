"""pytest 公共配置。

这些用例是**纯单元测试**:不连数据库、不调云端接口,因此可以在没有
DASHSCOPE_API_KEY、后端也没启动的情况下跑通。
需要真实链路验证的部分由 scripts/ 下的三个联调脚本负责。

运行:
    cd backend
    ../.venv/Scripts/python.exe -m pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让用例可以直接 import app.*,不依赖调用方的当前目录
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
