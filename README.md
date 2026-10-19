# NL2SQL 智能问数系统

> 用中文口语提问，自动生成 SQL、在数据仓库上执行并给出答案的 **Text-to-SQL 智能体**。
> 后端 FastAPI + LangGraph 13 节点状态图，前端 Vue3，全栈 Docker Compose 一键启动。

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.138-green)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-9cf)
![Vue](https://img.shields.io/badge/Vue-3.5-brightgreen)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ed)

---

## 它做什么

```
👤 华东地区一共卖出了多少件商品？
🤖 改写问题 → 检索数仓元数据 → 命中字段/指标 → 生成 SQL → 安全校验 → 执行
    SELECT SUM(order_quantity) AS total_quantity
    FROM fact_order fo JOIN dim_region dr ON fo.region_id = dr.region_id
    WHERE dr.region_name = '华东'
   📊 total_quantity: 235

👤 那华南呢？              ← 指代消解，自动补全为完整问题
👤 预测下个月能卖多少钱     ← 超出数据范围，明确拒答而不是编造 SQL
```

三个核心设计，对应三个真实难点：

| 难点 | 做法 |
|---|---|
| 口语问法 ≠ 库表字段名 | 向量 + 关键词**双路召回 + RRF 融合**检索元数据，再注入指标口径与 few-shot |
| LLM 生成的 SQL 经常跑不通 | **自省纠错闭环**：校验 / 执行报错回喂模型重写，最多 3 轮 |
| 问了数仓没有的东西，模型会编 SQL | **数据范围边界判定**：输出 `NO_ANSWER` 标记即走拒答分支，图内提前终止 |

## 关键指标（自建 50 条分层评测集）

| 指标 | 结果 |
|---|---|
| EX 执行准确率 | **97.5%** |
| SQL 有效率 | **100%** |
| 拒答准确率 / 误拒答率 | **90% / 0%** |
| 表 / 字段 / 指标 召回率 @10 | 100% / 85.8% / 100% |
| 组件级综合 F1（7 子句） | 90.4 |
| 多轮改写专项（开启 vs 关闭） | **87.5% vs 37.5%** |

> 当前为免费网关演练数据，正式结论需切 DeepSeek 官方通道重跑（见 `data-agent/docs/LLM通道切换指南.md`）。

## 快速开始

```bash
# 0) 首次需单独下载 Embedding 权重（1.3 GB，不在仓库内）
python -m modelscope.cli.cli download \
    --model BAAI/bge-large-zh-v1.5 \
    --local_dir data-agent/docker/embedding/bge-large-zh-v1.5

# 1) 一条命令起全栈
cd data-agent/docker
cp .env.example .env && docker compose up -d --build
```

浏览器打开 http://localhost:8080（API 文档 http://localhost:8000/docs）。
TEI 在 CPU 下加载模型约需 5~6 分钟，属正常现象。

## 目录

```
data-agent/          后端：FastAPI + LangGraph（README 含完整说明）
data-agent-fronted/  前端：Vue3 + Element Plus + ECharts
docs/                架构说明、部署指南、简历与毕设写作素材
```

## 文档

| 文档 | 内容 |
|---|---|
| [`data-agent/README.md`](data-agent/README.md) | **主文档**：能力清单、架构图、评测与消融数据、复现步骤 |
| [`docs/architecture.md`](docs/architecture.md) | 架构详解：13 节点、自省闭环、拒答机制、SSE 协议、设计权衡 |
| [`docs/deployment.md`](docs/deployment.md) | Docker 部署、启动顺序、常见问题 |

---

个人学习与技术实践项目，代码与文档均可自由参考。
