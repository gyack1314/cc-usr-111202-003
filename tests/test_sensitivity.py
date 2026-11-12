"""用小树核对每人至多一条事件、层向量 L1 敏感度与公开遍历口径。"""
import json
import unittest

from research_data.io import load
from research_data.selection import select_per_person, level_counts
from research_data.tree import Tree

TINY_TREE = [
    {"id": "all", "parent": None},
    {"id": "a", "parent": "all"},
    {"id": "b", "parent": "all"},
    {"id": "a1", "parent": "a"},
    {"id": "a2", "parent": "a"},
    {"id": "b1", "parent": "b"},
    {"id": "b2", "parent": "b"},
]


def _event(eid, subject, day, leaf):
    return {"event_id": eid, "subject_id": subject,
            "event_day": day, "leaf": leaf}


class SensitivityTests(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(TINY_TREE)

    def test_first_event_by_public_order_only(self):
        events = [
            _event("e0", "p1", 5, "a2"),
            _event("e1", "p1", 1, "a1"),
            _event("e2", "p1", 1, "b1"),
            _event("e3", "p2", 9, "b2"),
        ]
        selected = select_per_person(events, self.tree)
        # (day,event_id) 升序：p1 的 (1,e1) 入选；同天 e2 不入选。
        self.assertEqual(selected, {"p1": "a1", "p2": "b2"})
        counts = level_counts(self.tree, selected)
        self.assertEqual(counts["a1"], 1)
        self.assertEqual(counts["a2"], 0)       # 未入选事件不贡献
        self.assertEqual(counts["b2"], 1)
        self.assertEqual(counts["b1"], 0)
        self.assertEqual(counts["a"], 1)
        self.assertEqual(counts["all"], 2)

    def test_layer_l1_sensitivity_is_one_for_add_and_remove(self):
        base = [_event("e0", "p1", 1, "a1"), _event("e1", "p2", 2, "b2")]
        with_person = base + [
            _event("e2", "p3", 1, "b1"),
            _event("e3", "p3", 2, "b2"),  # 同一人的多条记录只留一条
        ]
        c0 = level_counts(self.tree, select_per_person(base, self.tree))
        c1 = level_counts(self.tree, select_per_person(with_person, self.tree))
        for depth in range(self.tree.max_depth + 1):
            nodes = self.tree.nodes_by_depth[depth]
            l1 = sum(abs(c1[n] - c0[n]) for n in nodes)
            self.assertEqual(l1, 1, f"深度 {depth} 的 L1 敏感度应为 1")
        # 反向：删除一个人的全部记录同样只移动 1。
        c2 = level_counts(
            self.tree,
            select_per_person([e for e in with_person
                               if e["subject_id"] != "p3"], self.tree),
        )
        for depth in range(self.tree.max_depth + 1):
            nodes = self.tree.nodes_by_depth[depth]
            self.assertEqual(sum(abs(c0[n] - c2[n]) for n in nodes), 0)

    def test_zero_leaves_are_present_in_public_tree(self):
        selected = select_per_person([_event("e0", "p1", 1, "a1")], self.tree)
        counts = level_counts(self.tree, selected)
        self.assertEqual(set(counts), set(self.tree.ids))
        self.assertEqual(counts["b1"], 0)
        self.assertEqual(counts["b2"], 0)

    def test_tree_rejects_cycle_and_mixed_leaf_depth(self):
        cyclic = json.loads(json.dumps(TINY_TREE))
        cyclic[0]["parent"] = "b2"  # 资料 invalid-domain 的同类情形
        with self.assertRaises(ValueError):
            Tree(cyclic)
        shallow = [
            {"id": "r", "parent": None},
            {"id": "x", "parent": "r"},
            {"id": "y", "parent": "r"},
            {"id": "x1", "parent": "x"},
        ]  # y 自身是叶但深度浅于 x1
        with self.assertRaises(ValueError):
            Tree(shallow)

    def test_bundled_material_sensitivity_small_tree(self):
        value, _ = load("data/normal.json")
        tree = Tree(value["tree"])
        selected = select_per_person(value["events"], tree)
        counts = level_counts(tree, selected)
        # p1 有两条事件，只保留 (1,e0)->east-early。
        self.assertEqual(selected["p1"], "east-early")
        self.assertEqual(counts["all"], 3)
        self.assertEqual(sum(counts[leaf] for leaf in tree.leaves), 3)


if __name__ == "__main__":
    unittest.main()
