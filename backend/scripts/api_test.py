"""接口端到端测试:逐条覆盖毕设需求。

前置:后端已在 127.0.0.1:8000 运行。用法(在 backend 目录下):
    python scripts/api_test.py
"""

from __future__ import annotations

import sys

# Windows 控制台默认 GBK,模型回答里可能带 emoji,直接 print 会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
SAMPLE = Path(__file__).resolve().parent.parent.parent / "sample_data" / "星辰X1智能手机_商品知识库.md"

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"   [{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def sse_events(client: httpx.Client, payload: dict, headers: dict) -> list[dict]:
    """发起流式问答,收集全部 SSE 事件。"""
    events: list[dict] = []
    with client.stream("POST", f"{BASE}/api/chat/stream", json=payload, headers=headers,
                       timeout=180) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def main() -> None:
    client = httpx.Client(timeout=60)

    # ---------- 需求 5:注册 / 登录 / 改密 ----------
    section("需求5: 注册 / 登录 / 修改密码")

    admin_login = client.post(f"{BASE}/api/auth/login",
                              json={"username": "admin", "password": "123456"})
    check("管理员 admin/123456 登录成功", admin_login.status_code == 200,
          f"HTTP {admin_login.status_code}")
    if admin_login.status_code != 200:
        print("   管理员登录失败,后续测试无法继续:", admin_login.text[:300])
        sys.exit(1)
    admin_token = admin_login.json()["access_token"]
    admin_h = {"Authorization": f"Bearer {admin_token}"}
    check("登录返回角色为 admin", admin_login.json()["user"]["role"] == "admin")

    uname = f"student{int(time.time()) % 100000}"
    reg = client.post(f"{BASE}/api/auth/register",
                      json={"username": uname, "password": "test123456"})
    check(f"注册普通用户 {uname}", reg.status_code == 201, f"HTTP {reg.status_code}")
    check("新用户默认角色为 user", reg.json().get("role") == "user")

    dup = client.post(f"{BASE}/api/auth/register",
                      json={"username": uname, "password": "test123456"})
    check("重复用户名被拒绝(409)", dup.status_code == 409, f"HTTP {dup.status_code}")

    user_login = client.post(f"{BASE}/api/auth/login",
                             json={"username": uname, "password": "test123456"})
    check("普通用户登录成功", user_login.status_code == 200)
    user_token = user_login.json()["access_token"]
    user_h = {"Authorization": f"Bearer {user_token}"}

    bad = client.post(f"{BASE}/api/auth/login",
                      json={"username": uname, "password": "wrong-password"})
    check("错误密码被拒绝(401)", bad.status_code == 401)

    me = client.get(f"{BASE}/api/auth/me", headers=user_h)
    check("凭 token 获取当前用户", me.status_code == 200 and me.json()["username"] == uname)

    noauth = client.get(f"{BASE}/api/auth/me")
    check("无 token 访问被拒绝(401)", noauth.status_code == 401)

    # ---------- 需求 1 & 6:知识库管理(仅管理员) ----------
    section("需求1+6: 知识库管理(仅管理员可操作)")

    forbidden = client.post(f"{BASE}/api/knowledge-bases", json={"name": "越权测试库"},
                            headers=user_h)
    check("普通用户创建知识库被拒绝(403)", forbidden.status_code == 403,
          f"HTTP {forbidden.status_code}")

    kb_name = f"星辰X1商品库{int(time.time()) % 10000}"
    kb_resp = client.post(f"{BASE}/api/knowledge-bases",
                          json={"name": kb_name, "description": "毕设演示知识库"},
                          headers=admin_h)
    check("管理员创建知识库", kb_resp.status_code == 201, f"HTTP {kb_resp.status_code}")
    kb_id = kb_resp.json()["id"]

    forbidden_docs = client.get(f"{BASE}/api/knowledge-bases/{kb_id}/documents", headers=user_h)
    check("普通用户查看文档列表被拒绝(403)", forbidden_docs.status_code == 403)

    kb_list_user = client.get(f"{BASE}/api/knowledge-bases", headers=user_h)
    check("普通用户可以浏览知识库列表(用于选择)",
          kb_list_user.status_code == 200 and any(k["id"] == kb_id for k in kb_list_user.json()))

    with SAMPLE.open("rb") as f:
        upload = client.post(
            f"{BASE}/api/knowledge-bases/{kb_id}/documents",
            files={"file": (SAMPLE.name, f, "text/markdown")},
            headers=admin_h,
            timeout=120,
        )
    check("上传文档成功", upload.status_code == 201, f"HTTP {upload.status_code}")
    doc_id = upload.json()["id"]

    bad_ext = client.post(
        f"{BASE}/api/knowledge-bases/{kb_id}/documents",
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=admin_h,
    )
    check("非法扩展名被拒绝(400)", bad_ext.status_code == 400, f"HTTP {bad_ext.status_code}")

    # 后台摄取是异步的,轮询等待
    status_val, chunk_count = "", 0
    for _ in range(60):
        time.sleep(2)
        docs = client.get(f"{BASE}/api/knowledge-bases/{kb_id}/documents", headers=admin_h).json()
        target = next((d for d in docs if d["id"] == doc_id), None)
        if target:
            status_val, chunk_count = target["status"], target["chunk_count"]
            if status_val in ("ready", "failed"):
                break
    check("文档解析并向量化完成", status_val == "ready",
          f"状态={status_val} 分块数={chunk_count}")
    check("产生了分块", chunk_count > 0, f"{chunk_count} 个分块")

    chunks = client.get(
        f"{BASE}/api/knowledge-bases/{kb_id}/documents/{doc_id}/chunks",
        params={"page": 1, "page_size": 3}, headers=admin_h,
    )
    check("分块预览接口可用", chunks.status_code == 200 and chunks.json()["total"] == chunk_count,
          f"共 {chunks.json().get('total')} 块")
    if chunks.status_code == 200 and chunks.json()["items"]:
        first = chunks.json()["items"][0]["content"]
        check("分块带有标题上下文前缀", first.startswith("【"), first[:40].replace("\n", " "))

    # ---------- 需求 2:问答 + 引用 ----------
    section("需求2: 知识库问答 + 引用片段")

    # 不再传知识库:检索范围是全部库,由打分排序决定引用谁
    events = sse_events(client, {
        "question": "这个手机多少钱?支持七天无理由退货吗?",
    }, admin_h)

    types = [e["type"] for e in events]
    check("SSE 返回 conversation 事件", "conversation" in types)
    check("SSE 返回 references 事件(引用片段)", "references" in types)

    answer = "".join(e["content"] for e in events if e["type"] == "token")
    refs = next((e["references"] for e in events if e["type"] == "references"), [])
    done = next((e for e in events if e["type"] == "done"), {})

    check("回答包含价格信息", "3999" in answer or "4299" in answer, answer[:60].replace("\n", " "))
    check("回答包含退货政策", "七天无理由" in answer or "7天" in answer)
    check("回答中带引用标注 [n]", "[1]" in answer or "[2]" in answer)
    check("引用片段有来源文件名", bool(refs) and all(r["filename"] for r in refs),
          f"{len(refs)} 条引用")
    check("引用片段标注来源知识库(跨库检索后可溯源)",
          bool(refs) and all(r.get("knowledge_base_name") for r in refs),
          f"来源库: {sorted({r.get('knowledge_base_name') for r in refs})}")
    check("引用片段带重排分数", bool(refs) and refs[0].get("rerank_score") is not None,
          f"首条重排分={refs[0].get('rerank_score') if refs else None}")
    check("done 事件含耗时与用量", done.get("latency_ms", 0) > 0,
          f"耗时 {done.get('latency_ms')}ms 用量 {done.get('usage')}")

    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation")

    # ---------- 需求 3 & 4:多会话 + 历史持久化 ----------
    section("需求3+4: 多用户多会话 + 历史持久化")

    ev2 = sse_events(client, {"conversation_id": conv_id, "question": "那保修多久?"}, admin_h)
    rewrite = next((e["query"] for e in ev2 if e["type"] == "rewrite"), None)
    check("追问触发历史感知改写", rewrite is not None, f"改写为: {rewrite}")
    ans2 = "".join(e["content"] for e in ev2 if e["type"] == "token")
    check("追问得到保修相关回答", "保修" in ans2 or "延保" in ans2, ans2[:50].replace("\n", " "))

    detail = client.get(f"{BASE}/api/conversations/{conv_id}", headers=admin_h).json()
    check("会话历史包含 4 条消息(2问2答)", len(detail["messages"]) == 4,
          f"实际 {len(detail['messages'])} 条")
    assistant_msgs = [m for m in detail["messages"] if m["role"] == "assistant"]
    check("助手消息落库时保存了引用快照",
          bool(assistant_msgs) and bool(assistant_msgs[0].get("references")),
          f"{len(assistant_msgs[0].get('references') or [])} 条")

    # 用新会话验证多会话
    ev3 = sse_events(client, {"question": "支持IP68防水吗?"}, admin_h)
    conv2_id = next(e["conversation_id"] for e in ev3 if e["type"] == "conversation")
    check("可以创建多个独立会话", conv2_id != conv_id, f"会话 {conv_id} 与 {conv2_id}")

    convs_page = client.get(f"{BASE}/api/conversations", headers=admin_h).json()
    convs = convs_page["items"]
    check("会话列表返回多个会话", len(convs) >= 2, f"{len(convs)} 个会话")
    check("会话列表带 total(前端分页依赖)", convs_page["total"] >= len(convs),
          f"total={convs_page['total']}")
    check("会话标题自动生成", all(c["title"] and c["title"] != "新对话" for c in convs),
          f"标题: {[c['title'] for c in convs[:3]]}")

    # 关键安全点:普通用户不能读到管理员的会话
    other = client.get(f"{BASE}/api/conversations/{conv_id}", headers=user_h)
    check("普通用户无法读取他人会话(404)", other.status_code == 404,
          f"HTTP {other.status_code}")

    # 普通用户自己的会话是隔离的
    ev4 = sse_events(client, {"question": "手机电池容量多大?"}, user_h)
    user_conv = next(e["conversation_id"] for e in ev4 if e["type"] == "conversation")
    user_convs = client.get(f"{BASE}/api/conversations", headers=user_h).json()["items"]
    check("普通用户只看到自己的会话",
          all(c["id"] != conv_id for c in user_convs) and any(c["id"] == user_conv for c in user_convs),
          f"该用户 {len(user_convs)} 个会话")

    # 模拟"不同时段重新登录":用新 token 读同一会话
    relogin = client.post(f"{BASE}/api/auth/login",
                          json={"username": "admin", "password": "123456"}).json()
    relogin_h = {"Authorization": f"Bearer {relogin['access_token']}"}
    again = client.get(f"{BASE}/api/conversations/{conv_id}", headers=relogin_h)
    check("重新登录后仍能找回历史对话",
          again.status_code == 200 and len(again.json()["messages"]) == 4,
          f"{len(again.json()['messages'])} 条消息")

    # ---------- 需求 7:性能优化 ----------
    section("需求7: 性能优化验证")

    t0 = time.perf_counter()
    client.get(f"{BASE}/api/conversations/{conv_id}", headers=admin_h)
    t_hist = (time.perf_counter() - t0) * 1000

    ev5 = sse_events(client, {"question": "这个手机多少钱?支持七天无理由退货吗?"}, admin_h)
    done5 = next((e for e in ev5 if e["type"] == "done"), {})
    check("语义缓存命中重复提问", done5.get("cache_hit") is True,
          f"cache_hit={done5.get('cache_hit')} 耗时 {done5.get('latency_ms')}ms")
    check("缓存命中显著快于完整链路", done5.get("latency_ms", 9999) < 500,
          f"{done5.get('latency_ms')}ms vs 首次 {done.get('latency_ms')}ms")

    stats = client.get(f"{BASE}/api/admin/stats", headers=admin_h)
    check("管理仪表盘可访问", stats.status_code == 200)
    s = stats.json()
    check("仪表盘返回完整统计", all(k in s for k in
          ("users", "knowledge_bases", "documents", "chunks", "conversations", "messages")),
          f"用户 {s.get('users')} 文档 {s.get('documents')} 分块 {s.get('chunks')} 会话 {s.get('conversations')}")
    check("缓存状态被统计", s.get("cache", {}).get("enabled") is True,
          f"条目 {s.get('cache', {}).get('entries')} 个")

    st_user = client.get(f"{BASE}/api/admin/stats", headers=user_h)
    check("普通用户访问仪表盘被拒绝(403)", st_user.status_code == 403)

    # ---------- 需求 5 续:改密 ----------
    section("需求5续: 修改密码")

    wrong_old = client.post(f"{BASE}/api/auth/change-password",
                            json={"old_password": "not-my-password", "new_password": "newpass123"},
                            headers=user_h)
    check("旧密码错误时拒绝改密(400)", wrong_old.status_code == 400)

    ok_change = client.post(f"{BASE}/api/auth/change-password",
                            json={"old_password": "test123456", "new_password": "newpass123"},
                            headers=user_h)
    check("正确旧密码可改密", ok_change.status_code == 200)

    old_login = client.post(f"{BASE}/api/auth/login",
                            json={"username": uname, "password": "test123456"})
    check("改密后旧密码失效(401)", old_login.status_code == 401)

    new_login = client.post(f"{BASE}/api/auth/login",
                            json={"username": uname, "password": "newpass123"})
    check("改密后新密码可登录", new_login.status_code == 200)

    # ---------- 清理 ----------
    section("清理测试数据")
    d1 = client.delete(f"{BASE}/api/conversations/{conv_id}", headers=admin_h)
    d2 = client.delete(f"{BASE}/api/conversations/{conv2_id}", headers=admin_h)
    d3 = client.delete(f"{BASE}/api/conversations/{user_conv}", headers=user_h)
    check("会话删除成功", d1.status_code == 204 and d2.status_code == 204 and d3.status_code == 204)
    gone = client.get(f"{BASE}/api/conversations/{conv_id}", headers=admin_h)
    check("删除后会话不可访问(404)", gone.status_code == 404)

    kb_del = client.delete(f"{BASE}/api/knowledge-bases/{kb_id}", headers=admin_h)
    check("知识库删除成功", kb_del.status_code == 204)

    client.close()

    # ---------- 汇总 ----------
    print()
    print("=" * 72)
    print(f"测试汇总: {len(passed)} 项通过,{len(failed)} 项失败")
    print("=" * 72)
    if failed:
        for f in failed:
            print(f"   FAILED: {f}")
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
