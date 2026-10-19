# NL2SQL 智能问数系统 · 评测数据集 Schema 说明

> 本文件是 Golden Set（`evaluation/golden_set.jsonl`）的标注规范与数据背景说明。
> 任何人在新增/修改评测样例前必须先读完本文档。

## 一、业务背景

电商平台"NL2SQL 智能问数系统"场景：店铺经营者用自然语言向系统提问，系统生成 SQL 并在
数据仓库（`dw` 库）上执行后返回答案。数据范围仅限**电商交易数仓**，
平台自身运营数据（用户账号、会话、提问记录，位于 `app_db` 库）**不在问数范围内**。

数据规模（2025-10 快照）：订单 115 笔、客户 20 人、日期覆盖 2025 年 Q1 共 90 天。

## 二、表结构（dw 库，共 5 张表）

星型模型：`fact_order` 事实表 + 4 张维表。

### fact_order（订单事实表）
| 字段 | 类型 | 说明 |
|---|---|---|
| order_id | varchar(30) | 订单号（主键） |
| customer_id | varchar(20) | 客户 ID → dim_customer |
| product_id | varchar(20) | 商品 ID → dim_product |
| date_id | int | 日期键（YYYYMMDD，如 20250101）→ dim_date |
| region_id | varchar(20) | 区域 ID → dim_region |
| order_quantity | int | 购买数量（件） |
| order_amount | float | 订单金额（元） |

### dim_customer（客户维表）
customer_id, customer_name, gender(男/女), member_level(青铜/白银/黄金/铂金)

### dim_product（商品维表）
product_id, product_name, category, brand

品类取值：手机数码 / 家用电器 / 鞋靴 / 服饰 / 食品饮料 / 休闲零食
品牌取值（部分）：苹果 / 三星 / 华为 / 戴森 / 美的 / 耐克 / 阿迪达斯 / 优衣库 / 雀巢 / 蒙牛 / 乐事 / 奥利奥 等

### dim_region（区域维表）
region_id, province（省/市）, region_name（大区）, country

| region_id | province | region_name |
|---|---|---|
| R001 | 广东省 | 华南 |
| R002 | 浙江省 | 华东 |
| R003 | 四川省 | 西南 |
| R004 | 北京市 | 华北 |
| R005 | 上海市 | 华东 |
| R006 | 湖北省 | 华中 |

注意：**"华东"是跨省大区（上海市+浙江省）**，按 region_name 过滤不能只匹配单个省。

### dim_date（日期维表）
date_id(int, YYYYMMDD), year(int), quarter(varchar, 'Q1'~'Q4'), month(int), day(int)

数据仅覆盖 **2025 年 Q1**（90 天）。评测问题不要出现 2024、Q3 等数据范围外的时间。

## 三、指标口径（meta 库登记，共 2 个）

| 指标 | 口径 | relevant_columns |
|---|---|---|
| GMV | 所有订单的成交金额总和 = `SUM(fact_order.order_amount)` | fact_order.order_amount |
| AOV | 所有订单的成交金额平均值 = `AVG(fact_order.order_amount)` | fact_order.order_amount |

> AOV 的 relevant_columns 于 2026-10-14 修正：原错误指向 `order_quantity`（购买数量），
> 与"成交金额平均值"的描述矛盾，已改为 `order_amount` 并重建元知识库。

## 四、gold_sql 标注规范

1. **必须在 dw 库真实执行通过**（新增样例后必须跑 `scripts/export_golden_set.py` 校验）；
2. **不加库名前缀**（写 `FROM fact_order`，不写 `dw.fact_order`）；
3. **聚合列必须 AS 别名**（与生成链路 prompt 的规则一致，便于结果比对）；
4. **单条 SELECT 语句**，禁止注释、禁止多语句；
5. **不得触碰安全网关黑名单**（`conf/app_config.yaml` 的 `sql_guard`）：
   - 关键字黑名单：INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/REPLACE/CALL/MERGE/REVOKE/LOAD/HANDLER/RENAME/SHUTDOWN/KILL/PREPARE/EXECUTE/FLUSH/PURGE/LOCK/UNLOCK；
   - 函数黑名单：SLEEP/BENCHMARK/LOAD_FILE/GET_LOCK/RELEASE_LOCK；
   - 无 LIMIT 的语句执行前会被自动追加 `LIMIT 1000`（default_limit），行数硬上限 1000、执行超时 10s —— **gold_sql 不受影响**，但标注时不要依赖"无 LIMIT"这一行为；
6. **等价判定规则**（评测脚本 EX 指标使用）：
   - 列集合语义可比（列名别名不同可接受，但列数与语义须一致）；
   - 行数相同；
   - 排序无关逐行比对（除非问题明确要求排序，此时按序比对 Top-N）；
   - 数值允许相对误差 1e-6（float 精度）；
   - "两件以上"类中文口语歧义统一取**含边界**口径（>= 2）。

## 五、类别定义（8 类）

| category | 含义 | 示例 |
|---|---|---|
| single_agg | 单表聚合（COUNT/SUM/AVG/MAX/MIN） | "一共成交了多少笔订单" |
| filter | 条件筛选（数值/等值过滤，单表） | "单笔金额超过一千块的订单" |
| group_order | 分组+排序（GROUP BY + ORDER BY） | "每个客户各下了多少笔单" |
| multi_table | fact_order × 维表关联 | "广东省的订单总金额" |
| metric | 指标口径（GMV/AOV，必须走指标） | "总成交总额（GMV）" |
| time_range | 基于 dim_date 的时间范围 | "2025年1月份的销售额" |
| top_n | Top-N 排名 | "消费金额最高的前5个客户" |
| out_of_scope | 越界/无关问题，**期望拒答** | "今天北京的天气怎么样" |

`out_of_scope` 类约定：`gold_sql` 留空字符串，`note` 写明期望拒答的原因；
评测时该类的正确行为是 SSE 返回 `{"type":"rejected","reason":"out_of_scope"}`，
指标为**拒答准确率**（该类中正确拒答占比）与**误拒答率**（其余类被错误拒答占比）。

## 六、扩充指引（20 → 50~100 条）

当前 20 条为种子集，按类别配比扩充：

| 类别 | 种子 | 建议扩充至 |
|---|---|---|
| single_agg | 3 | 8~12 |
| filter | 3 | 8~12（覆盖性别/会员等级/品牌/品类过滤） |
| group_order | 2 | 6~8 |
| multi_table | 3 | 10~12（四张维表各覆盖） |
| metric | 2 | 5~6 |
| time_range | 2 | 6~8（月/季度/日期区间组合） |
| top_n | 2 | 6~8（变 Top3/Top10、按销量/按金额） |
| out_of_scope | 3 | 8~10（天气/闲聊/app_db 概念/无关业务） |

扩充原则：question 必须口语化贴近真实经营者口吻（不写成 SQL 直译）；
每条新增后立即跑导出脚本校验可执行性。
