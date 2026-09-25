"""单台实体窑点火排程：单机、批处理不可并行、允许空闲，最早整体完工。

每批（即已保存方案的一个窑次）给出三个非负 / 正整数分钟数，均从同一
排程起点 0 起算：

  * release  r_j：最早可点火分钟（未到点可令实体窑空闲等待）；
  * process  p_j：烧成时长（正整数，点火后连续占用实体窑 p_j 分钟）；
  * deadline d_j：最迟完成分钟（完成时刻 c_j 必须满足 c_j ≤ d_j）。

同一时刻实体窑至多烧一批；目标是在全部批次都不逾期的前提下，使**整体
完工时刻（最后一批完成时刻）最早**，并返回各批的点火 / 完成时刻与
点火顺序。这是经典单机排程：批次带释放时刻与截止时刻（记法
``1|r_j|L_max ≤ 0`` 的可行性，并最小化 ``C_max``），一般情形为 NP 难；
窑次数至多 14，用位掩码 DP 精确求解。

算法（记批次数为 n，位 j 对应排序后的第 j 批）：

  1. 前向位掩码 DP 求每个批次集合从起点 0 起排的**最早完工时刻**
     ``dp[mask]``（已完成集合恰为 mask 时实体窑的最早空闲时刻）：

         dp[0] = 0
         dp[mask] = min over j in mask:
             c = max(dp[mask \\ {j}], r_j) + p_j，仅当 c ≤ d_j

     不可行状态记 +∞。``dp[全集]`` 为 +∞ 即无任何不逾期的点火顺序。

  2. ``dp[全集]`` 即全局最早整体完工时刻 C*（最优解中最后一批的完成时刻
     恒等于实体窑最早空闲时刻）。逐批贪心重建：在已选前缀（实体窑于 t 空闲）
     之后，按批次序号（窑次序号）升序尝试把批次 j 作为下一批，令其于
     s = max(t, r_j) 点火、c = s + p_j 完成；要求 c ≤ d_j，且**其余批次从
     c 起排仍能恰在 C* 完工**（后缀可行性子 DP）。首个满足者即被选中。

     后缀可行性同样是带释放 / 截止时刻的位掩码 DP（把初始空闲时刻由 0
     换成 c）：``f[0]=c``，``f[S]=min max(f[S\\{j}], r_j)+p_j``（c≤d_j），
     ``f[剩余集合] == C*`` 表示该前缀可扩展为一条最优完工顺序。

     每一步总存在可扩展候选（最优顺序本身按位提供），故贪心不会中断；
     序号升序取首个可扩展候选，得到的是全部最早完工顺序中**窑次序号序列
     字典序最小**者（同一步选更小序号且仍可扩展为最优顺序，字典序恒更优）。

分钟数全部为整数且运算仅取 max / 加法，各批点火与完成时刻亦为整数。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# 不可行状态的哨兵值：任何可行时刻都远小于它（分钟数规模有限）。
_INF = 10**18


class FiringOrderInfeasibleError(ValueError):
    """不存在使全部批次均不逾期的点火顺序。"""


@dataclass(frozen=True)
class FiringBatchInput:
    """一批（一个窑次）的排程参数。

    kiln_index 为该批在已保存方案中的窑次序号（从 1 开始）；
    release_minute / deadline_minute 为非负整数，firing_minutes 为正整数，
    分钟均从同一排程起点 0 起算。
    """

    kiln_index: int
    release_minute: int
    firing_minutes: int
    deadline_minute: int


@dataclass(frozen=True)
class FiringSlot:
    """点火顺序中的一个位置：某窑次的点火 / 完成时刻。"""

    order: int
    kiln_index: int
    start_minute: int
    finish_minute: int


@dataclass(frozen=True)
class FiringOrderResult:
    """点火排程结果：全部批次均不逾期时的最早整体完工方案。

    makespan_minute 为最后一批完成时刻（= 整体完工时刻，取最小值）；
    slots 按点火顺序排列，order 从 1 开始，恰好覆盖全部窑次。
    """

    makespan_minute: int
    slots: tuple[FiringSlot, ...]


def _earliest_finish_forward(
    releases: Sequence[int],
    firing: Sequence[int],
    deadlines: Sequence[int],
) -> list[int]:
    """前向位掩码 DP：每个批次集合从起点 0 起排的最早完工时刻；不可行为 +∞。"""

    n = len(releases)
    size = 1 << n
    dp = [_INF] * size
    dp[0] = 0
    for mask in range(1, size):
        best = _INF
        bits = mask
        while bits:
            bit = bits & -bits
            j = bit.bit_length() - 1
            prev = dp[mask ^ bit]
            if prev < _INF:
                finish = max(prev, releases[j]) + firing[j]
                if finish <= deadlines[j] and finish < best:
                    best = finish
            bits ^= bit
        dp[mask] = best
    return dp


def _earliest_suffix_finish(
    subset: int,
    start: int,
    releases: Sequence[int],
    firing: Sequence[int],
    deadlines: Sequence[int],
    table: list[int],
) -> int:
    """子集 subset 中各批次从实体窑于 start 空闲起排的最早完工时刻。

    与前向 DP 同构，仅初值 f[0]=start；只计算 subset 的子集。subset 的任意
    子集数值上都不大于 subset，且掩码升序枚举时 f[sub ^ bit] 的下标严格
    小于 sub（被读到的位置必为 subset 子集且本轮已赋值，无陈旧数据）。
    """

    n = len(releases)
    table[0] = start
    for sub in range(1, subset + 1):
        if sub & ~subset:
            continue
        best = _INF
        bits = sub
        while bits:
            bit = bits & -bits
            j = bit.bit_length() - 1
            prev = table[sub ^ bit]
            if prev < _INF:
                finish = max(prev, releases[j]) + firing[j]
                if finish <= deadlines[j] and finish < best:
                    best = finish
            bits ^= bit
        table[sub] = best
    return table[subset]


def schedule_firing_order(
    batches: Sequence[FiringBatchInput],
) -> FiringOrderResult:
    """求全部批次均不逾期时最早整体完工的点火顺序。

    :param batches: 各窑次的最早可点火 / 烧成时长 / 最迟完成分钟；
        窑次序号必须互不相同。窑次数 1～14，分钟为非负整数、时长为正整数。
    :returns: 最早整体完工时刻与按点火顺序排列的各批点火 / 完成时刻。
    :raises FiringOrderInfeasibleError: 不存在不逾期的点火顺序。
    :raises ValueError: 批次为空 / 超过 14 个，或窑次序号重复。
    """

    n = len(batches)
    if n == 0:
        raise ValueError("至少需要一个窑次批次")
    if n > 14:
        raise ValueError("窑次批次数量不得超过 14 个")

    ordered = sorted(batches, key=lambda b: b.kiln_index)
    if any(
        ordered[i].kiln_index == ordered[i - 1].kiln_index for i in range(1, n)
    ):
        raise ValueError("窑次序号不得重复")

    releases = [b.release_minute for b in ordered]
    firing = [b.firing_minutes for b in ordered]
    deadlines = [b.deadline_minute for b in ordered]
    indices = [b.kiln_index for b in ordered]

    size = 1 << n
    full = size - 1
    dp = _earliest_finish_forward(releases, firing, deadlines)
    makespan = dp[full]
    if makespan >= _INF:
        raise FiringOrderInfeasibleError(
            "不存在使全部窑次均不逾期的点火顺序："
            "单台实体窑无法在各批最迟完成分钟前烧完所有批次"
        )

    # ---- 贪心重建：序号升序取首个仍可扩展为 C* 完工顺序的候选 ----
    table = [_INF] * size
    slots: list[FiringSlot] = []
    used = 0
    current = 0  # 已选前缀结束后实体窑的空闲时刻
    for position in range(1, n + 1):
        remaining = full ^ used
        picked = -1
        finish = 0
        bits = remaining
        while bits:
            bit = bits & -bits
            j = bit.bit_length() - 1  # 序号升序枚举（位 0 即最小窑次序号）
            start = max(current, releases[j])
            candidate_finish = start + firing[j]
            if candidate_finish <= deadlines[j]:
                suffix = _earliest_suffix_finish(
                    remaining ^ bit,
                    candidate_finish,
                    releases,
                    firing,
                    deadlines,
                    table,
                )
                if suffix == makespan:
                    picked = j
                    finish = candidate_finish
                    break
            bits ^= bit

        # 最优顺序按位即为可扩展候选，正常情况下每步必能选中。
        if picked < 0:
            raise FiringOrderInfeasibleError(
                "重建点火顺序失败：不存在使全部窑次均不逾期的点火顺序"
            )

        slots.append(
            FiringSlot(
                order=position,
                kiln_index=indices[picked],
                start_minute=max(current, releases[picked]),
                finish_minute=finish,
            )
        )
        used |= 1 << picked
        current = finish

    return FiringOrderResult(makespan_minute=makespan, slots=tuple(slots))
