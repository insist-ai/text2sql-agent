"""阶段一(用户与权限)后端冒烟测试。

覆盖：注册 / 登录 / 刷新 token / 鉴权(401) / RBAC(403) / 参数校验(422) / 防用户枚举。

用法：
    1) 先启动后端（在 data-agent 目录）：
           uv run fastapi dev main.py
    2) 另开一个终端，在 data-agent 目录运行本脚本：
           uv run python scripts/stage1_smoke_test.py

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
    print(f"== 阶段一 冒烟测试  BASE_URL={BASE_URL} ==\n")
    client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    # 0. 服务可用性
    try:
        r = client.get("/openapi.json")
    except Exception as exc:
        print(f"[FAIL] 无法连接后端 {BASE_URL}: {exc}")
        print("       请先在 data-agent 目录执行: uv run fastapi dev main.py")
        return 1
    check("后端服务可访问 /openapi.json", r.status_code == 200, f"status={r.status_code}")

    # 每轮用随机用户名，避免残留数据干扰
    username = "test_" + uuid.uuid4().hex[:8]
    password = "Test@123456"

    # ---------- 注册 ----------
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    check("注册新用户 -> 201", r.status_code == 201,
          f"status={r.status_code} body={r.text[:100]}")

    r = client.post("/api/auth/register", json={"username": username, "password": password})
    check("重复用户名注册 -> 409", r.status_code == 409, f"status={r.status_code}")

    r = client.post("/api/auth/register", json={"username": "ab", "password": password})
    check("用户名过短 -> 422", r.status_code == 422, f"status={r.status_code}")

    r = client.post("/api/auth/register", json={"username": "bad name!", "password": password})
    check("用户名含非法字符 -> 422", r.status_code == 422, f"status={r.status_code}")

    r = client.post("/api/auth/register", json={"username": "valid_name_1", "password": "123"})
    check("密码过短 -> 422", r.status_code == 422, f"status={r.status_code}")

    # ---------- 登录 ----------
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    data = r.json() if r.status_code == 200 else {}
    access = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    check("登录成功 -> 200 且返回 access+refresh",
          r.status_code == 200 and bool(access) and bool(refresh),
          f"status={r.status_code}")
    check("token_type 为 bearer", data.get("token_type") == "bearer",
          f"token_type={data.get('token_type')}")

    r = client.post("/api/auth/login", json={"username": username, "password": "WrongPass123"})
    check("密码错误 -> 401", r.status_code == 401, f"status={r.status_code}")

    r = client.post("/api/auth/login", json={"username": "no_such_user_xyz", "password": password})
    check("用户不存在 -> 401", r.status_code == 401, f"status={r.status_code}")
    check("防用户枚举：两种失败提示一致",
          r.json().get("detail") == "用户名或密码错误",
          f"detail={r.json().get('detail')}")
    check("登录响应体不返回密码哈希",
          "password_hash" not in (data or {}),
          f"keys={list((data or {}).keys())}")

    # ---------- 鉴权(401) ----------
    r = client.get("/api/users/me")
    check("无 token 访问 /api/users/me -> 401", r.status_code == 401, f"status={r.status_code}")
    check("401 响应带 WWW-Authenticate: Bearer",
          r.headers.get("www-authenticate") == "Bearer",
          f"header={r.headers.get('www-authenticate')}")

    r = client.get("/api/users/me", headers={"Authorization": "Token abc"})
    check("认证头不是 Bearer 前缀 -> 401", r.status_code == 401, f"status={r.status_code}")

    r = client.get("/api/users/me", headers={"Authorization": "Bearer abc.def.ghi"})
    check("伪造/非法 token -> 401", r.status_code == 401, f"status={r.status_code}")

    r = client.get("/api/users/me", headers={"Authorization": "Bearer " + (access[:-4] + "aaaa")})
    check("篡改 token 末位 -> 401", r.status_code == 401, f"status={r.status_code}")

    # ---------- 正常访问 ----------
    auth = {"Authorization": f"Bearer {access}"}
    r = client.get("/api/users/me", headers=auth)
    check("带 access token 访问 -> 200", r.status_code == 200, f"status={r.status_code}")
    body = r.json() if r.status_code == 200 else {}
    check("返回用户名与注册一致", body.get("username") == username,
          f"username={body.get('username')}")
    check("默认角色为 user", body.get("role") == "user", f"role={body.get('role')}")

    # ---------- RBAC(403) ----------
    r = client.get("/api/admin/audit-logs", headers=auth)
    check("普通用户访问 admin 接口 -> 403", r.status_code == 403, f"status={r.status_code}")

    # ---------- 刷新 ----------
    r = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    new_access = r.json().get("access_token", "") if r.status_code == 200 else ""
    check("refresh 换新 access -> 200", r.status_code == 200 and bool(new_access),
          f"status={r.status_code}")

    r = client.get("/api/users/me", headers={"Authorization": f"Bearer {new_access}"})
    check("新 access 可用", r.status_code == 200, f"status={r.status_code}")

    r = client.post("/api/auth/refresh", json={"refresh_token": access})
    check("用 access 调 refresh -> 401(类型不匹配)", r.status_code == 401, f"status={r.status_code}")

    r = client.post("/api/auth/refresh", json={"refresh_token": "not-a-jwt"})
    check("非法 refresh token -> 401", r.status_code == 401, f"status={r.status_code}")

    client.close()

    total = len(_results)
    passed = sum(_results)
    print(f"\n== 结果: {passed}/{total} 通过 ==")
    if passed != total:
        print("存在失败项，请对照上方 [FAIL] 逐条排查。")
        return 1
    print("阶段一后端接口全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
