"""核对带权非负最小二乘一致化：解析解、约束、残差与后处理口径。"""
import math
import unittest
from fractions import Fraction

from research_data.consistency import (
    reconcile, weighted_objective, residuals, node_variances,
)
from research_data.mechanism import DemoNoise, noisy_levels
from research_data.selection import level_counts, select_per_person
from research_data.tree import Tree

EPS = (Fraction(1), Fraction(1))


def _two_leaf_tree():
    return Tree([
        {"id": "r", "parent": None},
        {"id": "l", "parent": "r"},
        {"id": "m", "parent": "r"},
    ])


def clip_then_spread(noisy, tree, variances):
    """被明确禁止冒充最优解的启发式：叶截负后按比例把父值摊给子树。"""
    leaves = {leaf: max(0.0, noisy[leaf]) for leaf in tree.leaves}
    result = dict(leaves)
    for depth in range(tree.max_depth - 1, -1, -1):
        for nid in tree.nodes_by_depth[depth]:
            child_sum = sum(result[c] for c in tree.children[nid])
            target = max(0.0, noisy[nid])
            if child_sum > 0:
                for child in tree.children[nid]:
                    result[child] = result[child] / child_sum * target
            else:
                share = target / len(tree.children[nid])
                for child in tree.children[nid]:
                    result[child] = share
            result[nid] = target
    return result


class ConsistencyTests(unittest.TestCase):
    def test_single_leaf_analytic_solution(self):
        tree = Tree([{"id": "r", "parent": None},
                     {"id": "only", "parent": "r"}])
        # 同层等权时，单叶即根：最优值是两个加噪值的加权平均（夹到非负）。
        noisy = {"r": -1.0, "only": -3.0}
        out = reconcile(tree, noisy, EPS)
        x = out["consistent_counts"]
        self.assertAlmostEqual(x["r"], x["only"], places=12)
        self.assertAlmostEqual(x["only"], 0.0, places=10)
        self.assertGreaterEqual(x["only"], 0.0)
        self.assertLessEqual(out["kkt_residual"], 1e-6)

        noisy = {"r": 2.0, "only": 4.0}
        out = reconcile(tree, noisy, EPS)
        x = out["consistent_counts"]
        self.assertAlmostEqual(x["only"], 3.0, places=8)
        self.assertAlmostEqual(x["r"], 3.0, places=8)
        self.assertLessEqual(out["kkt_residual"], 1e-6)

    def test_constraints_and_nonnegativity_on_small_tree(self):
        tree = _two_leaf_tree()
        noisy = {"r": 0.0, "l": -1.0, "m": 3.0}
        out = reconcile(tree, noisy, EPS)
        x = out["consistent_counts"]
        self.assertGreaterEqual(x["l"], 0.0)
        self.assertGreaterEqual(x["m"], 0.0)
        self.assertAlmostEqual(x["r"], x["l"] + x["m"], places=10)
        self.assertLessEqual(out["constraint_residual"], 1e-8)
        self.assertLessEqual(out["kkt_residual"], 1e-6)

        # 解析最优：l 被夹到 0，m 取 r 与 m 的均值 1.5。
        # 本层 var=2，权重 1/2：目标 = (2.25+1+2.25)/2 = 2.75。
        self.assertAlmostEqual(x["l"], 0.0, places=8)
        self.assertAlmostEqual(x["m"], 1.5, places=8)
        self.assertAlmostEqual(x["r"], 1.5, places=8)
        self.assertAlmostEqual(out["objective"], 2.75, places=6)

    def test_objective_recomputable_and_beats_naive_clip_spread(self):
        tree = _two_leaf_tree()
        noisy = {"r": 0.0, "l": -1.0, "m": 3.0}
        variances = node_variances(tree, EPS)
        out = reconcile(tree, noisy, EPS)
        x = out["consistent_counts"]
        # 仅凭回执中的数值重算目标值。
        again = weighted_objective(noisy, x, variances)
        self.assertAlmostEqual(again, out["objective"], places=12)
        diag = residuals(tree, noisy, x, variances,
                         [x["l"], x["m"]])
        self.assertLessEqual(diag["kkt_residual"], 1e-6)

        naive = clip_then_spread(noisy, tree, variances)
        naive_obj = weighted_objective(noisy, naive, variances)
        self.assertLess(out["objective"], naive_obj - 1e-9)

    def test_weights_follow_registered_depth_epsilon(self):
        tree = Tree([
            {"id": "r", "parent": None},
            {"id": "a", "parent": "r"},
            {"id": "b", "parent": "r"},
            {"id": "a1", "parent": "a"},
            {"id": "a2", "parent": "a"},
            {"id": "b1", "parent": "b"},
            {"id": "b2", "parent": "b"},
        ])
        eps = (Fraction("1/5"), Fraction("2/5"), Fraction("2/5"))
        variances = node_variances(tree, eps)
        self.assertAlmostEqual(variances["r"], 2.0 / 0.04)
        self.assertAlmostEqual(variances["a"], 2.0 / 0.16)
        self.assertAlmostEqual(variances["a1"], 2.0 / 0.16)
        noisy = {nid: float(i) - 2.0 for i, nid in enumerate(tree.ids)}
        out = reconcile(tree, noisy, eps)
        x = out["consistent_counts"]
        for nid in tree.ids:
            self.assertTrue(math.isfinite(x[nid]))
            self.assertGreaterEqual(x[nid], -1e-10)
        for nid in tree.ids:
            if tree.children[nid]:
                self.assertAlmostEqual(
                    x[nid], sum(x[c] for c in tree.children[nid]), places=8
                )

    def test_noise_covers_zero_true_leaves_and_is_deterministic_in_demo(self):
        tree = _two_leaf_tree()
        selected = {}  # 没有受试者：两个叶真计数都是零
        true_counts = level_counts(tree, selected)
        n1, _ = noisy_levels(tree, true_counts, EPS, DemoNoise(7))
        n2, _ = noisy_levels(tree, true_counts, EPS, DemoNoise(7))
        n3, _ = noisy_levels(tree, true_counts, EPS, DemoNoise(8))
        self.assertEqual(n1, n2)                  # 同种子可复现
        self.assertNotEqual(n1, n3)               # 不同种子结果不同
        self.assertTrue(any(n1[leaf] != 0.0 for leaf in tree.leaves))
        out = reconcile(tree, n1, EPS)
        self.assertLessEqual(out["constraint_residual"], 1e-8)


if __name__ == "__main__":
    unittest.main()
