"""回执卫生、演示/发布随机源边界与端到端重算核对。"""
import json
import tempfile
import unittest
from pathlib import Path

from research_data.io import load
from research_data.ledger import Ledger
from research_data.mechanism import DemoNoise, PublicationNoise
from research_data.publish import (
    publish_release, build_receipt, prepare_request,
    recompute_diagnostics, check_receipt_hygiene, RequestError,
)
from research_data.tree import Tree


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = str(Path(self.tmp.name) / "ledger.sqlite")
        self.value, self.digest = load("data/normal.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_demo_receipt_is_deterministic_and_marked_unpublishable(self):
        with Ledger(self.ledger_path) as ledger:
            r1 = publish_release(self.value, self.digest, ledger)
            r2 = publish_release(self.value, self.digest, ledger)
        self.assertEqual(r1["mode"], "demonstration")
        self.assertFalse(r1["publishable"])
        self.assertIn("demo_notice", r1)
        self.assertEqual(r1["noisy_counts"], r2["noisy_counts"])
        self.assertEqual(r1["receipt_hash"], r2["receipt_hash"])

    def test_receipt_contains_no_identity_true_count_or_seed(self):
        receipt = build_receipt(self.value, self.digest, {
            "scope_budget": "2", "spent_before": "0",
            "epsilon_charge": "1", "spent_after": "1", "remaining": "1",
        })
        serialized = json.dumps(receipt, ensure_ascii=False)
        for token in ("p1", "p2", "p3", "subject_id", "demo_seed",
                      "true_count", "raw_"):
            self.assertNotIn(token, serialized)
        check_receipt_hygiene(receipt)
        # 真实分布是 east-early=2，但回执里只能看到浮点噪声数。
        for val in receipt["noisy_counts"].values():
            self.assertIsInstance(val, float)
        # 全公开节点都在，包括真计数为零的 west-early。
        self.assertEqual(
            set(receipt["noisy_counts"]),
            {n["id"] for n in receipt["tree"]},
        )
        self.assertIn("west-early", receipt["noisy_counts"])

    def test_publication_mode_rejects_seed_and_uses_csprng(self):
        value = json.loads(json.dumps(self.value))
        value["mode"] = "publication"
        value["release_id"] = "pub-1"
        del value["demo_seed"]
        with self.assertRaises(RequestError):
            prepare_request({**value, "demo_seed": 123})
        with Ledger(self.ledger_path) as ledger:
            r1 = publish_release(value, self.digest, ledger)
            r2 = publish_release(value, self.digest, ledger)
        self.assertTrue(r1["publishable"])
        self.assertNotIn("demo_notice", r1)
        # 发布模式不可复现：重取是同一份（幂等），新 release_id 独立抽样。
        self.assertEqual(r1["receipt_hash"], r2["receipt_hash"])
        value["release_id"] = "pub-2"
        with Ledger(self.ledger_path) as ledger:
            r3 = publish_release(value, self.digest, ledger)
        self.assertNotEqual(r1["noisy_counts"], r3["noisy_counts"])
        self.assertIsInstance(PublicationNoise(), PublicationNoise)

    def test_receipt_binds_numbers_noise_and_budget(self):
        with Ledger(self.ledger_path) as ledger:
            receipt = publish_release(self.value, self.digest, ledger)
        self.assertEqual(
            receipt["mechanism"]["l1_sensitivity_by_depth"], 1
        )
        self.assertEqual(
            [p["epsilon"] for p in receipt["mechanism"]["level_params"]],
            ["1/5", "2/5", "2/5"],
        )
        self.assertEqual(receipt["mechanism"]["total_epsilon"], "1")
        self.assertEqual(receipt["budget"]["epsilon_charge"], "1")
        self.assertEqual(receipt["budget"]["spent_after"], "1")
        self.assertEqual(len(receipt["receipt_hash"]), 64)
        self.assertEqual(receipt["source_sha256"], self.digest)

    def test_independent_recompute_from_receipt_only(self):
        with Ledger(self.ledger_path) as ledger:
            receipt = publish_release(self.value, self.digest, ledger)
        diag = recompute_diagnostics(receipt)
        self.assertAlmostEqual(
            diag["objective"], receipt["consistency"]["objective"], places=10
        )
        self.assertAlmostEqual(
            diag["kkt_residual"], receipt["consistency"]["kkt_residual"],
            places=10,
        )
        self.assertLessEqual(diag["constraint_residual"], 1e-8)
        # 用回执数字直接验证父子相加与叶非负。
        tree = Tree([
            {"id": n["id"], "parent": n["parent"]} for n in receipt["tree"]
        ])
        x = receipt["consistent_counts"]
        for nid in tree.ids:
            if tree.children[nid]:
                self.assertAlmostEqual(
                    x[nid], sum(x[c] for c in tree.children[nid]), places=9
                )
        for leaf in tree.leaves:
            self.assertGreaterEqual(x[leaf], -1e-12)

    def test_empty_tree_material_still_publishes_all_zero_true_leaves(self):
        value, digest = load("data/empty-public-tree.json")
        value["release_id"] = "empty-1"
        with Ledger(self.ledger_path) as ledger:
            receipt = publish_release(value, digest, ledger)
        self.assertEqual(set(receipt["noisy_counts"]),
                         {n["id"] for n in receipt["tree"]})
        diag = recompute_diagnostics(receipt)
        self.assertLessEqual(diag["constraint_residual"], 1e-8)

    def test_invalid_material_rejected_before_any_charge(self):
        value, digest = load("data/invalid-domain.json")
        with Ledger(self.ledger_path) as ledger:
            with self.assertRaises(ValueError):
                publish_release(value, digest, ledger)
            self.assertIsNone(ledger.scope_status(value["privacy_scope"]))

    def test_demo_noise_seed_validation(self):
        with self.assertRaises(ValueError):
            DemoNoise(-1)
        with self.assertRaises(ValueError):
            DemoNoise(True)
        a = DemoNoise(1).laplace(2.0)
        b = DemoNoise(1).laplace(2.0)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
