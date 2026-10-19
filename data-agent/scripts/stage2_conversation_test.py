"""阶段二(会话路由)后端冒烟测试。

覆盖会话接口的增删改查 + 鉴权(401) + 越权(403) + 不存在(404) + 参数校验(422)。
接口：
    GET    /api/conversations                列出当前用户会话(分页)
    POST   /api/conversations                新建会话
    GET    /api/conversations/{id}/messages  获取某会话历史消息(校验归属)
    PATCH  /api/conversations/{id}           重命名
    DELETE /api/conversations/{id}           删除(级联删除消息)

用法：
    1) 先启动后端（在 data-agent 目录）：
           .venv\\Scripts\\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
    2) 另开一个终端，在 data-agent 目录运行本脚本：
           .venv\\Scripts\\python.exe scripts/stage2_conversation_test.py

可选环境变量：
    BASE_URL   后端地址，默认 http://127.0.0.1:8000
    TIMEOUT    单请求超时秒数，默认 15

退出码：全部通过为 0，存在失败为 1（便于接入 CI）。
"""
from __future__ import annotations

import os
import sys
import uuid

import httpx

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = float(os.getenv("TIMEOUT", "15"))

_results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append(bool(ok))
    line = f"{'[PASS]' if ok else '[FAIL]'} {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return bool(ok)


def main() -> int:
    print(f"== 阶段二 会话路由冒烟测试  BASE_URL={BASE_URL} ==\n")
    client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    # 0. 服务可用性
    try:
        r = client.get("/openapi.json")
    except Exception as exc:
        print(f"[FAIL] 无法连接后端 {BASE_URL}: {exc}")
        print("       请先在 data-agent 目录启动后端")
        return 1
    check("后端服务可访问 /openapi.json", r.status_code == 200, f"status={r.status_code}")

    # 每轮用随机用户名，避免残留数据干扰
    user_a = "conv_" + uuid.uuid4().hex[:8]
    user_b = "other_" + uuid.uuid4().hex[:8]
    password = "Test@123456"

    # ---------- 注册并登录用户 A ----------
    r = client.post("/api/auth/register", json={"username": user_a, "password": password})
    check("注册用户A -> 201", r.status_code == 201, f"status={r.status_code}")

    r = client.post("/api/auth/login", json={"username": user_a, "password": password})
    data = r.json() if r.status_code == 200 else {}
    access_a = data.get("access_token", "")
    check("登录用户A -> 200 且返回 token",
          r.status_code == 200 and bool(access_a),
          f"status={r.status_code}")

    auth_a = {"Authorization": f"Bearer {access_a}"}

    # ---------- 鉴权(401) ----------
    r = client.get("/api/conversations")
    check("无 token 访问 /api/conversations -> 401", r.status_code == 401, f"status={r.status_code}")

    r = client.get("/api/conversations", headers={"Authorization": "Bearer fake.token.jwt"})
    check("伪造 token 访问 -> 401", r.status_code == 401, f"status={r.status_code}")

    # ---------- 新建会话 ----------
    r = client.post("/api/conversations", json={"title": "sales-analysis"},
                     headers=auth_a)
    body = r.json() if r.status_code == 201 else {}
    conv_id = body.get("data", {}).get("id") if isinstance(body.get("data"), dict) else None
    check("新建会话(带标题) -> 201 且返回 id",
          r.status_code == 201 and conv_id is not None,
          f"status={r.status_code} conv_id={conv_id}")

    # 验证响应结构 {code,message,data}
    check("响应结构含 code/message/data",
          set(body.keys()) >= {"code", "message", "data"},
          f"keys={list(body.keys())}")
    check("code=0 且 message=ok",
          body.get("code") == 0 and body.get("message") == "ok",
          f"code={body.get('code')} message={body.get('message')}")

    # ---------- 参数校验(422) ----------
    r = client.post("/api/conversations", json={"title": "x" * 300}, headers=auth_a)
    check("标题超长(>255) -> 422", r.status_code == 422, f"status={r.status_code}")

    # ---------- 列出会话 ----------
    r = client.get("/api/conversations", headers=auth_a)
    list_body = r.json() if r.status_code == 200 else {}
    items = list_body.get("data", []) if isinstance(list_body.get("data"), list) else []
    check("列出会话 -> 200 且含刚建的会话",
          r.status_code == 200 and any(i.get("id") == conv_id for i in items),
          f"status={r.status_code} count={len(items)}")

    # 分页参数
    r = client.get("/api/conversations?offset=0&limit=5", headers=auth_a)
    check("分页参数 offset/limit -> 200", r.status_code == 200, f"status={r.status_code}")

    r = client.get("/api/conversations?limit=0", headers=auth_a)
    check("limit=0 非法 -> 422", r.status_code == 422, f"status={r.status_code}")

    # ---------- 获取历史消息(空) ----------
    r = client.get(f"/api/conversations/{conv_id}/messages", headers=auth_a)
    msg_body = r.json() if r.status_code == 200 else {}
    msgs = msg_body.get("data", []) if isinstance(msg_body.get("data"), list) else []
    check("获取历史消息(空) -> 200 且为空列表",
          r.status_code == 200 and len(msgs) == 0,
          f"status={r.status_code} count={len(msgs)}")

    # ---------- 重命名 ----------
    r = client.patch(f"/api/conversations/{conv_id}",
                     json={"title": "renamed-conv"}, headers=auth_a)
    rename_body = r.json() if r.status_code == 200 else {}
    renamed = rename_body.get("data", {}).get("title") if isinstance(rename_body.get("data"), dict) else None
    check("重命名会话 -> 200 且标题已更新",
          r.status_code == 200 and renamed == "renamed-conv",
          f"status={r.status_code} title={renamed}")

    # 重命名参数校验
    r = client.patch(f"/api/conversations/{conv_id}",
                     json={"title": ""}, headers=auth_a)
    check("重命名空标题 -> 422", r.status_code == 422, f"status={r.status_code}")

    # ---------- 越权(403) ----------
    # 注册用户 B
    r = client.post("/api/auth/register", json={"username": user_b, "password": password})
    check("注册用户B -> 201", r.status_code == 201, f"status={r.status_code}")

    r = client.post("/api/auth/login", json={"username": user_b, "password": password})
    data = r.json() if r.status_code == 200 else {}
    access_b = data.get("access_token", "")
    auth_b = {"Authorization": f"Bearer {access_b}"}

    r = client.get(f"/api/conversations/{conv_id}/messages", headers=auth_b)
    check("用户B访问用户A的会话消息 -> 403", r.status_code == 403, f"status={r.status_code}")

    r = client.patch(f"/api/conversations/{conv_id}",
                     json={"title": "hijack"}, headers=auth_b)
    check("用户B重命名用户A的会话 -> 403", r.status_code == 403, f"status={r.status_code}")

    r = client.delete(f"/api/conversations/{conv_id}", headers=auth_b)
    check("用户B删除用户A的会话 -> 403", r.status_code == 403, f"status={r.status_code}")

    # ---------- 不存在(404) ----------
    fake_id = 99999999
    r = client.get(f"/api/conversations/{fake_id}/messages", headers=auth_a)
    check("访问不存在的会话 -> 404", r.status_code == 404, f"status={r.status_code}")

    r = client.patch(f"/api/conversations/{fake_id}",
                     json={"title": "x"}, headers=auth_a)
    check("重命名不存在的会话 -> 404", r.status_code == 404, f"status={r.status_code}")

    r = client.delete(f"/api/conversations/{fake_id}", headers=auth_a)
    check("删除不存在的会话 -> 404", r.status_code == 404, f"status={r.status_code}")

    # ---------- 删除 ----------
    r = client.delete(f"/api/conversations/{conv_id}", headers=auth_a)
    check("删除会话 -> 200", r.status_code == 200, f"status={r.status_code}")

    # 删除后列表不含该会话
    r = client.get("/api/conversations", headers=auth_a)
    list_body = r.json() if r.status_code == 200 else {}
    items = list_body.get("data", []) if isinstance(list_body.get("data"), list) else []
    check("删除后会话列表不再含该会话",
          r.status_code == 200 and not any(i.get("id") == conv_id for i in items),
          f"status={r.status_code} count={len(items)}")

    # 删除后再访问 -> 404
    r = client.get(f"/api/conversations/{conv_id}/messages", headers=auth_a)
    check("删除后再访问该会话 -> 404", r.status_code == 404, f"status={r.status_code}")

    client.close()

    total = len(_results)
    passed = sum(_results)
    print(f"\n== 结果: {passed}/{total} 通过 ==")
    if passed != total:
        print("存在失败项，请对照上方 [FAIL] 逐条排查。")
        return 1
    print("阶段二会话路由接口全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
