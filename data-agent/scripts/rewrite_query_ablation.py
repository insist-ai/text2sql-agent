"""查询改写(rewrite_query)消融实验脚本 —— 论文消融素材。

实验设计：
  A 组(有历史/经改写)：同一会话先问 Q1="统计每个省份的销售总额"，再问 Q2="那华东呢"。
      rewrite_query 节点结合 Q1 将 Q2 改写为语义完整的问题(预期如"统计华东地区的销售总额")。
  B 组(无历史/未改写)：全新用户+全新会话直接问 "那华东呢"。
      无历史 → 跳过改写，原问题直接进入下游。
对比 A-Q2 与 B 的改写结果与最终 SQL 差异。

用法(后端已启动时)：
    d:/Work-Data-Agent/data-agent/.venv/Scripts/python.exe scripts/rewrite_query_ablation.py
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import random
import string
import sys
from pathlib import Path

import asyncmy
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BASE_URL = "http://127.0.0.1:8000"
DB_CONF = dict(host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3307")),
               user=os.getenv("DB_USER", "atguigu"), password=os.getenv("DB_PASSWORD", "please-set-env-DB_PASSWORD"),
               db=os.getenv("DB_NAME", "app_db"))
MAX_ROUNDS = 5  # LLM 网关偶发 500，整轮重试上限
# 注：PASSWORD 为测试用户的登录口令（不是基础设施密钥），保留硬编码以便复跑消融实验
PASSWORD = "Test@123456"
TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def rand_username() -> str:
    return "abl_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


async def register_login(client: httpx.AsyncClient) -> str:
    """注册并登录，返回 access_token。"""
    username = rand_username()
    r = await client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    if r.status_code != 201:
        raise RuntimeError(f"注册失败 {r.status_code}: {r.text[:200]}")
    r = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    if r.status_code != 200:
        raise RuntimeError(f"登录失败 {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


async def ask(client: httpx.AsyncClient, token: str, query: str, conversation_id: int | None = None) -> dict:
    """发起一次问数(SSE)，返回 {meta, progress, result, error}。"""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"query": query}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    meta, progress, result, error = None, [], None, None
    async with client.stream("POST", "/api/query", json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "ignore")
            raise RuntimeError(f"/api/query HTTP {resp.status_code}: {body[:200]}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            evt = json.loads(line[5:].strip())
            t = evt.get("type")
            if t == "meta":
                meta = evt.get("conversation_id")
            elif t == "progress":
                progress.append(evt)
            elif t == "result":
                result = evt.get("data")
            elif t == "error":
                error = evt.get("message")
    return {"meta": meta, "progress": progress, "result": result, "error": error}


def rewritten_of(out: dict) -> str | None:
    """从 progress 帧中提取查询改写结果。"""
    for evt in out["progress"]:
        if evt.get("step") == "查询改写" and evt.get("status") == "success":
            return evt.get("rewritten_query")
    return None


async def last_assistant_sql(conversation_id: int) -> str | None:
    """从数据库取该会话最近一条 assistant 消息的 sql_text。"""
    conn = await asyncmy.connect(**DB_CONF)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT sql_text FROM message "
                "WHERE conversation_id=%s AND role='assistant' "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else None
    finally:
        await conn.ensure_closed()


async def run_round(client: httpx.AsyncClient) -> dict:
    """跑一轮完整实验：A 组两轮对话 + B 组独立提问。"""
    token_a = await register_login(client)

    # A-Q1：不传 conversation_id 自动建会话
    q1 = await ask(client, token_a, "统计每个省份的销售总额")
    if q1["error"] is not None or q1["meta"] is None:
        raise RuntimeError(f"A-Q1 失败: {q1['error']}")
    cid = q1["meta"]

    # A-Q2：同会话省略式追问（关键观察点）
    q2 = await ask(client, token_a, "那华东呢", cid)
    if q2["error"] is not None:
        raise RuntimeError(f"A-Q2 失败: {q2['error']}")

    # B：全新用户/全新会话直接问同一省略式问题
    token_b = await register_login(client)
    qb = await ask(client, token_b, "那华东呢")
    # B 图执行失败不整轮重试：无历史条件下失败本身是合理的实验结果

    sql_a2 = await last_assistant_sql(cid)
    sql_b = None
    if qb["error"] is None and qb["meta"] is not None:
        sql_b = await last_assistant_sql(qb["meta"])
    return {"cid": cid, "q1": q1, "q2": q2, "qb": qb, "sql_a2": sql_a2, "sql_b": sql_b}


async def main() -> int:
    print(f"== 查询改写消融实验  BASE_URL={BASE_URL} ==")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
        out = None
        for attempt in range(1, MAX_ROUNDS + 1):
            print(f"\n---- 第 {attempt}/{MAX_ROUNDS} 轮 ----")
            try:
                out = await run_round(client)
                break
            except Exception as exc:
                print(f"[RETRY] 本轮失败({exc})，换新用户重试…")
        if out is None:
            print("\n[FAIL] 连续 5 轮均失败(多为 LLM 网关 500)，请稍后重试。")
            return 1

    # ---------- 实验结果 ----------
    rw1, rw2, rwb = rewritten_of(out["q1"]), rewritten_of(out["q2"]), rewritten_of(out["qb"])
    print("\n========== 实验结果 ==========")
    print(f"A 组会话 conversation_id = {out['cid']}")
    print(f"\n[A-Q1] 原问题  : 统计每个省份的销售总额")
    print(f"[A-Q1] 改写结果: {rw1}   (无历史，应原样返回)")
    print(f"\n[A-Q2] 原问题  : 那华东呢")
    print(f"[A-Q2] 改写结果: {rw2}   (应消解指代为完整问题)")
    print(f"[A-Q2] 最终 SQL:\n{out['sql_a2'] or '无(未落库 assistant 消息)'}")

    b_ok = out["qb"]["error"] is None
    print(f"\n[B]    原问题  : 那华东呢 (无历史)")
    print(f"[B]    改写结果: {rwb}   (无历史，应原样返回)")
    print(f"[B]    图执行  : {'成功' if b_ok else '失败(' + str(out['qb']['error']) + ')'}")
    print(f"[B]    最终 SQL:\n{out['sql_b'] or '无(图执行失败，未落库 assistant 消息)'}")

    # ---------- SQL 对比 ----------
    print("\n========== SQL 对比 (B 未改写 -> A 改写后) ==========")
    if out["sql_a2"] and out["sql_b"]:
        diff = "\n".join(difflib.unified_diff(
            out["sql_b"].splitlines(), out["sql_a2"].splitlines(),
            fromfile="B(无历史/未改写)", tofile="A(有历史/改写后)", lineterm=""))
        print(diff if diff else "(两 SQL 完全一致)")
    else:
        print(f"A SQL: {out['sql_a2']!r}  B SQL: {out['sql_b']!r}")

    # ---------- 自动判定 ----------
    checks = [
        ("A-Q1 无历史时改写结果=原问题", rw1 == "统计每个省份的销售总额"),
        ("A-Q2 改写结果非空且≠原问题(发生消解)", bool(rw2) and rw2 != "那华东呢"),
        ("B 无历史时改写结果=原问题", rwb == "那华东呢"),
        ("A-Q2 落库 assistant 消息含 SQL", bool(out["sql_a2"])),
    ]
    print("\n========== 验收判定 ==========")
    ok = True
    for name, passed in checks:
        print(f"{'[PASS]' if passed else '[FAIL]'} {name}")
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
