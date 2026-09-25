"""端到端联调校验:验证前端所依赖的接口契约。

与 api_test.py 的区别:这里只测「前端真的会用到」的那部分,重点是
SSE 事件序列、引用片段结构,以及回答里的 [n] 能否对上引用列表。

直接跑:  .venv/Scripts/python.exe backend/scripts/e2e_check.py
前置条件:后端已在 127.0.0.1:8000 运行。
"""

from __future__ import annotations

import sys

# Windows 控制台默认 GBK,模型回答里可能带 emoji,直接 print 会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import re
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample_data" / "星辰X1智能手机_商品知识库.md"
UPLOAD_DIR = ROOT / "uploads"

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def sse_events(client: httpx.Client, headers: dict, payload: dict) -> list[dict]:
    """按前端的解析方式读 SSE:以空行分块,取 data: 行。"""
    events: list[dict] = []
    with client.stream("POST", f"{BASE}/api/chat/stream", headers=headers,
                       json=payload, timeout=180) as resp:
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        buffer = ""
        for raw in resp.iter_text():
            buffer += raw
            blocks = buffer.split("\n\n")
            buffer = blocks.pop()
            for block in blocks:
                line = block.strip()
                if not line.startswith("data:"):
                    continue
                events.append(json.loads(line[5:].strip()))
    return events


def main() -> int:
    client = httpx.Client(timeout=120)

    # ---------- 1. 管理员登录 ----------
    section("1. 登录")
    r = client.post(f"{BASE}/api/auth/login",
                    json={"username": "admin", "password": "123456"})
    check("admin/123456 登录成功", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        return 1
    admin_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    check("登录返回 role=admin", r.json()["user"]["role"] == "admin")

    r = client.get(f"{BASE}/api/auth/me", headers=admin_h)
    check("GET /auth/me 可用(前端刷新用户信息)", r.status_code == 200)

    # ---------- 2. 知识库 + 文档入库 ----------
    section("2. 知识库与文档")
    # 前端的上传预检不再自己写死限制,而是读这份配置。两边一旦跑偏,用户会在
    # 浏览器里被一个错误的上限拦下(曾经前端 20MB、后端 50MB)。
    r = client.get(f"{BASE}/api/admin/upload-limits", headers=admin_h)
    limits = r.json() if r.status_code == 200 else {}
    check("上传限制可下发(前端据此预检,不再各写一份常量)",
          r.status_code == 200 and limits.get("allowed_extensions")
          and isinstance(limits.get("max_upload_mb"), int)
          and limits["max_upload_mb"] > 0, r.text[:150])
    check("下发的格式覆盖解析器实际支持的类型",
          {".md", ".txt", ".pdf", ".docx"} <= set(limits.get("allowed_extensions") or []),
          f"下发={limits.get('allowed_extensions')}")

    kb_name = f"联调知识库-{int(time.time())}"
    r = client.post(f"{BASE}/api/knowledge-bases", headers=admin_h,
                    json={"name": kb_name, "description": "e2e 检查用"})
    check("创建知识库", r.status_code == 201, r.text[:200])
    kb_id = r.json()["id"]

    r = client.post(f"{BASE}/api/knowledge-bases/{kb_id}/documents", headers=admin_h,
                    files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/markdown")})
    check("上传文档", r.status_code in (200, 201), r.text[:200])
    doc_id = r.json()["id"]

    status = ""
    for _ in range(60):
        time.sleep(2)
        r = client.get(f"{BASE}/api/knowledge-bases/{kb_id}/documents", headers=admin_h)
        doc = next((d for d in r.json() if d["id"] == doc_id), None)
        status = doc["status"] if doc else "?"
        if status in ("ready", "failed"):
            break
    check("文档入库完成(ready)", status == "ready", f"实际={status}")

    r = client.get(f"{BASE}/api/knowledge-bases/{kb_id}/documents/{doc_id}/chunks",
                   headers=admin_h, params={"page": 1, "page_size": 10})
    chunks = r.json()
    check("分块预览可分页", r.status_code == 200 and chunks["total"] > 0,
          f"total={chunks.get('total')}")
    check("分块带标题路径前缀(分块质量)",
          any("【" in c["content"] for c in chunks["items"]))

    # ---------- 3. 流式问答(核心) ----------
    section("3. 流式问答与引用")
    # 不传知识库:检索跨全部知识库,不用用户选库
    events = sse_events(client, admin_h, {"question": "这款手机的电池容量是多少?"})
    types = [e["type"] for e in events]

    check("首个事件是 conversation(前端据此更新地址栏)",
          types and types[0] == "conversation", f"types={types[:4]}")
    conv_id = events[0]["conversation_id"] if types else None
    check("发出 status 事件", "status" in types)
    check("references 在 token 之前发出(先渲染来源再出字)",
          "references" in types and "token" in types
          and types.index("references") < types.index("token"))
    check("存在 token 流式分片", types.count("token") > 1, f"token 数={types.count('token')}")
    check("以 done 收尾", types and types[-1] == "done", f"末尾={types[-1:]}")

    refs = next((e["references"] for e in events if e["type"] == "references"), [])
    check("引用非空", len(refs) > 0, f"len={len(refs)}")

    if refs:
        required = {"index", "chunk_id", "document_id", "filename",
                    "knowledge_base_name", "chunk_index", "content"}
        missing = required - set(refs[0].keys())
        check("引用字段完整(前端卡片依赖)", not missing, f"缺={missing}")
        check("引用序号从 1 开始连续",
              [r_["index"] for r_ in refs] == list(range(1, len(refs) + 1)))
        check("引用内容非空", all(r_["content"].strip() for r_ in refs))
        check("引用标注来源知识库", all(r_["knowledge_base_name"] for r_ in refs),
              f"来源库={sorted({r_['knowledge_base_name'] for r_ in refs})}")

    answer = "".join(e.get("content", "") for e in events if e["type"] == "token")
    check("回答非空", len(answer) > 10, f"len={len(answer)}")

    cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer)}
    check("回答里出现 [n] 引用标记", len(cited) > 0, f"cited={cited}")
    check("引用的 [n] 都能对上引用列表(否则前端角标会置灰)",
          cited <= set(range(1, len(refs) + 1)), f"越界={cited - set(range(1, len(refs) + 1))}")

    done = next((e for e in events if e["type"] == "done"), {})
    check("done 事件带 latency_ms", isinstance(done.get("latency_ms"), int))

    # 电池容量这类问题应当命中规格相关的分块
    hit_names = {r_["filename"] for r_ in refs}
    check("引用标注了来源文件", hit_names and all(hit_names), f"{hit_names}")

    # ---------- 4. 追问:历史感知改写 ----------
    section("4. 多轮改写与历史持久化")
    events2 = sse_events(client, admin_h,
                         {"conversation_id": conv_id, "question": "那它支持多少瓦快充?"})
    types2 = [e["type"] for e in events2]
    check("追问返回同一会话", types2[0] == "conversation"
          and events2[0]["conversation_id"] == conv_id)
    rewrite = next((e["query"] for e in events2 if e["type"] == "rewrite"), None)
    check("触发历史感知改写(把「它」补全)", rewrite is not None, "未触发改写")
    if rewrite:
        print(f"        改写后:{rewrite}")

    # ---------- 5. 历史找回 ----------
    section("5. 历史会话找回")
    r = client.get(f"{BASE}/api/conversations/{conv_id}", headers=admin_h)
    check("会话详情可取", r.status_code == 200)
    detail = r.json()
    check("会话不再绑定知识库(检索跨全库,由打分决定引用)",
          detail["conversation"]["knowledge_base_id"] is None,
          f"kb={detail['conversation'].get('knowledge_base_id')}")
    msgs = detail["messages"]
    check("消息按时间正序返回", [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"],
          f"{[m['role'] for m in msgs]}")
    check("助手消息的引用快照已落库",
          any(m["role"] == "assistant" and m["references"] for m in msgs))
    check("助手消息记录了耗时", any(m["role"] == "assistant" and m["latency_ms"] > 0 for m in msgs))
    check("会话标题已自动生成", detail["conversation"]["title"] != "新对话",
          detail["conversation"]["title"])

    r = client.get(f"{BASE}/api/conversations", headers=admin_h, params={"limit": 100})
    page = r.json()
    check("会话列表含该会话(侧栏可见)",
          any(c["id"] == conv_id for c in page["items"]))
    check("会话列表返回 total(前端分页显示正确条数)",
          isinstance(page.get("total"), int) and page["total"] >= len(page["items"]),
          f"total={page.get('total')}")

    r = client.get(f"{BASE}/api/conversations", headers=admin_h,
                   params={"keyword": "不存在的关键词xyz", "limit": 10})
    check("会话标题搜索可用", r.json()["items"] == [] and r.json()["total"] == 0,
          f"{r.json()}")

    # 会话不再绑定知识库:检索范围与任何库无关,所以没有「改绑」这回事了
    r = client.patch(f"{BASE}/api/conversations/{conv_id}", headers=admin_h,
                     json={"title": "联调改名"})
    check("重命名仍然可用",
          r.status_code == 200 and r.json()["title"] == "联调改名", r.text[:120])
    check("重命名不影响检索(会话上不再有知识库字段值)",
          r.json()["knowledge_base_id"] is None, f"kb={r.json().get('knowledge_base_id')}")

    # ---------- 6. 普通用户权限隔离 ----------
    section("6. 普通用户权限隔离")
    uname = f"e2e_user_{int(time.time())}"
    r = client.post(f"{BASE}/api/auth/register",
                    json={"username": uname, "password": "test123456"})
    check("注册普通用户", r.status_code in (200, 201), r.text[:200])
    r = client.post(f"{BASE}/api/auth/login",
                    json={"username": uname, "password": "test123456"})
    user_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    check("注册用户角色固定为 user", r.json()["user"]["role"] == "user")

    check("普通用户读取知识库列表允许(问答时用于展示已收录范围)",
          client.get(f"{BASE}/api/knowledge-bases", headers=user_h).status_code == 200)
    check("普通用户建知识库被拒 403",
          client.post(f"{BASE}/api/knowledge-bases", headers=user_h,
                      json={"name": "x"}).status_code == 403)
    check("普通用户上传文档被拒 403",
          client.post(f"{BASE}/api/knowledge-bases/{kb_id}/documents", headers=user_h,
                      files={"file": ("a.txt", b"hi", "text/plain")}).status_code == 403)
    check("普通用户看统计被拒 403",
          client.get(f"{BASE}/api/admin/stats", headers=user_h).status_code == 403)
    check("普通用户读上传限制被拒 403",
          client.get(f"{BASE}/api/admin/upload-limits", headers=user_h).status_code == 403)
    check("普通用户读他人会话返回 404(不泄露存在性)",
          client.get(f"{BASE}/api/conversations/{conv_id}", headers=user_h).status_code == 404)
    check("普通用户删他人会话返回 404",
          client.delete(f"{BASE}/api/conversations/{conv_id}", headers=user_h).status_code == 404)

    # 独立会话:普通用户自己新建,检索范围同样是全部库
    r = client.post(f"{BASE}/api/conversations", headers=user_h,
                    json={"title": "用户会话"})
    check("普通用户可新建会话", r.status_code == 201, r.text[:120])
    own_conv = r.json()["id"]
    check("普通用户可读取自己的会话",
          client.get(f"{BASE}/api/conversations/{own_conv}", headers=user_h).status_code == 200)

    # ---------- 7. 修改密码 ----------
    section("7. 修改密码")
    check("旧密码错误被拒",
          client.post(f"{BASE}/api/auth/change-password", headers=user_h,
                      json={"old_password": "wrongpass", "new_password": "newpass123"}
                      ).status_code in (400, 401))
    check("正确旧密码可改密",
          client.post(f"{BASE}/api/auth/change-password", headers=user_h,
                      json={"old_password": "test123456", "new_password": "newpass123"}
                      ).status_code == 200)
    check("新密码可登录",
          client.post(f"{BASE}/api/auth/login",
                      json={"username": uname, "password": "newpass123"}).status_code == 200)
    check("旧密码失效",
          client.post(f"{BASE}/api/auth/login",
                      json={"username": uname, "password": "test123456"}).status_code == 401)

    # ---------- 清理 ----------
    section("清理")
    # 先记下删库前的索引状态:此时全局稀疏索引里含联调库的分块
    before = client.get(f"{BASE}/api/admin/stats", headers=admin_h).json()
    before_chunks = before["chunks"]
    before_index = (before["bm25_indexes"] or [{}])[0].get("chunks")

    client.delete(f"{BASE}/api/knowledge-bases/{kb_id}", headers=admin_h)
    print("  已删除联调知识库")
    # 上传的原始文件必须随知识库一起清掉:CASCADE 只删数据库行,
    # 磁盘文件不主动删就会在 uploads/ 里无限堆积
    leftover = sorted(UPLOAD_DIR.glob(f"{doc_id}.*")) if UPLOAD_DIR.is_dir() else []
    check("删除知识库时一并清理了上传文件", not leftover,
          f"残留={[p.name for p in leftover]}")

    # 索引与语义缓存都是全局单例,删库后必须整体失效。如果这里出错,已删知识库的
    # 内容会在 TTL 内继续被引用出来 —— 用户看到的引用指向一个不存在的库,而且
    # 这类 bug 不报错、只答错,所以必须有回归用例守着。
    #
    # 不能只断言「引用里没有已删库」:样本用的就是真实商品文档,别的库很可能有同样
    # 内容,那样断言会因巧合通过。所以这里盯住全局索引本身 —— 删库后首次检索会
    # 触发失效重建,重建后的分块数必须等于删库后的当前值,且少于删库前。
    events = sse_events(client, admin_h, {"conversation_id": conv_id,
                                          "question": "这款手机的电池容量是多少?"})
    refs = next((e["references"] for e in events if e["type"] == "references"), [])
    stale = [r for r in refs if r.get("knowledge_base_name") == kb_name]
    check("删库后引用不再出现已删知识库的内容", not stale,
          f"残留引用={[r.get('filename') for r in stale]}")

    after = client.get(f"{BASE}/api/admin/stats", headers=admin_h).json()
    after_index = (after["bm25_indexes"] or [{}])[0].get("chunks")
    check("删库后全局稀疏索引已失效重建(不含已删分块)",
          after_index == after["chunks"] and (before_index or 0) > after["chunks"],
          f"删前索引={before_index} 删前分块={before_chunks} "
          f"删后索引={after_index} 删后分块={after['chunks']}")

    # 会话也要自己删干净。它挂在 admin 名下,而 clean_test_data.py 默认不清 admin 的
    # 会话(那会把真实对话一起删掉,必须显式加 --conversations),所以留在这里不管
    # 就会每跑一次攒一个「联调改名」。测试脚本应当自己收拾自己的数据。
    client.delete(f"{BASE}/api/conversations/{conv_id}", headers=admin_h)
    check("测试会话已自行清理(不留残渣)",
          client.get(f"{BASE}/api/conversations/{conv_id}", headers=admin_h).status_code == 404)

    print(f"\n{'=' * 46}")
    print(f"结果:{passed} 通过 / {failed} 失败")
    print("=" * 46)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
