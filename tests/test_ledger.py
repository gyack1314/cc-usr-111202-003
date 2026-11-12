"""账本并发额度竞争、故障恢复、幂等与跨版本累计的核对。"""
import copy
import json
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path

from research_data.io import load
from research_data.ledger import (
    Ledger, BudgetExceeded, IdempotencyConflict, ScopeBudgetConflict,
)
from research_data.publish import publish_release

TREE = [
    {"id": "all", "parent": None},
    {"id": "east", "parent": "all"},
    {"id": "west", "parent": "all"},
    {"id": "east-early", "parent": "east"},
    {"id": "east-late", "parent": "east"},
    {"id": "west-early", "parent": "west"},
    {"id": "west-late", "parent": "west"},
]


def make_value(release_id, *, scope="cohort", version="v1", budget="2",
               eps=("1/5", "2/5", "2/5"), mode="publication", seed=None):
    value = {
        "tree": TREE,
        "events": [
            {"event_id": "e0", "subject_id": "p1", "event_day": 1,
             "leaf": "east-early"},
            {"event_id": "e1", "subject_id": "p1", "event_day": 2,
             "leaf": "east-late"},
            {"event_id": "e2", "subject_id": "p2", "event_day": 1,
             "leaf": "east-early"},
        ],
        "epsilon_by_depth": list(eps),
        "privacy_scope": scope,
        "dataset_version": version,
        "release_id": release_id,
        "scope_budget": budget,
        "mode": mode,
        "solver": {"tolerance": 1e-8, "max_iterations": 10000},
    }
    if mode == "demonstration":
        value["demo_seed"] = 217 if seed is None else seed
    return value


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = str(Path(self.tmp.name) / "ledger.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_requests_never_overspend(self):
        # 总 epsilon = 1，预算 2：最多两单，第三单必须被拒。
        Ledger(self.ledger_path).close()  # 预热账本文件，隔离建表竞态
        results = []
        barrier = threading.Barrier(5)
        errors = []

        def worker(idx):
            try:
                with Ledger(self.ledger_path) as ledger:
                    barrier.wait()
                    payload, reused, _ = ledger.reserve(
                        privacy_scope="cohort",
                        scope_budget=Fraction(2),
                        release_id=f"r{idx}",
                        request_fingerprint=f"fp{idx}",
                        epsilon_charge=Fraction(1),
                        dataset_version="v1",
                        source_sha256="0" * 64,
                        mode="publication",
                        builder=lambda snap: {"k": idx, "budget": snap},
                    )
                    results.append((payload["k"], reused))
            except Exception as exc:  # noqa: BLE001 - 测试需要捕获竞争结果
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 2)
        self.assertTrue(all(not reused for _, reused in results))
        self.assertEqual(errors.count("BudgetExceeded"), 3)
        with Ledger(self.ledger_path) as ledger:
            status = ledger.scope_status("cohort")
        self.assertEqual(status["spent"], "2")
        self.assertEqual(status["remaining"], "0")
        self.assertEqual(len(status["releases"]), 2)

    def test_concurrent_duplicate_same_id_returns_one_charge(self):
        Ledger(self.ledger_path).close()  # 预热账本文件
        outcomes = []
        barrier = threading.Barrier(4)
        lock = threading.Lock()

        def worker():
            try:
                with Ledger(self.ledger_path) as ledger:
                    barrier.wait()
                    payload, reused, _ = ledger.reserve(
                        privacy_scope="cohort",
                        scope_budget=Fraction(2),
                        release_id="dup",
                        request_fingerprint="same-fingerprint",
                        epsilon_charge=Fraction(1),
                        dataset_version="v1",
                        source_sha256="0" * 64,
                        mode="publication",
                        builder=lambda snap: {"answer": 42},
                    )
                    with lock:
                        outcomes.append((payload, reused))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    outcomes.append(("error", type(exc).__name__))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(outcomes), 4)
        self.assertTrue(all(o[0] == {"answer": 42} for o in outcomes))
        self.assertEqual(sum(1 for _, reused in outcomes if reused), 3)
        with Ledger(self.ledger_path) as ledger:
            status = ledger.scope_status("cohort")
        self.assertEqual(status["spent"], "1")
        self.assertEqual(len(status["releases"]), 1)

    def test_builder_failure_rolls_back_without_charge(self):
        with Ledger(self.ledger_path) as ledger:
            with self.assertRaises(RuntimeError):
                ledger.reserve(
                    privacy_scope="cohort",
                    scope_budget=Fraction(2),
                    release_id="broken",
                    request_fingerprint="fp",
                    epsilon_charge=Fraction(1),
                    dataset_version="v1",
                    source_sha256="0" * 64,
                    mode="publication",
                    builder=lambda snap: (_ for _ in ()).throw(
                        RuntimeError("注入故障")),
                )
            status = ledger.scope_status("cohort")
            # 回滚后连 scope 行都不存在；同一 release_id 可正常重试。
            self.assertIsNone(status)
            payload, reused, snap = ledger.reserve(
                privacy_scope="cohort",
                scope_budget=Fraction(2),
                release_id="broken",
                request_fingerprint="fp",
                epsilon_charge=Fraction(1),
                dataset_version="v1",
                source_sha256="0" * 64,
                mode="publication",
                builder=lambda s: {"ok": True},
            )
            self.assertFalse(reused)
            self.assertEqual(snap["spent_after"], "1")

    def test_delivery_failure_keeps_charge_and_retry_returns_same_receipt(self):
        value = make_value("r1")
        attempts = {"n": 0}

        def failing_sink(_):
            attempts["n"] += 1
            raise OSError("对外投递失败")

        with Ledger(self.ledger_path) as ledger:
            with self.assertRaises(OSError):
                publish_release(value, "a" * 64, ledger, sink=failing_sink)
            status = ledger.scope_status("cohort")
            self.assertEqual(status["spent"], "1")  # 预算已保留

            delivered = []
            first = publish_release(value, "a" * 64, ledger,
                                    sink=lambda r: delivered.append(r))
            second = publish_release(value, "a" * 64, ledger)
        self.assertEqual(attempts["n"], 1)
        self.assertTrue(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["receipt_hash"], second["receipt_hash"])
        self.assertEqual(first["noisy_counts"], second["noisy_counts"])
        self.assertEqual(len(delivered), 1)

    def test_duplicate_request_is_identical_and_not_recharged(self):
        value = make_value("r1")
        with Ledger(self.ledger_path) as ledger:
            first = publish_release(value, "a" * 64, ledger)
            second = publish_release(value, "a" * 64, ledger)
            status = ledger.scope_status("cohort")
        self.assertEqual(first["receipt_hash"], second["receipt_hash"])
        self.assertEqual(first["noisy_counts"], second["noisy_counts"])
        self.assertEqual(first["consistent_counts"],
                         second["consistent_counts"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(status["releases"]), 1)
        self.assertEqual(status["spent"], "1")

    def test_parameter_conflict_on_same_id_is_rejected(self):
        value = make_value("r1")
        changed = make_value("r1", eps=("1/10", "1/10", "1/10"))
        with Ledger(self.ledger_path) as ledger:
            publish_release(value, "a" * 64, ledger)
            with self.assertRaises(IdempotencyConflict):
                publish_release(changed, "a" * 64, ledger)
            # 内容变化（事件不同）同样冲突。
            other = copy.deepcopy(value)
            other["events"].append(
                {"event_id": "e9", "subject_id": "p9", "event_day": 4,
                 "leaf": "west-late"})
            with self.assertRaises(IdempotencyConflict):
                publish_release(other, "b" * 64, ledger)
            status = ledger.scope_status("cohort")
        self.assertEqual(status["spent"], "1")

    def test_new_version_same_scope_accumulates_budget(self):
        v1 = make_value("r1", version="v1")
        v2 = make_value("r2", version="v2")
        with Ledger(self.ledger_path) as ledger:
            publish_release(v1, "a" * 64, ledger)
            publish_release(v2, "c" * 64, ledger)
            status = ledger.scope_status("cohort")
        self.assertEqual(status["spent"], "2")
        self.assertEqual(status["remaining"], "0")
        versions = {r["dataset_version"] for r in status["releases"]}
        self.assertEqual(versions, {"v1", "v2"})

        # 预算耗尽后第三单（即使换文件名/版本）也必须拒绝。
        v3 = make_value("r3", version="v3")
        with Ledger(self.ledger_path) as ledger:
            with self.assertRaises(BudgetExceeded):
                publish_release(v3, "d" * 64, ledger)

    def test_exact_fraction_arithmetic_no_float_overspend(self):
        value = make_value("r1", eps=("1/3", "1/3", "1/3"), budget="1")
        with Ledger(self.ledger_path) as ledger:
            publish_release(value, "a" * 64, ledger)
            status = ledger.scope_status("cohort")
        self.assertEqual(status["spent"], "1")       # 1/3+1/3+1/3 精确为 1
        self.assertEqual(status["remaining"], "0")
        nxt = make_value("r2", eps=("1/3", "1/3", "1/3"), budget="1")
        with Ledger(self.ledger_path) as ledger:
            with self.assertRaises(BudgetExceeded):
                publish_release(nxt, "e" * 64, ledger)

    def test_scope_budget_change_is_conflict(self):
        with Ledger(self.ledger_path) as ledger:
            ledger.reserve(
                privacy_scope="cohort", scope_budget=Fraction(2),
                release_id="r1", request_fingerprint="f1",
                epsilon_charge=Fraction(1), dataset_version="v1",
                source_sha256="0" * 64, mode="publication",
                builder=lambda s: {"x": 1},
            )
            with self.assertRaises(ScopeBudgetConflict):
                ledger.reserve(
                    privacy_scope="cohort", scope_budget=Fraction(3),
                    release_id="r2", request_fingerprint="f2",
                    epsilon_charge=Fraction(1), dataset_version="v1",
                    source_sha256="0" * 64, mode="publication",
                    builder=lambda s: {"x": 2},
                )

    def test_bundled_data_versions_share_scope_budget(self):
        v1, digest1 = load("data/normal.json")
        v2, digest2 = load("data/same-scope-new-version.json")
        self.assertEqual(v1["privacy_scope"], v2["privacy_scope"])
        self.assertEqual(v1["scope_budget"], v2["scope_budget"])
        with Ledger(self.ledger_path) as ledger:
            publish_release(v1, digest1, ledger)
            publish_release(v2, digest2, ledger)
            status = ledger.scope_status(v1["privacy_scope"])
        # 两份资料各扣 1，scope 预算 2 全部用尽。
        self.assertEqual(status["spent"], "2")


if __name__ == "__main__":
    unittest.main()
