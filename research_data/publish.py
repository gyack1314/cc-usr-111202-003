"""一次性分层计数发布的编排与不可混淆的发布回执。

回执把三类信息绑定在同一份文档里：
- 可见数字：逐节点加噪计数与一致化连续实数计数（覆盖全部公开节点，含零叶）；
- 噪声：每层 epsilon、Laplace 尺度、方差、L1 敏感度与选择规则；
- 预算：scope、release_id、扣款前后余额与总 epsilon。

对外回执禁止出现 subject_id、真实计数与随机种子；演示模式额外带不可发布标记。
"""

import hashlib
import json
import math
from fractions import Fraction

from .consistency import reconcile, weighted_objective, residuals, node_variances
from .io import rational, unique
from .ledger import Ledger
from .mechanism import make_noise_source, noisy_levels
from .selection import select_per_person, level_counts
from .tree import Tree

RECEIPT_FORMAT = "hierarchical-laplace-release/v1"
_FORBIDDEN_RECEIPT_KEYS = ("subject_id", "true_count", "true_counts",
                           "raw_count", "demo_seed", "seed")


class RequestError(ValueError):
    pass


def canonical_json(obj):
    """排序键、无空白、拒绝非有限数的规范 JSON，用于指纹与回执哈希。"""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True)


def _fingerprint(value):
    """机制相关内容的规范指纹；仅存于本地账本，不进入对外回执。"""
    events = sorted(
        (
            {
                "event_id": e["event_id"],
                "subject_id": e["subject_id"],
                "event_day": e["event_day"],
                "leaf": e["leaf"],
            }
            for e in value["events"]
        ),
        key=lambda e: (e["event_day"], e["event_id"]),
    )
    material = {
        "tree": value["tree"],
        "events": events,
        "epsilon_by_depth": value["epsilon_by_depth"],
        "privacy_scope": value["privacy_scope"],
        "scope_budget": value["scope_budget"],
        "dataset_version": value["dataset_version"],
        "mode": value["mode"],
        "demo_seed": value.get("demo_seed"),
        "solver": value.get("solver", {}),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def prepare_request(value):
    """校验资料并解析机制参数，返回树、Fraction 预算与求解器配置。"""
    if not isinstance(value, dict):
        raise RequestError("顶层需要对象")
    tree = Tree(value["tree"])
    unique(value["events"], "event_id")
    epsilons = [rational(x) for x in value["epsilon_by_depth"]]
    if len(epsilons) != tree.max_depth + 1:
        raise RequestError("epsilon_by_depth 必须与树各层一一对应")
    if any(eps <= 0 for eps in epsilons):
        raise RequestError("每层 epsilon 必须为正")
    scope_budget = rational(value["scope_budget"])
    if scope_budget <= 0:
        raise RequestError("scope_budget 必须为正")
    for label in ("privacy_scope", "release_id", "dataset_version"):
        if not isinstance(value.get(label), str) or not value[label].strip():
            raise RequestError(f"{label} 必须是非空字符串")
    mode = value.get("mode")
    if mode not in ("publication", "demonstration"):
        raise RequestError("mode 只能是 publication 或 demonstration")
    demo_seed = value.get("demo_seed")
    if mode == "demonstration":
        if isinstance(demo_seed, bool) or not isinstance(demo_seed, int) \
                or demo_seed < 0:
            raise RequestError("演示模式必须显式提供非负整数 demo_seed")
    elif demo_seed is not None:
        raise RequestError("发布模式不得携带固定种子")

    solver_in = value.get("solver", {}) or {}
    tolerance = solver_in.get("tolerance", 1e-8)
    max_iterations = solver_in.get("max_iterations", 10000)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) \
            or not math.isfinite(tolerance) or tolerance <= 0:
        raise RequestError("solver.tolerance 必须是正有限数")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) \
            or max_iterations <= 0:
        raise RequestError("solver.max_iterations 必须是正整数")
    return {
        "tree": tree,
        "epsilons": epsilons,
        "scope_budget": scope_budget,
        "mode": mode,
        "demo_seed": demo_seed,
        "tolerance": float(tolerance),
        "max_iterations": max_iterations,
    }


def _finite_floats(mapping):
    for key, val in mapping.items():
        if not isinstance(val, float) or not math.isfinite(val):
            raise RequestError(f"节点 {key} 的发布数值不是有限浮点")


def build_receipt(value, source_sha256, budget_snapshot):
    """执行选择、加噪与一致化，生成完整回执（不含真实计数与身份）。"""
    prepared = prepare_request(value)
    tree = prepared["tree"]
    epsilons = prepared["epsilons"]

    # 以下两步只在本地内存中处理真实资料，其结果不写入回执。
    selected = select_per_person(value["events"], tree)
    true_counts = level_counts(tree, selected)

    noise_source = make_noise_source(prepared["mode"], prepared["demo_seed"])
    noisy, _ = noisy_levels(tree, true_counts, epsilons, noise_source)
    _finite_floats(noisy)
    result = reconcile(
        tree, noisy, epsilons,
        tolerance=prepared["tolerance"],
        max_iterations=prepared["max_iterations"],
    )
    consistent = result["consistent_counts"]
    _finite_floats(consistent)
    variances = result["variance"]

    level_params = [
        {
            "depth": depth,
            "epsilon": str(eps),
            "laplace_scale": 1.0 / float(eps),
            "variance": 2.0 / float(eps * eps),
        }
        for depth, eps in enumerate(epsilons)
    ]
    total_epsilon = sum(epsilons, Fraction(0))
    receipt = {
        "receipt_format": RECEIPT_FORMAT,
        "privacy_scope": value["privacy_scope"],
        "release_id": value["release_id"],
        "dataset_version": value["dataset_version"],
        "source_sha256": source_sha256,
        "mode": prepared["mode"],
        "publishable": prepared["mode"] == "publication",
        "mechanism": {
            "name": "independent-laplace-per-depth",
            "adjacency": "add-or-remove-all-records-of-one-subject",
            "l1_sensitivity_by_depth": 1,
            "selection_rule": "first-event-per-subject-by-(event_day,event_id)",
            "level_params": level_params,
            "total_epsilon": str(total_epsilon),
        },
        "tree": [
            {
                "id": nid,
                "parent": tree.parent[nid],
                "depth": tree.depth[nid],
            }
            for nid in tree.ids
        ],
        "noisy_counts": {nid: noisy[nid] for nid in tree.ids},
        "consistent_counts": {nid: consistent[nid] for nid in tree.ids},
        "variance_by_node": {nid: variances[nid] for nid in tree.ids},
        "consistency": {
            "objective": result["objective"],
            "kkt_residual": result["kkt_residual"],
            "constraint_residual": result["constraint_residual"],
            "min_leaf_count": result["min_leaf_count"],
            "iterations": result["iterations"],
            "solver": {
                "method": "lawson-hanson-nonnegative-least-squares",
                "tolerance": prepared["tolerance"],
                "max_iterations": prepared["max_iterations"],
            },
        },
        "budget": dict(budget_snapshot),
    }
    if prepared["mode"] == "demonstration":
        receipt["demo_notice"] = (
            "固定种子演示输出，禁止对外发布；不构成任何隐私预算消耗凭证"
        )
    receipt["receipt_hash"] = _hash_receipt(receipt)
    check_receipt_hygiene(receipt)
    return receipt


def _hash_receipt(receipt):
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def check_receipt_hygiene(receipt):
    """对外回执卫生：无身份、无种子、无真实计数字段，所有计数为有限浮点。"""
    forbidden = set(_FORBIDDEN_RECEIPT_KEYS)

    def walk(obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key in forbidden:
                    raise RequestError(f"回执不得包含字段 {key}")
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(receipt)
    for block in ("noisy_counts", "consistent_counts"):
        for val in receipt[block].values():
            if not isinstance(val, float) or not math.isfinite(val):
                raise RequestError("回执计数必须是有限浮点")
    if receipt["mode"] == "demonstration" and receipt["publishable"]:
        raise RequestError("演示输出不得标记为可发布")
    if receipt["mode"] == "publication" and not receipt["publishable"]:
        raise RequestError("发布模式输出必须标记为可发布")
    return True


def publish_release(value, source_sha256, ledger: Ledger, *, sink=None):
    """完整发布流程：事务内预留+建单，事务提交后再对外投递。

    sink 为可选投递函数（如写文件、发消息）；投递失败不回滚预算，
    用同一 release_id 重试即可取回完全相同的回执。
    """
    prepared = prepare_request(value)
    total_epsilon = sum(prepared["epsilons"], Fraction(0))
    fingerprint = _fingerprint(value)

    def builder(snapshot):
        return build_receipt(value, source_sha256, snapshot)

    payload, reused, snapshot = ledger.reserve(
        privacy_scope=value["privacy_scope"],
        scope_budget=prepared["scope_budget"],
        release_id=value["release_id"],
        request_fingerprint=fingerprint,
        epsilon_charge=total_epsilon,
        dataset_version=value["dataset_version"],
        source_sha256=source_sha256,
        mode=prepared["mode"],
        builder=builder,
    )
    if reused:
        # 已存回执原样返回；reused 只是本次投递的元数据，不参与也不改变回执哈希。
        original_hash = payload["receipt_hash"]
        payload = dict(payload)
        payload["reused"] = True
        payload["receipt_hash"] = original_hash
    check_receipt_hygiene(payload)
    if sink is not None:
        sink(payload)  # 投递在提交之后；失败由调用方用同 ID 重试
    return payload


def recompute_diagnostics(receipt):
    """仅凭公开回执重算目标值与残差，不接触原始资料。"""
    tree = Tree([
        {"id": node["id"], "parent": node["parent"]}
        for node in receipt["tree"]
    ])
    epsilons = [
        Fraction(level["epsilon"])
        for level in sorted(receipt["mechanism"]["level_params"],
                            key=lambda x: x["depth"])
    ]
    variances = node_variances(tree, epsilons)
    noisy = receipt["noisy_counts"]
    consistent = receipt["consistent_counts"]
    leaf_weights = [consistent[leaf] for leaf in tree.leaves]
    diag = residuals(tree, noisy, consistent, variances, leaf_weights)
    diag["objective"] = weighted_objective(noisy, consistent, variances)
    return diag
