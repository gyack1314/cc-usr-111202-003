"""固定公开分类树的解析、校验与公开遍历顺序。

树结构本身是公开信息：节点 id、父子关系、深度与叶集合均可出现在发布回执中。
节点顺序沿用资料中的公开顺序，所有需要确定性遍历的地方（噪声抽取、回执）都使用它。
"""


class TreeError(ValueError):
    """树结构不满足公开分类树约定。"""


class Tree:
    __slots__ = (
        "ids", "parent", "children", "depth", "leaves",
        "leaf_set", "nodes_by_depth", "descendant_leaves", "max_depth",
    )

    def __init__(self, nodes):
        if not isinstance(nodes, list) or not nodes:
            raise TreeError("tree 必须是非空节点列表")
        ids = []
        seen = set()
        for node in nodes:
            if not isinstance(node, dict) or "id" not in node:
                raise TreeError("节点缺少 id")
            nid = node["id"]
            if not isinstance(nid, str) or not nid.strip():
                raise TreeError("节点 id 必须是非空字符串")
            if nid in seen:
                raise TreeError(f"节点重复: {nid}")
            seen.add(nid)
            ids.append(nid)

        parent = {}
        for node, nid in zip(nodes, ids):
            p = node.get("parent")
            if p is not None and (not isinstance(p, str) or p not in seen):
                raise TreeError(f"节点 {nid} 引用了未知父节点")
            parent[nid] = p

        roots = [nid for nid in ids if parent[nid] is None]
        if len(roots) != 1:
            raise TreeError("公开树需要唯一根")

        children = {nid: [] for nid in ids}
        depth = {}
        for nid in ids:
            at = nid
            visited = set()
            steps = 0
            while at is not None:
                if at in visited:
                    raise TreeError("树含环")
                visited.add(at)
                steps += 1
                at = parent[at]
            depth[nid] = steps - 1
            p = parent[nid]
            if p is not None:
                children[p].append(nid)  # 按公开顺序追加

        max_depth = max(depth.values())
        leaf_depths = {depth[nid] for nid in ids if not children[nid]}
        if len(leaf_depths) != 1 or next(iter(leaf_depths)) != max_depth:
            raise TreeError("所有叶节点必须处于同一最大深度")

        nodes_by_depth = {d: [] for d in range(max_depth + 1)}
        for nid in ids:
            nodes_by_depth[depth[nid]].append(nid)

        leaves = [nid for nid in ids if not children[nid]]
        descendant_leaves = {nid: [] for nid in ids}
        for leaf in leaves:
            at = leaf
            while at is not None:
                descendant_leaves[at].append(leaf)
                at = parent[at]
        for nid in ids:
            descendant_leaves[nid].sort(key=leaves.index)

        self.ids = ids
        self.parent = parent
        self.children = children
        self.depth = depth
        self.leaves = leaves
        self.leaf_set = set(leaves)
        self.nodes_by_depth = nodes_by_depth
        self.descendant_leaves = descendant_leaves
        self.max_depth = max_depth

    def public_nodes(self):
        """资料中的公开节点顺序。"""
        return list(self.ids)

    def to_public_data(self):
        """可对外公开的树结构片段。"""
        return [{"id": nid, "parent": self.parent[nid]} for nid in self.ids]
