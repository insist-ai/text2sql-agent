"""RRF k 值确定性单元测试（构造数据，不依赖真实召回/embedding 服务）。

构造 20 条候选、两路交叉名次，复用 fuse_recall.py 的 rrf_fuse，
验证 k 取不同值时 Top-10 排序存在差异，且大小 k 的语义方向正确。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.nodes.fuse_recall import rrf_fuse


class Item:
    """极简候选实体，只有 id。"""
    def __init__(self, id: str):
        self.id = id
    def __repr__(self):
        return self.id


def rrf_scores(ranked_lists: list[list], key_func, k: int) -> dict:
    """计算每条的 RRF 分数（不截断），仅用于导出 CSV 与断言验证。"""
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            key = key_func(item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


def main():
    # ---------- 构造 20 条候选 ----------
    all_ids = [f"E{i:02d}" for i in range(1, 21)]

    # A 路：自然顺序 E01..E20（名次 1..20）
    list_a = [Item(id) for id in all_ids]

    # B 路：打乱，且
    #   · B 路第 1 名 = E20（A 路排名靠后）
    #   · B 路第 3 名 = E03（与 A 路同为第 3 名，形成"双路都第 3"的实体）
    b_order = [
        "E20", "E01", "E03", "E02", "E04",
        "E05", "E06", "E07", "E08", "E09",
        "E10", "E11", "E12", "E13", "E14",
        "E15", "E16", "E17", "E18", "E19",
    ]
    assert len(b_order) == 20
    assert b_order[0] == "E20"
    assert b_order[2] == "E03"
    list_b = [Item(id) for id in b_order]

    top_n = 10
    ks = [1, 10, 60]

    # ---------- 融合 ----------
    results: dict[int, list[Item]] = {}
    for k in ks:
        results[k] = rrf_fuse([list_a, list_b], lambda it: it.id, k, top_n)

    order_1 = [it.id for it in results[1]]
    order_60 = [it.id for it in results[60]]

    # ---------- 断言 (a) : k=1 与 k=60 的 Top-10 实体顺序存在差异 ----------
    assert order_1 != order_60, (
        f"k=1 与 k=60 Top-{top_n} 顺序应不同，但均为 {order_60}"
    )

    # ---------- 断言 (b) : 大小 k 语义方向正确 ----------
    # P = E20：单路 B 第 1 名（A 路 rank=20，靠后）
    # Q = E03：双路都第 3 名
    # k=1（小）→ 头部名次权重极大 → P 因 B 路第 1 名应排 Q 前面
    # k=60（大）→ 分数趋于平均 → Q 双路累积应反超 P
    scores_1 = rrf_scores([list_a, list_b], lambda it: it.id, 1)
    scores_60 = rrf_scores([list_a, list_b], lambda it: it.id, 60)

    p_key, q_key = "E20", "E03"
    assert scores_1[p_key] > scores_1[q_key], (
        f"k=1 时 {p_key}({scores_1[p_key]:.5f}) 应 > {q_key}({scores_1[q_key]:.5f})"
    )
    assert scores_60[p_key] < scores_60[q_key], (
        f"k=60 时 {p_key}({scores_60[p_key]:.5f}) 应 < {q_key}({scores_60[q_key]:.5f})"
    )

    # ---------- 输出 ----------
    print(f"构造候选: 20 条实体, A 路自然顺序, B 路打乱(E20 第 1, E03 第 3)")
    print(f"Top-N = {top_n}, 扫描 k = {ks}")
    print()
    for k in ks:
        order = [it.id for it in results[k]]
        diff = sum(1 for a, b in zip(order, order_60) if a != b)
        print(f"k={k:<3} Top-{top_n}: {' > '.join(order)}")
        print(f"      与 k=60 顺序差异条目数: {diff}")
        print()

    print("[PASS] 3/3")

    # ---------- 导出 CSV ----------
    csv_path = Path(__file__).resolve().parents[1] / "docs" / "eval" / "rrf_k_sweep_synthetic.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["k,rank,entity,score"]
    for k in ks:
        score_map = rrf_scores([list_a, list_b], lambda it: it.id, k)
        for rank, it in enumerate(results[k], start=1):
            lines.append(f"{k},{rank},{it.id},{score_map[it.id]:.5f}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"CSV 已导出: {csv_path}")


if __name__ == "__main__":
    main()
