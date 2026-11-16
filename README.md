# fieldmap 数据湖字段演进与下游兼容判定

湖仓 42 张表、60 个下游视图与任务。上游改字段靠群消息，下游半夜报错。fieldmap 把「上游新版元数据 + 下游登记」跑成一次判定：哪些字段只是改名、哪些放宽、哪些直接破坏，谁被影响，迁移单长什么样。

## 能力

- 采信 DDL 与 JSON 两种元数据，编码回退、字节序标记、重复列、行不齐、缺身份逐条给行号。
- 类型格按 Decimal 值域 / 刻度 / 精确整数判放宽与收窄，跨族与未知类型保守判破坏。
- 读者 / 写者两向判定，结论分兼容、需别名、破坏三档。
- 字段映射与别名 SQL；改名同时换类型的项不生成别名，单独列为冲突。
- 影响面 = 引用身份解析后的反向闭包，附环、自引用、断链、单边登记。
- 批次幂等：同一输入命中批次键不再新增行；基线落后的提交返回 409。
- 迁移单 Markdown + 字段账、引用账、账目、运行清单四份导出。

## 模块

| 模块 | 动作 | 入口 |
|---|---|---|
| M1 元数据采信 | 采信文件、看诊断与隔离区 | `POST /api/schemas/import`、`/` |
| M2 类型格 | 查值域刻度、比两个类型 | `GET /api/types`、`/types` |
| M3 兼容判定 | 两向判定、原因码 | `POST /api/compat` |
| M4 字段映射 | 映射表、别名 SQL、冲突 | `GET /api/mapping`、`/mapping` |
| M5 影响面 | 闭包、环、断链、单边 | `GET /api/impact`、`/impact` |
| M6 版本批次 | 跑批次、历史、陈旧拒绝 | `POST /api/runs`、`/batches` |
| M7 迁移单 | 条款号、账目导出 | `GET /api/export/spec.md`、`/spec` |

## 核心规则

1. 字段身份只认 `field_id`：改名不是新字段，同名不同身份也不是同一个字段。
2. 类型放宽 = 新类型能无损装下旧类型的全部取值（值域、刻度、精确整数同时满足）。
3. 必须连与结论：`required` 放宽对读者兼容，收紧对写者破坏。
4. 引用解析过身份：名字还在但身份换了的算断链，不允许悄悄绑到新列。
5. 影响面 = 直接引用 + 断链节点 + 反向闭包，深度取最短跳数。

## 定义

字段、类型格、时间区间、引用、账目的完整定义在 [`docs/DATA-DICTIONARY.md`](docs/DATA-DICTIONARY.md)；模块与陷阱登记在 [`docs/DEPTH.md`](docs/DEPTH.md)；启动与失败处置在 [`docs/RUNBOOK.md`](docs/RUNBOOK.md)。

## 启动

```bash
docker compose up -d --build
curl -s localhost:18202/api/health
```

宿主 `18202` → 容器 `8000`。启动时自动把 `sample_data/` 的样例跑成第一条批次。

## 演示步骤

1. `/` 规格单：字段清单、采信诊断（阻断 2 行、提示 4 行）、隔离区 CSV。
2. `/types` 类型与兼容：类型格、两向判定表、就地比较两个类型。
3. `/mapping` 字段映射：`order_no → order_code` 的别名 SQL，`channel → pay_channel` 的冲突。
4. `/impact` 影响面：受影响节点 4 个、1 个环、1 个自引用、2 处断链、2 处单边。
5. `/batches` 版本历史：四账并列；「陈旧提交」验证 409。
6. `/spec` 迁移单：6 条破坏条款，导出 Markdown 与三份 CSV。

## 页面

`/`、`/types`、`/mapping`、`/impact`、`/batches`、`/spec`，另有 `/docs` 作为接口说明。

## 目录

```text
app/                入口、配置、原因码、清单、库、渲染
app/modules/m1..m7/ 每个模块 handler + service + 三个纯规则/读写文件
app/templates/      base + 6 页
app/static/         theme / layout / components + app / table / footnote
docs/               DEPTH、DATA-DICTIONARY、RUNBOOK
sample_data/        5 个样例，含一个 GB18030 + CRLF + 标记的脏件
tests/              conftest、7 个模块测试、e2e、不变量、fixtures
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 已知不覆盖

见 [`docs/DEPTH.md`](docs/DEPTH.md) 末节：SQL 只抽显式限定名，DDL 只解析列定义，类型格不含复杂类型，区间重叠只标记不阻断。
