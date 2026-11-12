"""按 privacy_scope 累计的隐私预算账本（SQLite，单文件、无外部服务）。

约定：
- 相邻发布共享 scope 即共享额度，dataset_version 换版本不重置预算；
- release_id 是 scope 内幂等键，同 ID 同请求重取同一回执，不再抽样、不重复扣款；
- 同 ID 但内容/机制参数冲突一律拒绝；
- 预留与回执生成在同一事务内完成，事务提交后发布失败也保留已消耗额度；
- BEGIN IMMEDIATE 让并发提交在写锁上排队，逐事务校验余额，杜绝并发超支。

金额全部以 Fraction 的规范字符串存储，不用二进制浮点判断超支。
"""

import json
import sqlite3
from fractions import Fraction


class LedgerError(RuntimeError):
    pass


class BudgetExceeded(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class ScopeBudgetConflict(LedgerError):
    pass


class Ledger:
    schema_version = 1

    def __init__(self, path):
        self._path = str(path)
        self._conn = sqlite3.connect(
            self._path, timeout=30.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        # WAL 与外键设置必须在事务之外完成。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _initialize(self):
        # 用写锁串行化首次建表，避免多个连接同时创建账本文件。
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {
                row[0]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not tables:
                # 逐条执行，保持在当前事务内，结束后统一 COMMIT。
                for statement in (
                    "CREATE TABLE scopes("
                    "privacy_scope TEXT PRIMARY KEY,"
                    "scope_budget TEXT NOT NULL, spent TEXT NOT NULL)",
                    "CREATE TABLE releases("
                    "privacy_scope TEXT NOT NULL, release_id TEXT NOT NULL,"
                    "request_fingerprint TEXT NOT NULL,"
                    "epsilon_charge TEXT NOT NULL,"
                    "dataset_version TEXT NOT NULL,"
                    "source_sha256 TEXT NOT NULL, mode TEXT NOT NULL,"
                    "payload_json TEXT NOT NULL,"
                    "created_at TEXT NOT NULL DEFAULT "
                    "(strftime('%Y-%m-%dT%H:%M:%fZ','now')),"
                    "PRIMARY KEY (privacy_scope, release_id))",
                ):
                    self._conn.execute(statement)
            elif "scopes" not in tables or "releases" not in tables:
                raise LedgerError("账本文件结构无法识别")
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def reserve(self, *, privacy_scope, scope_budget, release_id,
                request_fingerprint, epsilon_charge, dataset_version,
                source_sha256, mode, builder):
        """一次事务内完成余额校验、幂等判定、抽样建单与扣款。

        builder 只在真正新建发布时于事务内调用，返回可 JSON 序列化的回执；
        builder 抛错则整单回滚、不扣款。返回 (payload, reused, budget_snapshot)。
        """
        budget = self._as_fraction(scope_budget, "scope_budget")
        charge = self._as_fraction(epsilon_charge, "epsilon_charge")
        if budget <= 0:
            raise LedgerError("scope_budget 必须为正")
        if charge <= 0:
            raise LedgerError("epsilon_charge 必须为正")

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT scope_budget, spent FROM scopes WHERE privacy_scope=?",
                (privacy_scope,),
            ).fetchone()
            if row is None:
                spent = Fraction(0)
                self._conn.execute(
                    "INSERT INTO scopes(privacy_scope, scope_budget, spent) "
                    "VALUES(?,?,?)",
                    (privacy_scope, str(budget), "0"),
                )
            else:
                registered_budget = Fraction(row["scope_budget"])
                if registered_budget != budget:
                    raise ScopeBudgetConflict(
                        f"scope {privacy_scope} 已登记预算 "
                        f"{registered_budget}，与本次 {budget} 冲突"
                    )
                spent = Fraction(row["spent"])

            existing = self._conn.execute(
                "SELECT request_fingerprint, payload_json FROM releases "
                "WHERE privacy_scope=? AND release_id=?",
                (privacy_scope, release_id),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict(
                        f"release_id {release_id} 已存在且请求指纹不同"
                    )
                payload = json.loads(existing["payload_json"])
                snapshot = self._snapshot(budget, spent)
                self._conn.execute("COMMIT")
                return payload, True, snapshot

            if spent + charge > budget:
                raise BudgetExceeded(
                    f"scope {privacy_scope} 预算不足：已耗 {spent} + "
                    f"本次 {charge} > 总额 {budget}"
                )

            spent_after = spent + charge
            snapshot = {
                "scope_budget": str(budget),
                "spent_before": str(spent),
                "epsilon_charge": str(charge),
                "spent_after": str(spent_after),
                "remaining": str(budget - spent_after),
            }
            payload = builder(snapshot)  # 事务内抽样，失败随事务回滚
            payload_json = json.dumps(
                payload, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"), sort_keys=True,
            )
            self._conn.execute(
                "INSERT INTO releases(privacy_scope, release_id, "
                "request_fingerprint, epsilon_charge, dataset_version, "
                "source_sha256, mode, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (privacy_scope, release_id, request_fingerprint, str(charge),
                 dataset_version, source_sha256, mode, payload_json),
            )
            self._conn.execute(
                "UPDATE scopes SET spent=? WHERE privacy_scope=?",
                (str(spent_after), privacy_scope),
            )
            self._conn.execute("COMMIT")
            return payload, False, snapshot
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def scope_status(self, privacy_scope):
        row = self._conn.execute(
            "SELECT scope_budget, spent FROM scopes WHERE privacy_scope=?",
            (privacy_scope,),
        ).fetchone()
        if row is None:
            return None
        releases = [
            dict(r)
            for r in self._conn.execute(
                "SELECT release_id, dataset_version, epsilon_charge, mode, "
                "created_at FROM releases WHERE privacy_scope=? "
                "ORDER BY created_at, release_id",
                (privacy_scope,),
            )
        ]
        return {
            "privacy_scope": privacy_scope,
            "scope_budget": row["scope_budget"],
            "spent": row["spent"],
            "remaining": str(Fraction(row["scope_budget"])
                             - Fraction(row["spent"])),
            "releases": releases,
        }

    @staticmethod
    def _as_fraction(value, label):
        if not isinstance(value, Fraction):
            raise LedgerError(f"{label} 必须是 Fraction")
        return value

    @staticmethod
    def _snapshot(budget, spent):
        return {
            "scope_budget": str(budget),
            "spent_before": str(spent),
            "epsilon_charge": None,
            "spent_after": str(spent),
            "remaining": str(budget - spent),
        }
