"""firing 单元测试：固定用例 + 小规模暴力枚举交叉验证。"""

import os
import random
import sys
import time
from itertools import permutations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.firing import (  # noqa: E402
    FiringBatchInput,
    FiringOrderInfeasibleError,
    schedule_firing_order,
)


def _brute_force(windows):
    """枚举全部 n! 个点火顺序，返回 (最早整体完工, 字典序最小的窑次序号序列)。

    windows: [(kiln_index, release, firing, deadline)]。
    无可行顺序返回 None。
    """
    best = None
    for perm in permutations(windows):
        current = 0
        feasible = True
        for _, release, firing, deadline in perm:
            finish = max(current, release) + firing
            if finish > deadline:
                feasible = False
                break
            current = finish
        if feasible:
            order = tuple(w[0] for w in perm)
            key = (current, order)
            if best is None or key < best:
                best = key
    return best


def _run(windows):
    batches = [
        FiringBatchInput(
            kiln_index=idx,
            release_minute=r,
            firing_minutes=p,
            deadline_minute=d,
        )
        for idx, r, p, d in windows
    ]
    return schedule_firing_order(batches)


def _check_random(trials, seed):
    rng = random.Random(seed)
    for trial in range(trials):
        n = rng.randint(1, 7)
        windows = []
        for idx in range(1, n + 1):
            r = rng.randint(0, 12)
            p = rng.randint(1, 6)
            d = r + p + rng.randint(0, 10)  # 含大量不可行窗口
            windows.append((idx, r, p, d))

        expected = _brute_force(windows)
        if expected is None:
            try:
                _run(windows)
                assert False, f"trial {trial}: 应抛出 FiringOrderInfeasibleError"
            except FiringOrderInfeasibleError:
                pass
            continue

        result = _run(windows)
        order = tuple(s.kiln_index for s in result.slots)
        assert result.makespan_minute == expected[0], (
            f"trial {trial}: makespan {result.makespan_minute} != {expected[0]}, "
            f"windows={windows}"
        )
        assert order == expected[1], (
            f"trial {trial}: order {order} != {expected[1]}, windows={windows}"
        )

        # 时间线基本不变式
        assert len(result.slots) == n
        assert [s.order for s in result.slots] == list(range(1, n + 1))
        assert sorted(order) == [w[0] for w in windows]
        by_index = {idx: (r, p, d) for idx, r, p, d in windows}
        previous_finish = 0
        for slot in result.slots:
            r, p, d = by_index[slot.kiln_index]
            assert slot.start_minute >= r  # 不早于最早可点火
            assert slot.start_minute >= previous_finish  # 单台实体窑不并行
            assert slot.finish_minute == slot.start_minute + p
            assert slot.finish_minute <= d  # 不逾期
            previous_finish = slot.finish_minute
        assert result.makespan_minute == result.slots[-1].finish_minute


def test_single_batch():
    # 0 点火、5 完成
    result = _run([(1, 0, 5, 10)])
    assert result.makespan_minute == 5
    assert len(result.slots) == 1
    slot = result.slots[0]
    assert (slot.order, slot.kiln_index, slot.start_minute, slot.finish_minute) == (
        1, 1, 0, 5
    )

    # 最早可点火晚于起点：实体窑空闲等待到第 7 分钟
    result = _run([(1, 7, 3, 10)])
    assert result.makespan_minute == 10
    assert (result.slots[0].start_minute, result.slots[0].finish_minute) == (7, 10)

    # 自身窗口容不下烧成时长：r + p > d
    try:
        _run([(1, 8, 3, 10)])
        assert False, "应抛出 FiringOrderInfeasibleError"
    except FiringOrderInfeasibleError:
        pass


def test_no_waiting_runs_back_to_back():
    # 全部可在起点点火且窗口宽松：按窑次序号 1->2->3 紧排，无空闲
    result = _run([(1, 0, 10, 100), (2, 0, 8, 100), (3, 0, 5, 100)])
    assert result.makespan_minute == 23
    assert [
        (s.kiln_index, s.start_minute, s.finish_minute) for s in result.slots
    ] == [(1, 0, 10), (2, 10, 18), (3, 18, 23)]


def test_release_forces_reorder():
    # 窑 1 最早第 20 分钟才能点火：先烧 2、3 并等待，最优顺序 2->3->1，完工 30
    result = _run([(1, 20, 10, 100), (2, 0, 8, 100), (3, 0, 5, 100)])
    assert result.makespan_minute == 30
    assert [
        (s.kiln_index, s.start_minute, s.finish_minute) for s in result.slots
    ] == [(2, 0, 8), (3, 8, 13), (1, 20, 30)]


def test_deadline_forces_reorder():
    # 序号升序顺序 1->2 会逾期（窑2须在9前完成）；唯一可行顺序 2->1
    result = _run([(1, 0, 10, 100), (2, 0, 5, 9)])
    assert result.makespan_minute == 15
    assert [
        (s.kiln_index, s.start_minute, s.finish_minute) for s in result.slots
    ] == [(2, 0, 5), (1, 5, 15)]


def test_total_processing_exceeds_all_deadlines_infeasible():
    # 总烧成 23 分钟，但每批最迟完成均不超过 20 -> 任何顺序都逾期
    try:
        _run([(1, 0, 10, 20), (2, 0, 8, 20), (3, 0, 5, 20)])
        assert False, "应抛出 FiringOrderInfeasibleError"
    except FiringOrderInfeasibleError:
        pass


def test_tight_window_boundary_is_feasible():
    # 完成时刻恰好等于最迟完成分钟（闭区间，不逾期）
    result = _run([(1, 0, 5, 5)])
    assert result.makespan_minute == 5
    assert result.slots[0].finish_minute == 5


def test_lexicographically_smallest_among_optimal_orders():
    # 三个批次完全对称：6 个顺序同为最早完工 15，取序号序列字典序最小 1->2->3
    result = _run([(1, 0, 5, 100), (2, 0, 5, 100), (3, 0, 5, 100)])
    assert result.makespan_minute == 15
    assert tuple(s.kiln_index for s in result.slots) == (1, 2, 3)


def test_kiln_indices_need_not_be_contiguous():
    # 窑次序号只用于标识与字典序定序，允许非连续（按已保存方案实际序号提交）
    result = _run([(2, 0, 3, 100), (5, 0, 4, 100)])
    assert tuple(s.kiln_index for s in result.slots) == (2, 5)
    assert result.makespan_minute == 7


def test_lex_smallest_optimal_order_non_prefix_case():
    # 释放时刻使「序号最小前缀完工最早」的贪心假象失效：
    # 顺序 2->1->3 与 1->3->2 均可行且完工 13；字典序最小最优为 1->3->2，
    # 但前缀 {1} 的最早完工（2）出现在顺序 1->2->3（逾期）中，故重建必须
    # 借助后缀可扩展性而非仅看前缀最早完工。
    windows = [(1, 1, 1, 8), (2, 8, 5, 18), (3, 5, 1, 8)]
    result = _run(windows)
    assert result.makespan_minute == 13
    assert tuple(s.kiln_index for s in result.slots) == (1, 3, 2)
    assert [
        (s.kiln_index, s.start_minute, s.finish_minute) for s in result.slots
    ] == [(1, 1, 2), (3, 5, 6), (2, 8, 13)]


def test_invalid_batch_list():
    for bad in ([], [
        FiringBatchInput(kiln_index=i, release_minute=0, firing_minutes=1,
                         deadline_minute=10)
        for i in range(1, 16)
    ]):
        try:
            schedule_firing_order(bad)
            assert False, f"{len(bad)} 批: 应抛出 ValueError"
        except ValueError:
            pass

    try:
        schedule_firing_order([
            FiringBatchInput(1, 0, 1, 10),
            FiringBatchInput(1, 0, 1, 10),
        ])
        assert False, "重复窑次序号应抛出 ValueError"
    except ValueError:
        pass


def test_random_brute_force():
    _check_random(3000, seed=123)


def test_n14_performance_and_correctness():
    # n=14 宽松窗口：1->14 紧排，耗时可控
    windows = [(i, 0, 1, 1000) for i in range(1, 15)]
    t0 = time.time()
    result = _run(windows)
    elapsed = time.time() - t0
    assert result.makespan_minute == 14
    assert tuple(s.kiln_index for s in result.slots) == tuple(range(1, 15))
    assert elapsed < 10.0, f"n=14 宽松场景耗时 {elapsed:.2f}s"

    # n=14 随机紧窗口（每步可能枚举多个候选并做后缀 DP），仍需在数秒内返回
    rng = random.Random(7)
    worst = 0.0
    for _ in range(10):
        windows = [
            (
                i,
                rng.randint(0, 30),
                rng.randint(1, 4),
                rng.randint(30, 80),
            )
            for i in range(1, 15)
        ]
        t0 = time.time()
        try:
            result = _run(windows)
            assert result.makespan_minute <= 80
        except FiringOrderInfeasibleError:
            pass
        worst = max(worst, time.time() - t0)
    assert worst < 10.0, f"n=14 紧窗口场景耗时 {worst:.2f}s"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 个测试通过")
