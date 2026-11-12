"""受试者贡献收缩与各树层真实计数（仅本地计算，绝不进入对外回执）。

公开排序规则：同一 subject_id 的多条事件按 (event_day, event_id) 升序，
每人只保留第一条；未入选事件不再对任何层计数做贡献。该排序只依赖事件自身的
公开字段，与事件类别、事件结果无关，因此选择函数不引入额外的类别级敏感度。
"""

from .io import identity, integer
from .tree import TreeError


def select_per_person(events, tree):
    """返回 {subject_id: leaf_id}，每人至多一条有效事件。"""
    chosen = {}
    for event in events:
        subject = identity(event["subject_id"])
        integer(event["event_day"])
        leaf = event["leaf"]
        if not isinstance(leaf, str) or leaf not in tree.leaf_set:
            raise TreeError("事件分类必须是公开叶节点")
        key = (integer(event["event_day"]), identity(event["event_id"]))
        if subject not in chosen or key < chosen[subject][0]:
            chosen[subject] = (key, leaf)
    return {subject: leaf for subject, (_, leaf) in chosen.items()}


def level_counts(tree, selected):
    """按公开节点顺序给出每个节点的真实整数计数（含真计数为零的叶）。

    返回 dict[node_id] -> int。此函数结果只供本地机制使用，发布层禁止写出。
    """
    leaf_counts = {leaf: 0 for leaf in tree.leaves}
    for leaf in selected.values():
        leaf_counts[leaf] += 1

    counts = dict(leaf_counts)
    for depth in range(tree.max_depth - 1, -1, -1):
        for nid in tree.nodes_by_depth[depth]:
            counts[nid] = sum(counts[child] for child in tree.children[nid])
    return {nid: counts[nid] for nid in tree.ids}
