from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy import select, text
from app.entities.column_info import ColumnInfo
from app.entities.column_metric import ColumnMetric
from app.entities.metric_info import MetricInfo
from app.entities.table_info import TableInfo
from app.models.column_info import ColumnInfoMySQL
from app.models.column_metric import ColumnMetricMySQL
from app.models.metric_info import MetricInfoMySQL
from app.models.table_info import TableInfoMySQL
from app.repositories.mysql.meta.mappers.column_info_mapper import ColumnInfoMapper
from app.repositories.mysql.meta.mappers.table_info_mapper import TableInfoMapper


class MetaMySQLRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _upsert(self, model, entities):
        """幂等写入：主键存在则更新（非主键列），不存在则插入。

        对于无任何非主键列的表（如 column_metric 复合主键表），
        用主键自身做一次 no-op 更新，等价于 INSERT IGNORE 但不产生 Duplicate entry 提示。
        """
        if not entities:
            return
        rows = [asdict(entity) for entity in entities]
        stmt = mysql_insert(model).values(rows)
        non_pk_cols = [
            col for col in model.__table__.columns if not col.primary_key
        ]
        if non_pk_cols:
            update_cols = {col.name: stmt.inserted[col.name] for col in non_pk_cols}
        else:
            # 纯主键表（如 column_metric 复合主键）：用主键自身做一次 no-op 更新，
            # 等价于 INSERT IGNORE，但不会产生 "Duplicate entry" 提示。
            first_pk = next(col for col in model.__table__.columns if col.primary_key)
            update_cols = {first_pk.name: stmt.inserted[first_pk.name]}
        stmt = stmt.on_duplicate_key_update(**update_cols)
        await self.session.execute(stmt)

    async def save_table_infos(self, table_infos: list[TableInfo]):
        await self._upsert(TableInfoMySQL, table_infos)

    async def save_column_infos(self, columns_info: list[ColumnInfo]):
        await self._upsert(ColumnInfoMySQL, columns_info)

    async def save_metric_infos(self, metric_infos: list[MetricInfo]):
        await self._upsert(MetricInfoMySQL, metric_infos)

    async def save_column_metrics(self, column_metrics: list[ColumnMetric]):
        await self._upsert(ColumnMetricMySQL, column_metrics)

    async def get_column_info_by_id(self, id: str) -> ColumnInfo | None:
        column_info: ColumnInfoMySQL | None = await self.session.get(ColumnInfoMySQL, id)
        if column_info:
            return ColumnInfoMapper.to_entity(column_info)
        else:
            return None
    async def get_table_info_by_id(self, id: str) -> TableInfo | None:
        table_info: TableInfoMySQL | None = await self.session.get(TableInfoMySQL, id)
        if table_info:
            return TableInfoMapper.to_entity(table_info)
        else:
            return None

    async def get_key_columns_by_table_id(self,table_id: str) ->list[ColumnInfo]:
        sql = "select * from column_info where table_id = :table_id and role in ('prinmary_key','foreign_key')"
        result = await self.session.execute(text(sql),{"table_id": table_id})
        return [ColumnInfo(**dict(row)) for row in result.mappings().fetchall()]

    async def list_column_infos(self) -> list[ColumnInfo]:
        """返回元数据库全部字段信息（供关键词索引构建使用）。"""
        result = await self.session.execute(select(ColumnInfoMySQL))
        return [ColumnInfoMapper.to_entity(model) for model in result.scalars().all()]

    async def list_metric_infos(self) -> list[MetricInfo]:
        """返回元数据库全部指标信息（供关键词索引构建使用）。"""
        result = await self.session.execute(select(MetricInfoMySQL))
        return [
            MetricInfo(
                id=model.id,
                name=model.name,
                description=model.description,
                relevant_columns=model.relevant_columns or [],
                alias=model.alias or [],
            )
            for model in result.scalars().all()
        ]