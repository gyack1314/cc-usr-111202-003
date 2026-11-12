"""Laplace 机制的噪声源与逐节点加噪。

每个树层向量的 L1 敏感度为 1（相邻数据集只差一个人的全部记录，每人至多一条
入选事件，单层只有一个节点计数变化 1）。按事先登记的正 epsilon_level，对该层
每个节点独立加入尺度 b=1/epsilon_level 的 Laplace 噪声；真计数为零的叶同样加噪。

随机源两类：
- PublicationNoise：默认发布用途，基于 secrets（操作系统 CSPRNG），无种子、不可复现；
- DemoNoise：显式演示模式的固定种子 PRNG，仅用于明确标记、不得对外发布的材料。
两者都按节点的公开顺序逐个抽样，保证同资料下演示结果确定可重算。
"""

import math
import random
import secrets
from fractions import Fraction


class NoiseError(ValueError):
    pass


class _BaseNoise:
    def laplace(self, scale):
        u = self._unit() - 0.5
        # 逆 CDF：-b*sgn(u)*ln(1-2|u|)；|u| 接近 1/2 时做稳健处理。
        z = 1.0 - 2.0 * abs(u)
        if z <= 0.0:
            z = 5e-324
        return -scale * math.copysign(math.log(z), u)


class PublicationNoise(_BaseNoise):
    """发布模式随机源：secrets 提供的操作系统级 CSPRNG，无种子。"""

    mode = "publication"

    def _unit(self):
        return secrets.randbelow(1 << 53) / float(1 << 53)


class DemoNoise(_BaseNoise):
    """演示模式随机源：固定种子，输出必须标记为不可对外发布。"""

    mode = "demonstration"

    def __init__(self, seed):
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise NoiseError("demo_seed 必须是非负整数")
        self.seed = seed
        self._rng = random.Random(seed)

    def _unit(self):
        return self._rng.random()


def make_noise_source(mode, demo_seed=None):
    if mode == "publication":
        if demo_seed is not None:
            raise NoiseError("发布模式不得携带固定种子")
        return PublicationNoise()
    if mode == "demonstration":
        if demo_seed is None:
            raise NoiseError("演示模式必须显式提供 demo_seed")
        return DemoNoise(demo_seed)
    raise NoiseError("mode 只能是 publication 或 demonstration")


def noisy_levels(tree, true_counts, epsilon_by_depth, noise_source):
    """对全部节点（含零叶）按公开顺序加独立 Laplace 噪声。

    epsilon_by_depth 为 Fraction 序列，长度等于树深度数（根深度 0 到叶深度）。
    返回 {node_id: float} 及 {node_id: 尺度 b}。
    """
    if len(epsilon_by_depth) != tree.max_depth + 1:
        raise NoiseError("epsilon_by_depth 必须与树各层一一对应")
    scales = {}
    noisy = {}
    for depth, eps in enumerate(epsilon_by_depth):
        if not isinstance(eps, Fraction) or eps <= 0:
            raise NoiseError("每层 epsilon 必须是事先登记的正有理数")
        scale = 1.0 / float(eps)
        for nid in tree.nodes_by_depth[depth]:
            scales[nid] = scale
            noisy[nid] = float(true_counts[nid]) + noise_source.laplace(scale)
    return noisy, scales
