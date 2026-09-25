# 陶艺工作室共窑排程 API

小型陶艺工作室的「共窑」装窑排程服务：提交 1～14 件器物（唯一编号、正整数体积、
允许烧成温度闭区间）与单窑容量，使用**位掩码动态规划**求出覆盖全部器物的
**最少窑次**方案，并在窑次相同时选择**整体字典序最小**的方案。
已保存的方案支持目标窑次容量反查、限窑次接单、追加器物（填入原窑或追加窑次，
另存为新方案）、**取消器物后的紧凑重排**（另存为新方案）、**釉色隔离**
（提交器物互斥对，只读重求最少窑次建议）、两版方案差异对比、
**窑次温控核验**（按分段线性插值精确求落在该窑合格温区内的最长连续时段），
以及**单台实体窑点火排程**（按各窑次的最早可点火 / 烧成时长 / 最迟完成，
求全部批次均不逾期时的最早整体完工时刻与点火顺序）。

技术栈：Python 3.12 · FastAPI · SQLite · Uvicorn。

## 排程规则

- 同一窑的器物必须同时满足：
  1. **总体积 ≤ 单窑容量**；
  2. 各器物温度区间存在**公共交集**（`max(下界) ≤ min(上界)`）；
  3. 该窑烧成温度取交集下界，即 `max(各器物 temp_low)`；
  4. 提交釉色隔离互斥对时，互斥对中的两件器物亦**不得同窑**。
- 目标：窑次数最少。
- 窑次相同时的字典序规则：
  - 每个窑内器物编号**升序**排列；
  - 再将各窑按其编号序列排序（序列比较按标准字符串字典序，前缀视为更小）；
  - 在所有最少窑次方案中，取排序后整体字典序最小者。
- 任何**单件**器物体积超过单窑容量时无法烧成，返回明确错误
  （HTTP 400，错误码 `ITEM_EXCEEDS_CAPACITY`）。

## 单条命令启动

需要 Docker 20.10+ 与 Docker Compose v2：

```bash
docker compose up --build -d
```

容器启动时会自动：

1. 安装 / 校验 Python 依赖（`requirements.txt`）；
2. 初始化 SQLite 数据库（建表，幂等）；
3. 启动 Uvicorn 服务。

启动后服务监听容器内 `8000` 端口，默认映射到宿主机 **`8000`**
端口（可用环境变量 `KILN_API_PORT` 修改，见下文配置）：

```bash
$ curl -s http://localhost:8000/health
{"status":"ok"}
```

停止：`docker compose down`（加 `-v` 可同时删除数据库卷）。

## 配置

均为可选项，通过环境变量配置（见 `docker-compose.yml`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KILN_API_PORT` | `8000` | 宿主机映射端口 |
| `KILN_DB_PATH` | `/data/kiln.db` | 容器内 SQLite 数据库文件路径 |
| `UVICORN_HOST` | `0.0.0.0` | 服务监听地址 |
| `UVICORN_PORT` | `8000` | 容器内监听端口 |

示例：宿主机改用 18000 端口 —— `KILN_API_PORT=18000 docker compose up --build -d`。
数据库通过命名卷 `kiln-data` 持久化于 `/data`。

## API 地址与端口

默认地址：**http://localhost:8000**

- 交互式文档（Swagger UI）：http://localhost:8000/docs
- OpenAPI 规范：http://localhost:8000/openapi.json
- 健康检查：`GET http://localhost:8000/health`

### 1. 创建排程方案

`POST http://localhost:8000/plans`

请求体：

```json
{
  "capacity": 10,
  "items": [
    {"item_id": "p1", "volume": 5, "temp_low": 100, "temp_high": 200},
    {"item_id": "p2", "volume": 6, "temp_low": 120, "temp_high": 220},
    {"item_id": "p3", "volume": 4, "temp_low": 150, "temp_high": 300},
    {"item_id": "p4", "volume": 5, "temp_low": 1000, "temp_high": 1100}
  ]
}
```

字段约束：`items` 数量 1～14；`item_id` 为非空且唯一的字符串；`volume` 与
`capacity` 为正整数；`temp_low ≤ temp_high`（温度为整数闭区间）。

成功响应 `201 Created`：

```json
{
  "id": "9c05e113e3e0430eb552a0bd01036dbb",
  "capacity": 10,
  "item_count": 4,
  "kiln_count": 3,
  "items": [ ... 原样回显 ... ],
  "kilns": [
    {"index": 1, "item_ids": ["p1"],        "total_volume": 5,  "firing_temp": 100},
    {"index": 2, "item_ids": ["p2", "p3"],  "total_volume": 10, "firing_temp": 150},
    {"index": 3, "item_ids": ["p4"],        "total_volume": 5,  "firing_temp": 1000}
  ],
  "created_at": "2026-09-24T05:34:51.026381+00:00"
}
```

错误情况：

- `400` `ITEM_EXCEEDS_CAPACITY`：单件器物超过单窑容量，响应体含全部超容器物编号；
- `422`：请求参数非法（编号重复 / 区间非法 / 数量超限 / 非正整数等）。

### 2. 按 ID 读取方案

`GET http://localhost:8000/plans/{id}`

返回与创建接口相同的完整方案；方案不存在时返回 `404`（错误码 `PLAN_NOT_FOUND`）。

curl 示例：

```bash
curl -s http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb
```

### 3. 求满足目标窑次的最低单窑容量

`POST http://localhost:8000/plans/{id}/capacity-target`

针对**已保存**的方案，以方案中保存的器物体积与温度区间为准，在
「最大单件体积 ～ 器物总体积」的整数容量范围内，求使**最少窑次 ≤ 目标窑次**
的**最小单窑容量**。每个容量点的判定沿用与创建方案相同的求解器
（同窑温区规则、同窑次字典序排序规则）。**只读计算，不改写原方案。**

请求体：

```json
{"target_kiln_count": 2}
```

字段约束：`target_kiln_count` 为整数，范围 `1～原方案器物数`。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/capacity-target \
  -H 'Content-Type: application/json' \
  -d '{"target_kiln_count": 2}'
```

成功响应 `200 OK`（最低容量、实际窑次与对应装窑明细）：

```json
{
  "plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "target_kiln_count": 2,
  "min_capacity": 15,
  "kiln_count": 2,
  "kilns": [
    {"index": 1, "item_ids": ["p1", "p2", "p3"], "total_volume": 15, "firing_temp": 150},
    {"index": 2, "item_ids": ["p4"],             "total_volume": 5,  "firing_temp": 1000}
  ]
}
```

错误情况：

- `400` `TARGET_OUT_OF_RANGE`：`target_kiln_count` 超出 `1～原方案器物数`；
- `400` `TARGET_UNREACHABLE`：容量取器物总体积时最少窑次仍超过目标
  （通常因温度区间互不兼容），响应体说明所需的最少窑次；
- `404` `PLAN_NOT_FOUND`：方案不存在；
- `422`：请求参数非法（如 `target_kiln_count` 小于 1 或非整数）。

### 4. 限窑次接单（在最多 max_kilns 窑内择优保留器物）

`POST http://localhost:8000/plans/{id}/production-limit`

针对**已保存**的方案，沿用其**单窑容量**以及每件器物的**体积与温度区间**，
在**最多 `max_kilns` 窑**的产能约束下选择本批可烧（保留）的器物，
其余器物延期。选择按以下优先级择优：

1. **保留件数最多**；
2. 件数相同则**保留器物总体积最大**；
3. 仍并列时，取**保留编号升序列表字典序最小**者（标准字符串字典序）。

保留器物的**实际装窑**完全复用创建方案的求解器：窑次取保留器物的
**最少窑次**（可能少于 `max_kilns`），窑次相同时取整体字典序最小方案。
**只读计算，不改写原方案。**

请求体：

```json
{"max_kilns": 2}
```

字段约束：`max_kilns` 为整数，范围 `1～原方案器物数`。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/production-limit \
  -H 'Content-Type: application/json' \
  -d '{"max_kilns": 2}'
```

成功响应 `200 OK`（保留 / 延期编号均按编号升序，并附实际装窑明细）：

```json
{
  "plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "max_kilns": 2,
  "kept_count": 3,
  "delayed_count": 1,
  "kept_volume": 15,
  "kept_item_ids": ["p1", "p2", "p3"],
  "delayed_item_ids": ["p4"],
  "kiln_count": 2,
  "kilns": [
    {"index": 1, "item_ids": ["p1"],       "total_volume": 5,  "firing_temp": 100},
    {"index": 2, "item_ids": ["p2", "p3"], "total_volume": 10, "firing_temp": 150}
  ]
}
```

上例中 `p4` 温区（1000～1100）与其余器物无交集，2 窑内容纳不下 4 件，
故延期；保留的 `p1 / p2 / p3` 以最少 2 窑烧成。
注意 `kiln_count` 是保留器物的**实际**窑次——例如 `max_kilns` 传 2
而保留器物 1 窑即可装下时，`kiln_count` 为 `1`。

错误情况：

- `400` `MAX_KILNS_OUT_OF_RANGE`：`max_kilns` 超出 `1～原方案器物数`；
- `404` `PLAN_NOT_FOUND`：方案不存在；
- `422`：请求参数非法（如 `max_kilns` 小于 1 或非整数）。

### 5. 追加器物（填入原窑或追加窑次，另存为新方案）

`POST http://localhost:8000/plans/{id}/append-items`

针对**已保存**的方案追加 **1 件及以上**新器物，沿用原方案的**单窑容量**，
在**不改动原有器物与原窑次序**的前提下安排新器物：

- 新器物可填入**剩余容量与温度区间交集均满足**的原窑（同一原窑可填入多件，
  按合并后的整体判定；填入后该窑烧成温度更新为新的温区交集下界）；
- 填不进原窑的新器物进入**追加窑次**（排在原窑之后，规则与新建方案相同）；
- 全局选择**新增窑次数最少**的方案；并列时按「原窑次序 + 追加窑次序」
  排列各窑的升序编号列表，取**整体字典序最小**者。

结果**作为新方案保存并返回**（新方案 ID），**原方案不改写**。

请求体：

```json
{
  "items": [
    {"item_id": "p5", "volume": 4, "temp_low": 150,  "temp_high": 260},
    {"item_id": "p6", "volume": 6, "temp_low": 1000, "temp_high": 1050}
  ]
}
```

字段约束：`items` 数量 1～14；`item_id` 为非空字符串、请求内唯一且**不得与
原方案重复**；`volume` 为正整数；`temp_low ≤ temp_high`；合并后器物总数
不得超过 14 件。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/append-items \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"item_id": "p5", "volume": 4, "temp_low": 150, "temp_high": 260}, {"item_id": "p6", "volume": 6, "temp_low": 1000, "temp_high": 1050}]}'
```

成功响应 `201 Created`（新方案完整内容，格式与创建方案相同）：

```json
{
  "id": "7f4a2c10d8e44f2c9a1b3d5e6f708192",
  "capacity": 10,
  "item_count": 6,
  "kiln_count": 4,
  "items": [ ... 原 4 件 + 新 2 件 ... ],
  "kilns": [
    {"index": 1, "item_ids": ["p1", "p5"], "total_volume": 9,  "firing_temp": 150},
    {"index": 2, "item_ids": ["p2", "p3"], "total_volume": 10, "firing_temp": 150},
    {"index": 3, "item_ids": ["p4"],       "total_volume": 5,  "firing_temp": 1000},
    {"index": 4, "item_ids": ["p6"],       "total_volume": 6,  "firing_temp": 1000}
  ],
  "created_at": "2026-09-25T03:10:00.000000+00:00"
}
```

上例中 `p5`（体积 4、温区 150～260）可填入窑 1（剩余容量 5、温区交集
150～200 非空），填入后窑 1 烧成温度更新为交集下界 `150`；`p6` 温区
1000～1050 与窑 1、2 不相交，窑 3 剩余容量不足，故追加为窑 4。

错误情况：

- `400` `DUPLICATE_ITEM_ID`：新器物编号与原方案重复；
- `400` `ITEM_COUNT_EXCEEDS_LIMIT`：合并后器物总数超过 14 件；
- `400` `ITEM_EXCEEDS_CAPACITY`：新器物单件体积超过原方案单窑容量；
- `404` `PLAN_NOT_FOUND`：方案不存在；
- `422`：请求参数非法（请求内编号重复 / 区间非法 / 数量超限 / 非正整数等）。

### 6. 取消器物（紧凑重排，另存为新方案）

`POST http://localhost:8000/plans/{id}/cancel-items`

针对**已保存**的方案取消 1 件及以上器物，沿用原方案的**单窑容量**与剩余器物的
**体积、温度区间**，把剩余器物**重新紧凑排程**，优先级为：

1. **新方案窑次最少**（与创建方案相同的位掩码 DP 求解）；
2. 窑次相同时，把**原窑与新窑一对一最优配对**（配对规则与第 7 节方案对比
   完全一致：配对数取两边窑次数较小值，使**留在对应窑的器物数最多**，
   最优配对并列时取配对序列字典序最小），以**仍留在对应窑的器物数最多**为
   次目标；
3. 仍并列时，取新窑编号升序列表的**整体字典序最小**方案。

返回**取消编号、留窑 / 换窑编号、窑次配对与重排后的新方案**，新方案
**另存并返回新 ID**，**原方案不改写**。被腾空的原窑仍参与配对统计
（其上已无器物，自然不产生留窑）。

请求体：

```json
{"item_ids": ["p4"]}
```

字段约束：`item_ids` **非空**、编号在请求内**不重复**，且每个编号都必须
**属于原方案**；取消后**至少保留一件**器物（数量 1～14）。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/cancel-items \
  -H 'Content-Type: application/json' \
  -d '{"item_ids": ["p4"]}'
```

成功响应 `201 Created`：

```json
{
  "source_plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "new_plan_id": "3b1e7d20a9c54f6b8d2e1a47c9035fe8",
  "canceled_item_ids": ["p4"],
  "canceled_count": 1,
  "stayed_item_ids": ["p1", "p2", "p3"],
  "moved_item_ids": [],
  "stayed_count": 3,
  "moved_count": 0,
  "kiln_pairs": [
    {"source_index": 1, "target_index": 1},
    {"source_index": 2, "target_index": 2}
  ],
  "new_plan": {
    "id": "3b1e7d20a9c54f6b8d2e1a47c9035fe8",
    "capacity": 10,
    "item_count": 3,
    "kiln_count": 2,
    "items": [
      {"item_id": "p1", "volume": 5, "temp_low": 100,  "temp_high": 200},
      {"item_id": "p2", "volume": 6, "temp_low": 120,  "temp_high": 220},
      {"item_id": "p3", "volume": 4, "temp_low": 150,  "temp_high": 300}
    ],
    "kilns": [
      {"index": 1, "item_ids": ["p1"],       "total_volume": 5,  "firing_temp": 100},
      {"index": 2, "item_ids": ["p2", "p3"], "total_volume": 10, "firing_temp": 150}
    ],
    "created_at": "2026-09-25T06:20:00.000000+00:00"
  }
}
```

紧凑重排会真正压缩窑次：例如原方案容量放宽到 15 时为 `[p1,p2,p3] [p4]`，
取消高温器物 `p4` 后剩余 3 件 1 窑即可烧成，`kiln_pairs` 为
`[{"source_index": 1, "target_index": 1}]`，3 件全部留窑；而在容量 10 的
原方案 `[p1] [p2,p3] [p4]` 中取消 `p1` 后，`p2,p3` 组成的新窑与原窑 2
配对（`{"source_index": 2, "target_index": 1}`），两件均记为留窑。

错误情况：

- `400` `UNKNOWN_ITEM_ID`：存在不属于原方案的编号，响应体列出全部非法编号；
- `400` `CANCEL_ALL_ITEMS`：请求取消原方案全部器物（取消后至少保留一件）；
- `404` `PLAN_NOT_FOUND`：方案不存在；
- `422`：请求参数非法（列表为空 / 编号重复 / 编号为空白字符串 / 缺少字段等）。

### 7. 比较两份排程方案

`POST http://localhost:8000/plans/compare`

针对**两份已保存**的方案，以**共同器物编号**为范围，将来源与目标窑次
**一对一配对**，比较两版排程的差异：

- 配对数取**两边窑次数的较小值**；
- 配对使**留在对应窑中的共同器物数最大**（共同器物在来源窑 `i`、
  目标窑 `j`，且窑 `i` 与窑 `j` 配对，记为留窑）；
- 最优解并列时，按来源序号排序的（来源序号、目标序号）配对序列取
  **字典序最小**；
- 共同器物**未落在配对窑内**（所在来源窑或目标窑未被配对）也算换窑。

返回窑次配对、留在对应窑 / 换窑的共同器物编号，以及仅在来源或目标
方案出现的器物编号；所有编号列表均按编号升序。**只读计算，不改写任一方案。**

请求体：

```json
{
  "source_plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "target_plan_id": "7f4a2c10d8e44f2c9a1b3d5e6f708192"
}
```

字段约束：`source_plan_id` 与 `target_plan_id` 均为非空字符串。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/compare \
  -H 'Content-Type: application/json' \
  -d '{"source_plan_id": "9c05e113e3e0430eb552a0bd01036dbb", "target_plan_id": "7f4a2c10d8e44f2c9a1b3d5e6f708192"}'
```

成功响应 `200 OK`。示例：来源方案为第 1 节的 `[p1] [p2,p3] [p4]`，
目标方案为 `[p1,p3] [p2,p5]`（`p4` 已取消、新增 `p5`）：

```json
{
  "source_plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "target_plan_id": "7f4a2c10d8e44f2c9a1b3d5e6f708192",
  "common_count": 3,
  "pair_count": 2,
  "kiln_pairs": [
    {"source_index": 1, "target_index": 1},
    {"source_index": 2, "target_index": 2}
  ],
  "stayed_item_ids": ["p1", "p2"],
  "moved_item_ids": ["p3"],
  "only_in_source_item_ids": ["p4"],
  "only_in_target_item_ids": ["p5"]
}
```

上例中 `p1`（窑 1→窑 1）、`p2`（窑 2→窑 2）留窑；`p3` 由来源窑 2 改到
目标窑 1，记为换窑；`p4` 仅在来源方案、`p5` 仅在目标方案出现。

错误情况：

- `404` `PLAN_NOT_FOUND`：任一方案不存在（响应体指明是来源还是目标方案）；
- `422`：请求参数非法（缺少字段或编号为空字符串）。

### 8. 釉色隔离（提交器物互斥对，只读重求最少窑次建议）

`POST http://localhost:8000/plans/{id}/glaze-separation`

某些釉色（或釉色组合）在同一窑内会互相影响，需要强制**分窑烧成**。针对
**已保存**的方案，提交一份**非空的器物互斥对列表**（每对两件器物，表示二者
**不能同窑**）；服务沿用原方案保存的**器物、单窑容量及温区规则**，把互斥
约束并入子集可行性后**重新求出满足全部互斥对的最少窑次**，窑次并列时沿用
现有的**整体字典序规则**。结果**仅作建议**：返回原方案 ID、窑次数与装窑
明细，**不改写也不另存**原方案。

请求体：

```json
{
  "separation_pairs": [["p1", "p3"], ["p2", "p3"]]
}
```

字段约束：`separation_pairs` **非空**；每个互斥对恰好包含**两个**器物编号，
两端编号**必须不同**且都**属于原方案**；互斥对为**无序对**（`["p3","p1"]`
与 `["p1","p3"]` 等价）且**不得重复**（顺序相反视为同一对）。互斥对上限为
14 件器物的两两组合数 91 对。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/glaze-separation \
  -H 'Content-Type: application/json' \
  -d '{"separation_pairs": [["p1", "p3"], ["p2", "p3"]]}'
```

成功响应 `200 OK`。沿第 1 节容量 10 的方案 `[p1] [p2,p3] [p4]`：上例中
`p3` 与 `p1`、`p2` 均不能同窑，而 `p1+p2` 体积超容、`p4` 温区不兼容，
故最少窑次由 3 增至 4，每件单独成窑：

```json
{
  "plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "separation_pair_count": 2,
  "kiln_count": 4,
  "kilns": [
    {"index": 1, "item_ids": ["p1"], "total_volume": 5, "firing_temp": 100},
    {"index": 2, "item_ids": ["p2"], "total_volume": 6, "firing_temp": 120},
    {"index": 3, "item_ids": ["p3"], "total_volume": 4, "firing_temp": 150},
    {"index": 4, "item_ids": ["p4"], "total_volume": 5, "firing_temp": 1000}
  ]
}
```

互斥约束不必然增加窑次：同一方案仅提交 `[["p2","p3"]]` 时，3 窑仍可行，
服务在所有 3 窑方案中按整体字典序给出建议
`[p1,p3] [p2] [p4]`（首窑 `[p1,p3]` 总体积 9、烧成温度 150）。

错误情况：

- `400` `UNKNOWN_ITEM_ID`：互斥对含不属于原方案的编号，响应体列出全部非法编号；
- `404` `PLAN_NOT_FOUND`：方案不存在；
- `422`：请求参数非法（列表为空 / 对不是恰好两个编号 / 两端编号相同 /
  互斥对重复（含顺序相反）/ 编号为空白字符串 / 缺少字段等）。

### 9. 窑次温控核验（求落在合格温区内的最长连续时段）

`POST http://localhost:8000/plans/{id}/kilns/{index}/temperature-check`

针对**已保存**方案的某个窑次（`index` 从 1 开始，与方案中窑次序号一致），
以**该窑器物温区交集**（`max(各器物 temp_low)` ～ `min(各器物 temp_high)`）
为**合格温区闭区间**，核验一段窑温采样曲线：相邻采样点之间温度按
**线性变化**，精确求出温度落在合格温区内的**最长连续时段**。

- 采样点时刻为**整数秒**、温度为**整数**，时刻须**严格递增**；
- 相邻采样间隔**超过** `max_interval_seconds` 时曲线在该段**断开**
  （段内不计入，间隔恰好等于上限不断开）；
- **不在首末采样点之外外推**；
- 全部计算使用**精确分数**：线性穿越温区边界的时刻为有理数，起止时刻与
  时长均以**约分分数** `p/q` 字符串表示（如 `55/2`；整数为 `p/1`）；
- 最长时段**等长时取开始最早**者；**无正时长**时段时 `start` / `end`
  为 `null`、`duration_seconds` 为 `"0/1"`；
- `meets_requirement`：`duration_seconds ≥ min_hold_seconds` 即达标。

**只读计算，不改写原方案。**

请求体：

```json
{
  "samples": [
    {"time": 0,   "temperature": 160},
    {"time": 20,  "temperature": 200},
    {"time": 35,  "temperature": 240},
    {"time": 50,  "temperature": 200},
    {"time": 65,  "temperature": 180},
    {"time": 95,  "temperature": 190},
    {"time": 115, "temperature": 210}
  ],
  "min_hold_seconds": 25,
  "max_interval_seconds": 20
}
```

字段约束：`samples` 至少 1 个采样点，时刻为严格递增的整数秒、温度为整数；
`min_hold_seconds` 与 `max_interval_seconds` 均为正整数。

curl 示例（沿用第 1 节方案，窑 2 为 `[p2, p3]`，合格温区 150～220）：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/kilns/2/temperature-check \
  -H 'Content-Type: application/json' \
  -d '{"samples": [{"time": 0, "temperature": 160}, {"time": 20, "temperature": 200}, {"time": 35, "temperature": 240}, {"time": 50, "temperature": 200}, {"time": 65, "temperature": 180}, {"time": 95, "temperature": 190}, {"time": 115, "temperature": 210}], "min_hold_seconds": 25, "max_interval_seconds": 20}'
```

成功响应 `200 OK`：

```json
{
  "plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "kiln_index": 2,
  "item_ids": ["p2", "p3"],
  "temp_low": 150,
  "temp_high": 220,
  "min_hold_seconds": 25,
  "max_interval_seconds": 20,
  "start": "0/1",
  "end": "55/2",
  "duration_seconds": "55/2",
  "meets_requirement": true
}
```

上例中：t=20～35 由 200 升至 240，于 `55/2` 秒（27.5 秒）穿越上界 220；
t=35～50 降温于 `85/2` 秒（42.5 秒）回到 220，与其后仍在温区内的时段
合并为 `[85/2, 65]`（`45/2` 秒）；t=65～95 间隔 30 秒超过上限 20，
曲线断开；末段 `[95, 115]` 合格 20 秒。最长合格时段为 `[0, 55/2]`，
精确时长 `55/2` 秒，达到 25 秒最低保温要求。

错误情况：

- `404` `PLAN_NOT_FOUND`：方案不存在；
- `404` `KILN_NOT_FOUND`：窑次序号超出 `1～窑次数`；
- `422`：采样或参数非法（采样为空 / 时刻非严格递增 / 非整数 /
  保温秒数或采样间隔非正整数 / 缺少字段等）。

### 10. 单台实体窑点火排程（全部批次不逾期时最早整体完工）

`POST http://localhost:8000/plans/{id}/firing-order`

方案的各个窑次需要在**同一台实体窑**上逐批烧成：实体窑同一时刻至多烧一批
（**不可并行**），两批之间**允许空闲**等待。针对**已保存**的方案，按其
**现有窑次序号**为每一批提交三个分钟数（均为整数，从**同一排程起点 0**
起算）：

- `earliest_firing_minute`：该批**最早可点火**分钟（非负整数）；
- `firing_minutes`：该批**烧成时长**（正整数分钟，点火后连续占用实体窑）；
- `latest_finish_minute`：该批**最迟完成**分钟（非负整数，完成时刻不得晚于它）。

请求中的批次必须**恰好覆盖方案的全部窑次**（不缺、不多、窑次序号不重复）。
服务求一个点火顺序，使**全部批次均不逾期**（完成时刻 ≤ 各自最迟完成分钟），
并令**整体完工时刻（最后一批完成时刻）最早**；返回各批次的点火 / 完成时刻
与点火顺序。**只读计算，不改写方案的装窑结果。**

请求体（沿用第 1 节容量 10 的 3 窑方案 `[p1] [p2,p3] [p4]`）：

```json
{
  "batches": [
    {"kiln_index": 1, "earliest_firing_minute": 0,  "firing_minutes": 10, "latest_finish_minute": 100},
    {"kiln_index": 2, "earliest_firing_minute": 0,  "firing_minutes": 8,  "latest_finish_minute": 100},
    {"kiln_index": 3, "earliest_firing_minute": 0,  "firing_minutes": 5,  "latest_finish_minute": 100}
  ]
}
```

字段约束：`batches` 数量 1～14；`kiln_index` 为 ≥ 1 的整数（与方案中窑次序号
一致）且请求内不重复；最早可点火 / 最迟完成为**非负整数**，烧成时长为
**正整数**；批次集合必须与方案的全部窑次**一一对应**。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/plans/9c05e113e3e0430eb552a0bd01036dbb/firing-order \
  -H 'Content-Type: application/json' \
  -d '{"batches": [{"kiln_index": 1, "earliest_firing_minute": 0, "firing_minutes": 10, "latest_finish_minute": 100}, {"kiln_index": 2, "earliest_firing_minute": 0, "firing_minutes": 8, "latest_finish_minute": 100}, {"kiln_index": 3, "earliest_firing_minute": 0, "firing_minutes": 5, "latest_finish_minute": 100}]}'
```

成功响应 `200 OK`。上例三批均可从起点点火、窗口宽松，故按窑次序号紧排
`1 → 2 → 3`（无空闲），整体完工 23 分钟：

```json
{
  "plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "batch_count": 3,
  "makespan_minute": 23,
  "slots": [
    {"order": 1, "kiln_index": 1, "item_ids": ["p1"],       "start_minute": 0,  "finish_minute": 10},
    {"order": 2, "kiln_index": 2, "item_ids": ["p2", "p3"], "start_minute": 10, "finish_minute": 18},
    {"order": 3, "kiln_index": 3, "item_ids": ["p4"],       "start_minute": 18, "finish_minute": 23}
  ]
}
```

`slots` 按点火顺序 `order`（从 1 开始）排列，**恰好覆盖全部窑次**；
`start_minute = max(上一批完成时刻, 本批最早可点火分钟)`（实体窑可空闲等待），
`finish_minute = start_minute + firing_minutes`；`makespan_minute` 取最后一批
完成时刻，是所有不逾期顺序中的**最小值**。最早完工顺序并列时，返回其中
**窑次序号序列字典序最小**的点火顺序（完全对称的批次即按 `1,2,3,…` 紧排）。

最早可点火 / 最迟完成会改变最优顺序：把窑 1 的最早可点火改为第 20 分钟、
窑 3 的最迟完成收紧到第 12 分钟时，最优顺序变为
`3（0→5）→ 2（5→13）→ 1（20→30，空闲等待）`，整体完工 30 分钟：

```json
{
  "plan_id": "9c05e113e3e0430eb552a0bd01036dbb",
  "batch_count": 3,
  "makespan_minute": 30,
  "slots": [
    {"order": 1, "kiln_index": 3, "item_ids": ["p4"],       "start_minute": 0,  "finish_minute": 5},
    {"order": 2, "kiln_index": 2, "item_ids": ["p2", "p3"], "start_minute": 5,  "finish_minute": 13},
    {"order": 3, "kiln_index": 1, "item_ids": ["p1"],       "start_minute": 20, "finish_minute": 30}
  ]
}
```

错误情况：

- `400` `FIRING_BATCH_MISMATCH`：批次未恰好覆盖方案全部窑次（缺窑次或出现
  不属于该方案的窑次序号），响应体列出缺少 / 多出的序号；
- `400` `FIRING_ORDER_INFEASIBLE`：**不存在**使全部批次均不逾期的点火顺序
  （例如各批烧成总时长超过最迟完成窗口，或释放 / 截止窗口相互冲突；
  某批 `最早可点火 + 烧成时长 > 最迟完成` 时该批自身即无法烧成）；
- `404` `PLAN_NOT_FOUND`：方案不存在；
- `422`：请求参数非法（批次为空 / 超过 14 个 / 窑次序号重复 /
  最早可点火或最迟完成为负数 / 烧成时长非正整数 / 缺少字段等）。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
KILN_DB_PATH=./kiln.db uvicorn app.main:app --reload
```

运行求解器、温控核验与点火排程测试（无需第三方依赖，含随机用例的暴力枚举交叉验证）：

```bash
python3 tests/test_solver.py
python3 tests/test_tempcheck.py
python3 tests/test_firing.py
```

## 算法说明（n ≤ 14）

1. 枚举全部 `2^n` 个器物子集，用「去掉最低位 + 一件」递推每个子集的
   总体积与温度交集，标记可成窑的子集；
2. 位掩码 DP：`dp[0]=0`，
   `dp[mask] = 1 + min(dp[mask ^ g])`（`g` 枚举 `mask` 的可行非空子集）；
3. 贪心重建字典序最小方案：每一步在剩余器物中，枚举「包含剩余最小编号器物」的
   所有可行子集，选出取出后剩余器物仍能在剩余窑次内装完的、
   器物编号序列字典序最小的子集作为下一窑。

n = 14 时子集总数 16 384，典型耗时在 1 秒以内。

`capacity-target` 接口的最低容量搜索：窑次数随容量**单调不增**
（容量放宽只会扩大可成窑子集集合），故在
「最大单件体积 ～ 器物总体积」区间上**二分查找**最小可行容量，
每个容量点完整调用上述求解器，搜索次数为区间长度的对数级。

`production-limit` 接口的限窑次择优：直接复用步骤 1～2 得到的可行子集
表与 `dp`（最少窑次），枚举全部 `2^n` 个器物子集，仅保留
`dp[mask] ≤ max_kilns` 者，按「件数 → 总体积 → 编号升序列表字典序」
三级键取最优；确定保留集合后再调用完整求解器生成装窑明细，
故实际窑次取保留器物的最少窑次，方案排序与创建接口完全一致。
n = 14 时与求解器同为万级枚举规模，典型耗时约 1～3 秒。

`compare` 接口的窑次配对：记 `w[i][j]` 为来源窑 `i` 与目标窑 `j` 的
共同器物数，则「留窑的共同器物数最大」即**二分图最大权匹配**。
以「来源窑序 × 目标窑子集」做位掩码 DP（状态数 `m × 2^k`，
窑次数 `m、k` 均不超过器物数 14），求出最优值后再按
「（来源序号、目标序号）配对序列字典序最小」贪心重建配对；
双方各 14 窑时典型耗时约 1 秒。

`append-items` 接口的追加排程：记新器物 `k` 件、原窑 `m` 个
（合并后器物总数 ≤ 14，故 `m + k ≤ 14`）。

1. 枚举新器物的全部 `2^k` 个子集，递推总体积与温度交集，得到
   「可单独成追加窑」标记与最少追加窑次 `dp`（同求解器的子集表）；
2. 对每个原窑求可接纳的新器物子集（剩余容量 + 温区交集）；
3. **后缀位掩码 DP**：`best[j][mask]` 表示用原窑 `j..m-1` 与追加窑
   安置新器物集合 `mask` 所需的最少追加窑次，逐窑做子集 min 卷积回推，
   `best[0][全集]` 即全局最少新增窑次；
4. 按原窑次序贪心重建：为每个原窑在「剩余器物仍可在最少追加窑次内
   安置」的可接纳子集中，选合并后编号升序列表字典序最小者；
   剩余新器物复用求解器生成追加窑次（最少窑次 + 整体字典序最小）。

合并 14 件（如原 1 件 + 新 13 件）时典型耗时在数秒以内。

`cancel-items` 接口的紧凑重排：取消后剩余器物至多 13 件，先复用子集表与
`dp` 得到最少窑次 `K`，再在全部 `K` 窑可行划分中做「**原窑标签 × 器物子集**」
位掩码 DP：按原窑次序逐个标签（取消后仍含器物的原窑）决定其**不配对**或
与某个新窑（剩余器物的可行非空子集，且取出后剩余器物仍能在剩余窑次内装完）
配对，配对收益为新窑中本就属于该原窑的器物数（即留窑数）。状态
`(j, p, mask)` 表示原窑标签 `j..r-1` 待处理、还需配出 `p` 个新窑、已安置
器物集合 `mask`，编码为整数 `(留窑数 << F) | 已选窑排名位集`（`F` 为可行
窑子集总数，排名位采用反转位使整数取最大恰好等价于「留窑数最多 →
窑序列整体字典序最小」）。重建出最优划分后，再调用与 `/plans/compare`
相同的最大权配对算法生成对外的窑次配对与留窑 / 换窑编号，保证配对并列时
**沿用现有方案对比规则**。剩余 13 件时典型耗时在数秒以内。

`glaze-separation` 接口的釉色隔离：先把互斥对（无序对）转为每件器物的
「冲突邻居」位掩码，在步骤 1 的子集可行性递推中追加一条判定——
向子集加入一件器物时，该器物不得与子集内任一器物互为冲突对。互斥约束因此
**直接并入可行子集集合 `valid`**，步骤 2 的最少窑次 DP 与步骤 3 的整体
字典序贪心重建均无需改动，输出天然满足「最少窑次 + 窑次并列整体字典序
最小」，且仍是只读计算。计算量与无互斥求解相同，n = 14 时典型耗时在
1～2 秒以内。

`temperature-check` 接口的温控核验：以该窑器物温区交集为合格温区闭区间，
把采样曲线视为**分段线性函数**，全部中间量用 `Fraction` 精确计算。对每个
未断开的采样段（间隔 ≤ 上限），线性（单调）温度落在闭区间内的时刻集合
是一个闭区间，端点为采样时刻或温度恰等于温区边界的穿越时刻（有理数）；
相邻段在采样点处端点相接即合并为同一连续时段。取时长最长者（等长取开始
最早），无正时长时段时起止为空、时长为 0。计算量为采样点数的线性级。

`firing-order` 接口的单台实体窑点火排程：这是单机、批次不可并行、允许空闲、
带**最早可点火（释放时刻）与最迟完成（截止时刻）**的排程问题
（`1|r_j|L_max ≤ 0` 可行性 + 最早 `C_max`，一般情形 NP 难），窑次数至多 14，
用位掩码 DP 精确求解。

1. 前向位掩码 DP 求每个批次集合从起点 0 起排的**最早完工时刻**：
   `dp[0]=0`，`dp[mask] = min_j max(dp[mask∖{j}], r_j) + p_j`
   （仅保留完成时刻 `≤ d_j` 的转移）；`dp[全集]` 为 +∞ 即无任何不逾期顺序，
   否则它就是**最早整体完工时刻 C\***。
2. 逐批贪心重建：已选前缀结束后实体窑于 `t` 空闲，按窑次序号升序尝试把批次
   `j` 作为下一批（`s = max(t, r_j)`，完成 `c = s + p_j ≤ d_j`），并用同构的
   **后缀可行性 DP**（初值由 0 换为 `c`）判断其余批次从 `c` 起排能否恰在
   C\* 完工；首个可扩展为最优顺序的候选即被选中。每一步都存在可扩展候选
   （最优顺序本身按位提供），故重建必不中断，且序号升序取首个可扩展候选
   恰好得到全部最早完工顺序中**窑次序号序列字典序最小**者。

分钟数全为整数、运算仅含 `max` 与加法，各批点火 / 完成时刻亦为整数。
n = 14 时典型耗时在数秒以内。

## 目录结构

```
app/
  solver.py     # 位掩码 DP + 字典序重建（纯函数，无框架依赖）
  tempcheck.py  # 窑温核验：分段线性 + 分数精确求最长合格时段（纯函数）
  firing.py     # 单台实体窑点火排程：释放/截止窗口位掩码 DP（纯函数）
  database.py   # SQLite 初始化与读写
  schemas.py    # Pydantic 请求/响应模型
  main.py       # FastAPI 路由
tests/
  test_solver.py
  test_tempcheck.py
  test_firing.py
entrypoint.sh   # 安装依赖 → 初始化数据库 → 启动服务
Dockerfile
docker-compose.yml
requirements.txt
```
