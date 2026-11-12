"""全节点带权非负最小二乘一致化（后处理）。

已知加噪值 y_v 与方差 var_v = 2/epsilon_depth(v)^2，求
    min  sum_v (x_v - y_v)^2 / var_v
    s.t. x_v = 子节点和（所有内部节点）， x_leaf >= 0。
用叶向量 w >= 0 参数化 x = A w（A 的行是全部节点、列是叶，A[v,leaf]=1 当且仅当
叶在 v 子树下），问题等价于非负最小二乘
    min || W^{1/2} (A w - y) ||_2^2,  w >= 0,  W=diag(1/var)。
采用 Lawson-Hanson 主动集法求解：这是带非负约束二次规划的标准最优解，
不是“截负再按比例摊平”的启发式。整个过程只读取已加噪值，不接触真实计数，
也不强制根等于真实总数。
"""

import math
from fractions import Fraction


class SolverError(ValueError):
    pass


def _solve_spd(matrix, rhs):
    """部分主元高斯消元，matrix 为对称正定方阵（浮点）。"""
    n = len(rhs)
    a = [row[:] for row in matrix]
    b = rhs[:]
    for k in range(n):
        pivot_row = max(range(k, n), key=lambda r: abs(a[r][k]))
        if abs(a[pivot_row][k]) <= 1e-18:
            raise SolverError("正规方程矩阵奇异，无法继续一致化")
        if pivot_row != k:
            a[k], a[pivot_row] = a[pivot_row], a[k]
            b[k], b[pivot_row] = b[pivot_row], b[k]
        pivot = a[k][k]
        for r in range(k + 1, n):
            factor = a[r][k] / pivot
            if factor:
                a[r][k] = 0.0
                for c in range(k + 1, n):
                    a[r][c] -= factor * a[k][c]
                b[r] -= factor * b[k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (b[i] - sum(a[i][j] * x[j] for j in range(i + 1, n))) / a[i][i]
    return x


def _lawson_hanson(c, d, tolerance, max_iterations):
    """Lawson-Hanson 主动集 NNLS，最小化 0.5 w'Cw - d'w，w>=0。

    按 Lawson & Hanson (1974/1995) 经典流程实现：外层按负梯度把变量
    加入主动集 P，内层在 P 上解无约束最小二乘，出现负值时沿线段插值
    并把触零变量移出 P；移动集合单调变化，保证有限步终止。
    """
    n = len(d)
    w = [0.0] * n
    p_set = []                       # 主动集（允许为正的列）
    r_set = list(range(n))           # 被动集（固定为零的列）
    scale = max(max(abs(v) for v in row) for row in c) or 1.0
    tol = tolerance * max(1.0, scale, max(abs(v) for v in d) or 1.0)
    iterations = 0

    while r_set:
        iterations += 1
        if iterations > max_iterations:
            raise SolverError("一致化超过最大迭代次数")
        # 负梯度 -∇f = d - Cw，在被动集中选最大者。
        candidates = [
            (j, d[j] - sum(c[j][k] * w[k] for k in range(n)))
            for j in r_set
        ]
        j, best = max(candidates, key=lambda item: item[1])
        if best <= tol:
            break
        r_set.remove(j)
        p_set.append(j)

        while True:
            iterations += 1
            if iterations > max_iterations:
                raise SolverError("一致化超过最大迭代次数")
            sub = [[c[a][b] for b in p_set] for a in p_set]
            s_sub = _solve_spd(sub, [d[a] for a in p_set])
            if all(value > tol for value in s_sub):
                for idx, value in zip(p_set, s_sub):
                    w[idx] = value
                break

            # 沿 w -> s 的可行线段走到第一个触零分量。
            alpha = 1.0
            for idx, value in zip(p_set, s_sub):
                if value <= tol:
                    denom = w[idx] - value
                    candidate = w[idx] / denom if denom != 0 else 0.0
                    if candidate < alpha:
                        alpha = candidate
            moved = []
            for pos, (idx, value) in enumerate(zip(p_set, s_sub)):
                w[idx] = w[idx] + alpha * (value - w[idx])
                if value <= tol and abs(w[idx]) <= tol * 10.0:
                    moved.append(idx)
            for idx in moved:
                w[idx] = 0.0
                p_set.remove(idx)
                r_set.append(idx)
            r_set.sort()
            if not p_set:
                break
    return w, iterations, tol


def node_variances(tree, epsilon_by_depth):
    """var_v = 2 / epsilon_depth(v)^2，按公开节点顺序返回。"""
    if len(epsilon_by_depth) != tree.max_depth + 1:
        raise SolverError("epsilon_by_depth 必须与树各层一一对应")
    variances = {}
    for depth, eps in enumerate(epsilon_by_depth):
        if not isinstance(eps, Fraction) or eps <= 0:
            raise SolverError("每层 epsilon 必须是正有理数")
        var = 2.0 / float(eps * eps)
        for nid in tree.nodes_by_depth[depth]:
            variances[nid] = var
    return variances


def reconcile(tree, noisy, epsilon_by_depth, tolerance=1e-8, max_iterations=10000):
    """返回一致化结果包：连续实数计数、目标和、可重算残差。"""
    nodes = tree.ids
    leaves = tree.leaves
    y = [float(noisy[nid]) for nid in nodes]
    variances = node_variances(tree, epsilon_by_depth)
    weights = [1.0 / variances[nid] for nid in nodes]

    rows = len(nodes)
    cols = len(leaves)
    leaf_index = {leaf: j for j, leaf in enumerate(leaves)}
    a = [[0.0] * cols for _ in range(rows)]
    for i, nid in enumerate(nodes):
        for leaf in tree.descendant_leaves[nid]:
            a[i][leaf_index[leaf]] = 1.0

    # B = W^{1/2} A，z = W^{1/2} y；正规方程 C w = d。
    c = [[0.0] * cols for _ in range(cols)]
    d = [0.0] * cols
    for i in range(rows):
        sqrt_w = math.sqrt(weights[i])
        for j in range(cols):
            if a[i][j]:
                d[j] += sqrt_w * (sqrt_w * y[i])
                for k in range(cols):
                    if a[i][k]:
                        c[j][k] += weights[i]

    w, iterations, tol = _lawson_hanson(c, d, tolerance, max_iterations)
    x_values = [
        sum(a[i][j] * w[j] for j in range(cols)) for i in range(rows)
    ]
    x = dict(zip(nodes, x_values))

    objective = weighted_objective(noisy, x, variances)
    diagnostics = residuals(tree, noisy, x, variances, w, c, d)
    return {
        "consistent_counts": x,
        "leaf_weights": dict(zip(leaves, w)),
        "objective": objective,
        "variance": variances,
        "iterations": iterations,
        "solver_tolerance": tol,
        **diagnostics,
    }


def weighted_objective(noisy, consistent, variances):
    """sum (x_v - y_v)^2 / var_v，发布后可用回执数值独立重算。"""
    return sum(
        (float(consistent[nid]) - float(noisy[nid])) ** 2 / variances[nid]
        for nid in variances
    )


def residuals(tree, noisy, consistent, variances, leaf_weights=None,
              normal_c=None, normal_d=None):
    """最优性残差（KKT）与父子相加约束残差，均只依赖已加噪/一致化数值。"""
    leaves = tree.leaves
    nodes = tree.ids
    if leaf_weights is None:
        leaf_weights = [consistent[leaf] for leaf in leaves]

    if normal_c is None:
        # 由公开树与回执中的方差重建正规方程，便于外部独立重算。
        cols = len(leaves)
        leaf_index = {leaf: j for j, leaf in enumerate(leaves)}
        normal_c = [[0.0] * cols for _ in range(cols)]
        normal_d = [0.0] * cols
        for i, nid in enumerate(nodes):
            weight = 1.0 / variances[nid]
            below = [leaf_index[leaf]
                     for leaf in tree.descendant_leaves[nid]]
            for j in below:
                normal_d[j] += weight * float(noisy[nid])
                for k in below:
                    normal_c[j][k] += weight

    # KKT：w>0 要求梯度为零；w==0 要求负梯度不超过零。
    gradient = [
        sum(normal_c[j][k] * leaf_weights[k] for k in range(len(leaves)))
        - normal_d[j]
        for j in range(len(leaves))
    ]
    active_gradient = max(
        (abs(gradient[j]) for j, w in enumerate(leaf_weights) if w > 0.0),
        default=0.0,
    )
    inactive_violation = max(
        (-gradient[j] for j, w in enumerate(leaf_weights) if w <= 0.0),
        default=0.0,
    )
    kkt_residual = max(active_gradient, max(inactive_violation, 0.0))

    constraint_residual = 0.0
    for nid in nodes:
        if tree.children[nid]:
            gap = abs(consistent[nid]
                      - sum(consistent[child] for child in tree.children[nid]))
            constraint_residual = max(constraint_residual, gap)
    negative_leaf = min((consistent[leaf] for leaf in leaves), default=0.0)
    return {
        "kkt_residual": kkt_residual,
        "constraint_residual": constraint_residual,
        "min_leaf_count": negative_leaf,
    }
