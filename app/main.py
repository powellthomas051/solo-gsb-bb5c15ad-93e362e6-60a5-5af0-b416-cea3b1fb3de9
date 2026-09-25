"""陶艺工作室共窑排程 API。

POST /plans                              提交器物与容量，求解、保存并返回方案
POST /plans/compare                      比较两份已保存方案的窑次对应关系
GET  /plans/{plan_id}                    按 ID 读取已保存方案
POST /plans/{plan_id}/capacity-target    求使最少窑次不超过目标的最小单窑容量
POST /plans/{plan_id}/production-limit   限窑次接单：在最多 max_kilns 窑内择优保留器物
POST /plans/{plan_id}/append-items       追加器物：填入原窑或追加窑次，另存为新方案
POST /plans/{plan_id}/cancel-items       取消器物：紧凑重排，另存为新方案
POST /plans/{plan_id}/glaze-separation   釉色隔离：提交互斥对，只读重求最少窑次建议
POST /plans/{plan_id}/kilns/{index}/temperature-check
                                           窑次温控核验：求落在合格温区内的最长连续时段
POST /plans/{plan_id}/firing-order       单台实体窑点火排程：全部批次不逾期的最早整体完工
GET  /health                             健康检查
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import database
from .firing import (
    FiringBatch,
    NoFeasibleFiringOrderError,
    earliest_firing_schedule,
)
from .schemas import (
    AppendItemsIn,
    CancelItemsIn,
    CancelItemsOut,
    CapacityTargetIn,
    CapacityTargetOut,
    ErrorOut,
    FiringBatchOut,
    FiringOrderIn,
    FiringOrderOut,
    GlazeSeparationIn,
    GlazeSeparationOut,
    KilnOut,
    KilnPairOut,
    PlanCompareIn,
    PlanCompareOut,
    PlanCreate,
    PlanOut,
    ProductionLimitIn,
    ProductionLimitOut,
    TemperatureCheckIn,
    TemperatureCheckOut,
)
from .solver import (
    CapacityExceededError,
    TargetUnreachableError,
    Ware,
    append_items,
    cancel_items_and_repack,
    compare_kiln_assignments,
    min_capacity_for_target,
    select_within_kiln_limit,
    solve,
)
from .tempcheck import format_fraction, longest_qualified_period

app = FastAPI(
    title="陶艺工作室共窑排程 API",
    version="1.0.0",
    description="基于位掩码动态规划的最少窑次装窑排程服务（器物 1~14 件）",
)


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


@app.exception_handler(CapacityExceededError)
def _capacity_exceeded_handler(_: Request, exc: CapacityExceededError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "ITEM_EXCEEDS_CAPACITY", "message": str(exc)},
    )


@app.exception_handler(TargetUnreachableError)
def _target_unreachable_handler(_: Request, exc: TargetUnreachableError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "TARGET_UNREACHABLE", "message": str(exc)},
    )


@app.exception_handler(NoFeasibleFiringOrderError)
def _no_feasible_firing_order_handler(
    _: Request, exc: NoFeasibleFiringOrderError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "NO_FEASIBLE_FIRING_ORDER", "message": str(exc)},
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/plans",
    response_model=PlanOut,
    status_code=201,
    tags=["plans"],
    responses={400: {"model": ErrorOut, "description": "单件器物超过单窑容量"}},
    summary="创建共窑排程方案",
)
def create_plan(payload: PlanCreate) -> PlanOut:
    wares = [
        Ware(
            item_id=it.item_id,
            volume=it.volume,
            temp_low=it.temp_low,
            temp_high=it.temp_high,
        )
        for it in payload.items
    ]

    # 单件超容量时 solver 抛出 CapacityExceededError，由全局处理器返回 400。
    kilns = solve(wares, payload.capacity)

    plan = {
        "id": uuid.uuid4().hex,
        "capacity": payload.capacity,
        "item_count": len(wares),
        "kiln_count": len(kilns),
        "items": [it.model_dump() for it in payload.items],
        "kilns": [
            {
                "index": idx,
                "item_ids": list(k.item_ids),
                "total_volume": k.total_volume,
                "firing_temp": k.firing_temp,
            }
            for idx, k in enumerate(kilns, start=1)
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    database.save_plan(plan)
    return PlanOut(**plan)


@app.post(
    "/plans/compare",
    response_model=PlanCompareOut,
    tags=["plans"],
    responses={404: {"model": ErrorOut, "description": "任一方案不存在"}},
    summary="比较两份已保存的排程方案",
)
def compare_plans(payload: PlanCompareIn) -> PlanCompareOut:
    source = database.get_plan(payload.source_plan_id)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到来源排程方案: {payload.source_plan_id}",
            },
        )
    target = database.get_plan(payload.target_plan_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到目标排程方案: {payload.target_plan_id}",
            },
        )

    # 只读计算：以两份方案保存的窑次明细为准，不改写任一方案。
    result = compare_kiln_assignments(
        [k["item_ids"] for k in sorted(source["kilns"], key=lambda k: k["index"])],
        [k["item_ids"] for k in sorted(target["kilns"], key=lambda k: k["index"])],
    )

    return PlanCompareOut(
        source_plan_id=source["id"],
        target_plan_id=target["id"],
        common_count=len(result.stayed_item_ids) + len(result.moved_item_ids),
        pair_count=len(result.pairs),
        kiln_pairs=[
            KilnPairOut(source_index=s, target_index=t) for s, t in result.pairs
        ],
        stayed_item_ids=list(result.stayed_item_ids),
        moved_item_ids=list(result.moved_item_ids),
        only_in_source_item_ids=list(result.only_in_source_item_ids),
        only_in_target_item_ids=list(result.only_in_target_item_ids),
    )


@app.get(
    "/plans/{plan_id}",
    response_model=PlanOut,
    tags=["plans"],
    responses={404: {"model": ErrorOut, "description": "方案不存在"}},
    summary="按 ID 读取排程方案",
)
def read_plan(plan_id: str) -> PlanOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )
    return PlanOut(**plan)


@app.post(
    "/plans/{plan_id}/capacity-target",
    response_model=CapacityTargetOut,
    tags=["plans"],
    responses={
        400: {"model": ErrorOut, "description": "目标窑次超出范围或总体积容量下仍不可达"},
        404: {"model": ErrorOut, "description": "方案不存在"},
    },
    summary="求满足目标窑次的最低单窑容量",
)
def capacity_target(plan_id: str, payload: CapacityTargetIn) -> CapacityTargetOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    target = payload.target_kiln_count
    if target > plan["item_count"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "TARGET_OUT_OF_RANGE",
                "message": (
                    f"目标窑次 {target} 超出范围: "
                    f"原方案共 {plan['item_count']} 件器物，"
                    f"target_kiln_count 须在 1～{plan['item_count']} 之间"
                ),
            },
        )

    # 以原方案保存的器物体积与温度区间为准重新求解，不改写原方案。
    wares = [
        Ware(
            item_id=it["item_id"],
            volume=it["volume"],
            temp_low=it["temp_low"],
            temp_high=it["temp_high"],
        )
        for it in plan["items"]
    ]

    # 总体积容量下仍不可达时 solver 抛出 TargetUnreachableError，
    # 由全局处理器返回 400。
    min_capacity, kilns = min_capacity_for_target(wares, target)

    return CapacityTargetOut(
        plan_id=plan["id"],
        target_kiln_count=target,
        min_capacity=min_capacity,
        kiln_count=len(kilns),
        kilns=[
            KilnOut(
                index=idx,
                item_ids=list(k.item_ids),
                total_volume=k.total_volume,
                firing_temp=k.firing_temp,
            )
            for idx, k in enumerate(kilns, start=1)
        ],
    )


@app.post(
    "/plans/{plan_id}/production-limit",
    response_model=ProductionLimitOut,
    tags=["plans"],
    responses={
        400: {"model": ErrorOut, "description": "窑次上限超出范围"},
        404: {"model": ErrorOut, "description": "方案不存在"},
    },
    summary="限窑次接单：在最多 max_kilns 窑内择优保留器物",
)
def production_limit(plan_id: str, payload: ProductionLimitIn) -> ProductionLimitOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    max_kilns = payload.max_kilns
    if max_kilns > plan["item_count"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "MAX_KILNS_OUT_OF_RANGE",
                "message": (
                    f"窑次上限 {max_kilns} 超出范围: "
                    f"原方案共 {plan['item_count']} 件器物，"
                    f"max_kilns 须在 1～{plan['item_count']} 之间"
                ),
            },
        )

    # 以原方案保存的单窑容量、器物体积与温度区间计算，不改写原方案。
    wares = [
        Ware(
            item_id=it["item_id"],
            volume=it["volume"],
            temp_low=it["temp_low"],
            temp_high=it["temp_high"],
        )
        for it in plan["items"]
    ]
    result = select_within_kiln_limit(wares, plan["capacity"], max_kilns)

    return ProductionLimitOut(
        plan_id=plan["id"],
        max_kilns=max_kilns,
        kept_count=len(result.kept_item_ids),
        delayed_count=len(result.delayed_item_ids),
        kept_volume=sum(k.total_volume for k in result.kilns),
        kept_item_ids=list(result.kept_item_ids),
        delayed_item_ids=list(result.delayed_item_ids),
        kiln_count=len(result.kilns),
        kilns=[
            KilnOut(
                index=idx,
                item_ids=list(k.item_ids),
                total_volume=k.total_volume,
                firing_temp=k.firing_temp,
            )
            for idx, k in enumerate(result.kilns, start=1)
        ],
    )


@app.post(
    "/plans/{plan_id}/append-items",
    response_model=PlanOut,
    status_code=201,
    tags=["plans"],
    responses={
        400: {
            "model": ErrorOut,
            "description": "新编号与原方案重复 / 合并后超过 14 件 / 单件超容量",
        },
        404: {"model": ErrorOut, "description": "方案不存在"},
    },
    summary="追加器物：填入原窑或追加窑次，另存为新方案",
)
def append_plan_items(plan_id: str, payload: AppendItemsIn) -> PlanOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    original_ids = {it["item_id"] for it in plan["items"]}
    dup = sorted({it.item_id for it in payload.items} & original_ids)
    if dup:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "DUPLICATE_ITEM_ID",
                "message": f"新器物编号与原方案重复: {', '.join(dup)}",
            },
        )

    merged_count = plan["item_count"] + len(payload.items)
    if merged_count > 14:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "ITEM_COUNT_EXCEEDS_LIMIT",
                "message": (
                    f"合并后共 {merged_count} 件器物，超过 14 件上限: "
                    f"原方案 {plan['item_count']} 件 + 新增 {len(payload.items)} 件"
                ),
            },
        )

    # 以原方案保存的单窑容量、器物体积与温度区间计算，不改写原方案；
    # 原窑按保存的窑次次序传入，结果作为新方案保存。
    by_id = {
        it["item_id"]: Ware(
            item_id=it["item_id"],
            volume=it["volume"],
            temp_low=it["temp_low"],
            temp_high=it["temp_high"],
        )
        for it in plan["items"]
    }
    original_kilns = [
        [by_id[i] for i in k["item_ids"]]
        for k in sorted(plan["kilns"], key=lambda k: k["index"])
    ]
    new_wares = [
        Ware(
            item_id=it.item_id,
            volume=it.volume,
            temp_low=it.temp_low,
            temp_high=it.temp_high,
        )
        for it in payload.items
    ]

    # 新器物单件超容量时 solver 抛出 CapacityExceededError，由全局处理器返回 400。
    kilns = append_items(original_kilns, new_wares, plan["capacity"])

    new_plan = {
        "id": uuid.uuid4().hex,
        "capacity": plan["capacity"],
        "item_count": merged_count,
        "kiln_count": len(kilns),
        "items": [*plan["items"], *(it.model_dump() for it in payload.items)],
        "kilns": [
            {
                "index": idx,
                "item_ids": list(k.item_ids),
                "total_volume": k.total_volume,
                "firing_temp": k.firing_temp,
            }
            for idx, k in enumerate(kilns, start=1)
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    database.save_plan(new_plan)
    return PlanOut(**new_plan)


@app.post(
    "/plans/{plan_id}/cancel-items",
    response_model=CancelItemsOut,
    status_code=201,
    tags=["plans"],
    responses={
        400: {
            "model": ErrorOut,
            "description": "编号不属于原方案 / 取消后无剩余器物",
        },
        404: {"model": ErrorOut, "description": "方案不存在"},
    },
    summary="取消器物：紧凑重排，另存为新方案",
)
def cancel_plan_items(plan_id: str, payload: CancelItemsIn) -> CancelItemsOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    original_ids = {it["item_id"] for it in plan["items"]}
    cancel_ids = payload.item_ids  # 非空 / 去空白 / 不重复已在请求模型（422）校验
    unknown = sorted(set(cancel_ids) - original_ids)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "UNKNOWN_ITEM_ID",
                "message": f"以下器物编号不属于原方案: {', '.join(unknown)}",
            },
        )

    if len(set(cancel_ids)) >= plan["item_count"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "CANCEL_ALL_ITEMS",
                "message": (
                    f"原方案共 {plan['item_count']} 件器物，全部取消后无剩余器物: "
                    "取消后至少需要保留一件"
                ),
            },
        )

    # 以原方案保存的单窑容量、器物体积与温度区间重新紧凑排程，不改写原方案；
    # 原窑按保存的窑次次序传入，用于新旧窑次一对一配对的留窑 / 换窑统计。
    by_id = {
        it["item_id"]: Ware(
            item_id=it["item_id"],
            volume=it["volume"],
            temp_low=it["temp_low"],
            temp_high=it["temp_high"],
        )
        for it in plan["items"]
    }
    original_kilns = [
        [i for i in k["item_ids"]]
        for k in sorted(plan["kilns"], key=lambda k: k["index"])
    ]

    result = cancel_items_and_repack(
        [by_id[i] for i in original_ids],
        plan["capacity"],
        original_kilns,
        cancel_ids,
    )

    cancel_set = set(result.canceled_item_ids)
    remaining_items = [it for it in plan["items"] if it["item_id"] not in cancel_set]
    new_plan = {
        "id": uuid.uuid4().hex,
        "capacity": plan["capacity"],
        "item_count": len(remaining_items),
        "kiln_count": len(result.kilns),
        "items": remaining_items,
        "kilns": [
            {
                "index": idx,
                "item_ids": list(k.item_ids),
                "total_volume": k.total_volume,
                "firing_temp": k.firing_temp,
            }
            for idx, k in enumerate(result.kilns, start=1)
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    database.save_plan(new_plan)

    return CancelItemsOut(
        source_plan_id=plan["id"],
        new_plan_id=new_plan["id"],
        canceled_item_ids=list(result.canceled_item_ids),
        canceled_count=len(result.canceled_item_ids),
        stayed_item_ids=list(result.stayed_item_ids),
        moved_item_ids=list(result.moved_item_ids),
        stayed_count=len(result.stayed_item_ids),
        moved_count=len(result.moved_item_ids),
        kiln_pairs=[
            KilnPairOut(source_index=s, target_index=t) for s, t in result.pairs
        ],
        new_plan=PlanOut(**new_plan),
    )


@app.post(
    "/plans/{plan_id}/glaze-separation",
    response_model=GlazeSeparationOut,
    tags=["plans"],
    responses={
        400: {"model": ErrorOut, "description": "互斥对含不属于原方案的编号"},
        404: {"model": ErrorOut, "description": "方案不存在"},
    },
    summary="釉色隔离：按器物互斥对只读重求最少窑次建议",
)
def glaze_separation(
    plan_id: str, payload: GlazeSeparationIn
) -> GlazeSeparationOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    original_ids = {it["item_id"] for it in plan["items"]}
    unknown = sorted(
        {x for pair in payload.separation_pairs for x in pair} - original_ids
    )
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "UNKNOWN_ITEM_ID",
                "message": f"以下器物编号不属于原方案: {', '.join(unknown)}",
            },
        )

    # 沿用原方案保存的单窑容量、器物体积与温度区间重新求解；
    # 互斥约束并入子集可行性，只读计算，不改写或另存原方案。
    wares = [
        Ware(
            item_id=it["item_id"],
            volume=it["volume"],
            temp_low=it["temp_low"],
            temp_high=it["temp_high"],
        )
        for it in plan["items"]
    ]
    kilns = solve(
        wares,
        plan["capacity"],
        separation_pairs=payload.separation_pairs,
    )

    return GlazeSeparationOut(
        plan_id=plan["id"],
        separation_pair_count=len(payload.separation_pairs),
        kiln_count=len(kilns),
        kilns=[
            KilnOut(
                index=idx,
                item_ids=list(k.item_ids),
                total_volume=k.total_volume,
                firing_temp=k.firing_temp,
            )
            for idx, k in enumerate(kilns, start=1)
        ],
    )


@app.post(
    "/plans/{plan_id}/kilns/{index}/temperature-check",
    response_model=TemperatureCheckOut,
    tags=["plans"],
    responses={404: {"model": ErrorOut, "description": "方案或窑次不存在"}},
    summary="窑次温控核验：求落在合格温区内的最长连续时段",
)
def temperature_check(
    plan_id: str, index: int, payload: TemperatureCheckIn
) -> TemperatureCheckOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    kilns = sorted(plan["kilns"], key=lambda k: k["index"])
    if not 1 <= index <= len(kilns):
        raise HTTPException(
            status_code=404,
            detail={
                "error": "KILN_NOT_FOUND",
                "message": (
                    f"方案 {plan_id} 共 {len(kilns)} 个窑次，"
                    f"窑次序号须为 1～{len(kilns)}: {index}"
                ),
            },
        )
    kiln = kilns[index - 1]

    # 以方案保存的该窑器物温区交集为合格温区，只读计算，不改写原方案。
    by_id = {it["item_id"]: it for it in plan["items"]}
    temp_low = max(by_id[i]["temp_low"] for i in kiln["item_ids"])
    temp_high = min(by_id[i]["temp_high"] for i in kiln["item_ids"])

    result = longest_qualified_period(
        [(s.time, s.temperature) for s in payload.samples],
        temp_low,
        temp_high,
        payload.min_hold_seconds,
        payload.max_interval_seconds,
    )

    return TemperatureCheckOut(
        plan_id=plan["id"],
        kiln_index=kiln["index"],
        item_ids=list(kiln["item_ids"]),
        temp_low=temp_low,
        temp_high=temp_high,
        min_hold_seconds=payload.min_hold_seconds,
        max_interval_seconds=payload.max_interval_seconds,
        start=format_fraction(result.start),
        end=format_fraction(result.end),
        duration_seconds=format_fraction(result.duration),
        meets_requirement=result.meets_requirement,
    )


@app.post(
    "/plans/{plan_id}/firing-order",
    response_model=FiringOrderOut,
    tags=["plans"],
    responses={
        400: {"model": ErrorOut, "description": "窑次未恰好覆盖 / 无可行点火顺序"},
        404: {"model": ErrorOut, "description": "方案不存在"},
    },
    summary="单台实体窑点火排程：全部批次不逾期的最早整体完工",
)
def firing_order(plan_id: str, payload: FiringOrderIn) -> FiringOrderOut:
    plan = database.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PLAN_NOT_FOUND",
                "message": f"未找到排程方案: {plan_id}",
            },
        )

    kiln_count = plan["kiln_count"]
    unknown = sorted(
        {
            b.kiln_index
            for b in payload.batches
            if not 1 <= b.kiln_index <= kiln_count
        }
    )
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "UNKNOWN_KILN_INDEX",
                "message": (
                    f"方案 {plan_id} 共 {kiln_count} 个窑次，窑次序号须为 "
                    f"1～{kiln_count}: {', '.join(str(i) for i in unknown)}"
                ),
            },
        )
    covered = {b.kiln_index for b in payload.batches}
    missing = sorted(set(range(1, kiln_count + 1)) - covered)
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "MISSING_KILN_BATCH",
                "message": (
                    f"必须恰好覆盖全部 {kiln_count} 个窑次，缺少窑次: "
                    + ", ".join(str(i) for i in missing)
                ),
            },
        )

    # 只读计算：以方案保存的窑次明细带出各批器物编号，不改写原方案。
    # 无可行顺序时 firing 模块抛出 NoFeasibleFiringOrderError，
    # 由全局处理器返回 400。
    items_by_index = {k["index"]: k["item_ids"] for k in plan["kilns"]}
    constraint_by_index = {b.kiln_index: b for b in payload.batches}
    schedule = earliest_firing_schedule(
        [
            FiringBatch(
                kiln_index=b.kiln_index,
                earliest_start=b.earliest_start_minute,
                duration=b.duration_minutes,
                latest_completion=b.latest_completion_minute,
            )
            for b in payload.batches
        ]
    )

    batches_out: list[FiringBatchOut] = []
    for pos, b in enumerate(schedule.batches, start=1):
        c = constraint_by_index[b.kiln_index]
        batches_out.append(
            FiringBatchOut(
                position=pos,
                kiln_index=b.kiln_index,
                item_ids=list(items_by_index[b.kiln_index]),
                earliest_start_minute=c.earliest_start_minute,
                duration_minutes=c.duration_minutes,
                latest_completion_minute=c.latest_completion_minute,
                start_minute=b.start,
                completion_minute=b.completion,
            )
        )

    return FiringOrderOut(
        plan_id=plan["id"],
        kiln_count=kiln_count,
        makespan_minute=schedule.makespan,
        order=[b.kiln_index for b in schedule.batches],
        batches=batches_out,
    )
