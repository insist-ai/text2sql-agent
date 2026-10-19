# NL2SQL 智能问数系统 · Golden Set 评测报告

| 指标 | 值 |
|---|---|
| 样本总数 | 50（业务类 40 / 越界类 10） |
| EX 执行准确率 | 97.5% |
| EM 精确匹配率 | 10.0% |
| SQL 有效率 | 100.0% |
| 平均尝试次数（自省闭环） | 2.0 |
| 平均耗时 | 45885 ms |
| 拒答准确率（out_of_scope） | 90.0% |
| 误拒答率（业务类被拒） | 0.0% |
| 平均输入字符（估算口径） | 30 |
| 平均输出字符（估算口径） | 126 |
| 表召回率 Recall@10 | 100.0% |
| 字段召回率 Recall@10 | 85.8% |
| 指标召回率 Recall@10 | 100.0% |
| 组件级综合 F1 | 90.4 |

> token 口径说明：LLM 实例未暴露 usage 回调，采用字符数估算（输入=问题+改写后问题，输出=生成 SQL），非真实 token。
> EM 归一化说明：统一小写/压缩空白/去末尾分号，并剥掉安全网关自动追加的尾部 LIMIT n。
> 平均尝试说明：sql_attempts 含首次生成，故最小值为 1（生成即成功且直接执行为 2：生成+执行各记一条）。
> EX 多参考答案说明：部分问题存在多种等价正确写法（主要是输出列多寡），gold 以 `gold_sqls` 列举变体，任一变体执行结果等价即判 EX 通过，避免把「逻辑全对、投影列不同」误判为失败。
> 组件级 F1 说明：把 SQL 拆为 SELECT / 聚合 / WHERE / GROUP BY / ORDER BY / JOIN / LIMIT 七个子句，逐子句做集合 F1（两边皆空记满分），用于定位错误发生在哪个环节，分辨率高于端到端 EX。

## 组件级指标（子句 F1，%）

| 组件 | F1 | 说明 |
|---|---|---|
| SELECT 投影 | 84.2 | 输出列选择 |
| 聚合函数 | 95.0 | SUM/AVG/COUNT/MIN/MAX 及参数列 |
| WHERE 过滤 | 88.3 | 过滤条件（列+操作符+值） |
| GROUP BY | 95.0 | 分组维度 |
| ORDER BY | 85.0 | 排序字段与方向 |
| JOIN 关联 | 85.0 | 表关联条件 |
| LIMIT | 100.0 | 返回条数 |

## 分类别指标

| 类别 | 样本 | EX | EM | 有效率 | 平均尝试 | 拒答准确率 |
|---|---|---|---|---|---|---|
| filter | 6 | 100.0 | 0.0 | 100.0 | 2.0 | - |
| group_order | 6 | 100.0 | 0.0 | 100.0 | 2.0 | - |
| metric | 5 | 80.0 | 20.0 | 100.0 | 2.0 | - |
| multi_table | 8 | 100.0 | 12.5 | 100.0 | 2.0 | - |
| out_of_scope | 10 | - | - | - | - | 90.0 |
| single_agg | 5 | 100.0 | 40.0 | 100.0 | 2.0 | - |
| time_range | 5 | 100.0 | 0.0 | 100.0 | 2.0 | - |
| top_n | 5 | 100.0 | 0.0 | 100.0 | 2.0 | - |

## 失败样例（bad case 候选）

### q036（metric）
- 问题：2025年一季度每个月的GMV分别是多少？
- 拒答：False
- gold_sql：`SELECT dd.month, SUM(fo.order_amount) AS GMV FROM fact_order fo JOIN dim_date dd ON fo.date_id = dd.date_id WHERE dd.year = 2025 AND dd.quarter = 'Q1' GROUP BY dd.month ORDER BY dd.month`
- pred_sql：`SELECT CONCAT(SUBSTRING(CAST(f.date_id AS CHAR), 1, 4), '-', SUBSTRING(CAST(f.date_id AS CHAR), 5, 2)) AS month, SUM(f.order_amount) AS gmv FROM fact_order f JOIN dim_date d ON f.date_id = d.date_id WHERE d.year = 2025 AND d.quarter = 'Q1' GROUP BY CONCAT(SUBSTRING(CAST(f.date_id AS CHAR), 1, 4), '-', SUBSTRING(CAST(f.date_id AS CHAR), 5, 2)) ORDER BY month LIMIT 1000`
- EX=False EM=False 尝试=2 耗时=55533ms
- 组件 F1：select=0.5 agg=1.0 where=1.0 group=0.0 order=0.0 join=1.0 limit=1.0
- 错误：结果不等价: gold 3 行 / pred 3 行

### q046（out_of_scope）
- 问题：预测一下下个月能卖多少钱
- 拒答：False
- gold_sql：`(期望拒答)`
- pred_sql：`SELECT ROUND(AVG(monthly_sales), 2) AS next_month_sales_forecast
FROM (
    SELECT LEFT(CAST(date_id AS CHAR), 6) AS month_id,
           SUM(order_amount) AS monthly_sales
    FROM fact_order
    GROUP BY LEFT(CAST(date_id AS CHAR), 6)
) AS monthly_data LIMIT 1000`
- EX=False EM=False 尝试=2 耗时=51788ms
- 组件 F1：select=0.0 agg=0.0 where=0.0 group=0.0 order=0.0 join=0.0 limit=0.0
- 错误：（无）
