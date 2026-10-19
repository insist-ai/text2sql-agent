import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.conf.app_config import app_config
from app.core.sql_guard import SqlGuardError, SqlGuardPolicy, check_sql


class DWMySQLRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
        # 仓储层安全策略（执行前兜底，防止节点层被绕过）
        self._guard_policy = SqlGuardPolicy.from_config(app_config.sql_guard)

    async def get_column_types(self, table_name: str) -> dict[str, str]:
        sql = f"show columns from {table_name}"
        result = await self.session.execute(text(sql))
        return {row.Field: row.Type for row in result.fetchall()}

    async def get_column_values(self, table_name: str, column_name: str, limit: int):
        sql = f"select distinct {column_name} from {table_name} limit {limit}"
        result = await self.session.execute(text(sql))
        return result.scalars().fetchall()

    async def get_db_info(self):
        sql = "select version()"
        result = await self.session.execute(text(sql))
        version = result.scalar()

        dialect = self.session.bind.dialect.name
        return {"dialect": dialect, "version": version}

    async def validate_sql(self, sql: str):
        sql = f"explain {sql}"
        await self.session.execute(text(sql))

    async def run_sql(self, sql: str, policy: SqlGuardPolicy | None = None) -> list[dict]:
        """执行查询。执行前由安全网关兜底校验（只读白名单/单语句/无注释/强制 LIMIT），
        并施加执行超时、返回行数上限、结果集大小上限。"""
        policy = policy or self._guard_policy

        # 兜底校验：任何绕过节点层直接调用仓储的路径都会在这里被拦截
        safe_sql = check_sql(sql, policy)

        try:
            result = await asyncio.wait_for(
                self.session.execute(text(safe_sql)),
                timeout=policy.execution_timeout,
            )
        except asyncio.TimeoutError:
            raise SqlGuardError(
                "查询执行超时，已被安全策略中止。请增加过滤条件、缩小时间范围或减少返回数据量。",
                code="timeout",
            )

        rows = [dict(row) for row in result.mappings().fetchall()]

        # 返回行数硬上限（LIMIT 已收敛，双保险）
        if len(rows) > policy.max_rows:
            rows = rows[: policy.max_rows]

        # 结果集大小硬上限
        result_bytes = len(json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8"))
        if result_bytes > policy.max_result_bytes:
            raise SqlGuardError(
                "查询结果集过大，已被安全策略拒绝。请增加过滤条件或减少返回字段。",
                code="result_too_large",
            )
        return rows
