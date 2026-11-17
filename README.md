# 博物馆藏品开放目录采集网页

面向研究人员的博物馆藏品开放目录采集与发布服务。浏览器中填写目录接口地址、
启动或续采采集批次，并下载当前已发布的完整 JSON 数据集。
藏品数据员可在**校订页**对年代、材质做人工校订（保留所依据的源值），
后续采集发布时自动核对源目录的更正，并下载校订版 JSON；原始发布下载保持可用。

- **采集端**：FastAPI + SQLite，网页入口 + REST API（端口 `8000`）
- **示例源**：独立 FastAPI 服务，内置 3 个版本 × 每版 3 页的藏品数据，
  支持快照令牌分页与故障注入控制台（端口 `8001`）

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 采集台（研究人员主入口） | http://localhost:8000/ |
| 藏品校订页（数据员入口） | http://localhost:8000/corrected |
| 示例源故障控制台 | http://localhost:8001/ |
| 采集端 API 文档 | http://localhost:8000/docs |
| 示例源 API 文档 | http://localhost:8001/docs |

SQLite 数据库保存在命名卷 `museum-data`（容器内 `/data/museum.db`），
容器重建后批次与已发布数据不丢失。

## 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 终端 1：示例源
.venv/bin/python -m uvicorn source_service.app:app --port 8001

# 终端 2：采集端（数据库默认落在 ./data/museum.db）
MUSEUM_DEFAULT_SOURCE_URL=http://127.0.0.1:8001/catalog \
MUSEUM_SOURCE_ADMIN_URL=http://127.0.0.1:8001/ \
.venv/bin/python -m uvicorn app_service.main:app --port 8000
```

## 网页演示流程

1. 打开 http://localhost:8000/ ，目录接口地址已预填
   `http://source:8001/catalog`（本地直跑时为 `http://127.0.0.1:8001/catalog`）。
2. **启动新批次**：批次先探测源首页，绑定返回的 `snapshot_token` 与总页数，
   随后后台线程逐页采集；整批 3 页采齐后自动发布。
3. 在 http://localhost:8001/ 故障控制台可：
   - **切换版本**（v1/v2/v3，旧版本所有快照令牌立即失效）；
   - **令单页失败一次**（指定版本与页，下次请求返回 500，仅一次）；
   - **令令牌失效**（存活令牌全部失效，再翻页返回 `410 Gone`）。
4. 批次失败后，网页上出现 **续采** 按钮：已落库的页直接复用、不再请求源，
   仅补采缺失页。
5. 令牌失效的批次状态为 `invalid`，续采会被拒绝，必须 **新建批次**
   获取新快照令牌。
6. 点击 **⬇ 下载完整 JSON** 获取当前已发布数据集（附件下载）。
7. 打开 http://localhost:8000/corrected 校订页：
   - 对某件藏品的年代/材质点 **校订**，填写校订值保存（系统记录所依据的源值）；
   - 在源控制台切换 v2（A002 年代、A003 材质被源更正，A004 被删除）后新建批次；
   - 校订页可看到核对结果：源未变的校订沿用、源已改成校订值的自动消除、
     源改成第三值的列为冲突、被删藏品的校订随藏品移除；
   - 冲突逐字段裁决（**采用源值** 或 **保留校订值**），存在未决冲突时
     **禁止下载校订版**；裁决完成后可下载校订版 JSON。

## 核心业务规则与实现

### 快照令牌分页，批次绑定单一快照

- 协议：`GET /catalog` 取首页并签发 `snapshot_token`；
  `GET /catalog?token=...&page=n` 凭令牌翻页。
- 建批时先成功取得首页，将 `snapshot_token / source_version / total_pages`
  写入 `batches` 表；采集线程自始至终只使用该令牌。
- 源对非存活/已失效令牌返回 `410`；采集端收到后批次落 `invalid` 终态。
  采集端还校验源回传令牌与批次绑定令牌一致（`snapshot_mismatch`），
  杜绝新旧快照混用。
- 同一版本允许同时存在多个有效令牌，因此多个批次可并行而互不干扰；
  切换版本或手动失效会使当时全部存活令牌作废。

### 页级持久化与续采

- 每成功采到一页，原始 JSON 立即写入 `batch_pages` 表（按 `batch_id, page_no`
  主键）。
- 续采（失败后或应用重启后）从库中读出已保存页并跳过，只请求缺失页。
- 应用重启时，启动恢复逻辑把磁盘上仍为 `running` 的批次标记为
  `failed / interrupted`（线程已不存在），可在网页一键续采。

### 整批采齐才发布，失败保留原数据

- 只有 `batch_pages` 集齐 `1..total_pages` 全部页才触发发布。
- 发布在 **单个 SQLite 事务** 内完成：全量重写 `items` 与
  `published_items`、更新 `published_snapshot`、批次置 `published`；
  任一步失败整体回滚，旧发布数据不受影响。
- 全量重写天然实现"移除源中已不存在的藏品"：v1 中的 A004 在 v2 被删除后，
  发布结果里不再出现 A004。

### 新旧批次防覆盖（发布陈旧保护）

- 发布前检查当前已发布快照的来源批次 id：若已发布数据来自 **更晚启动**
  （id 更大）的批次，较早批次即使后采齐也不得覆盖，批次置
  `superseded`，当前发布原样保留。

### 人工校订（年代 / 材质）

- 校订页（`/corrected`）可修改字段、并排查看源值与校订值、下载校订版
  JSON；原始发布下载（`/api/published/download`）保持可用、不受校订影响。
- 每项校订在 `corrections` 表记录 **所依据的源值**（`basis_value`）。
  每次发布（与发布同一事务内）逐项核对：
  - 源值未变（仍等于依据）→ **沿用校订**；
  - 源值已等于校订值 → **差异自动消除**，校订记录清除；
  - 源值偏离依据且不同于校订值 → 列为 **冲突**，依据与校订值保留待裁决；
  - 源删除该藏品 → 藏品从校订版移除，其校订一并清除；
  - 未校订字段随源更新。
- 冲突逐字段裁决：**采用源值**（校订作废）或 **保留校订值**
  （以当前源值重新作为校订依据）。存在未决冲突时，
  `GET /api/corrected/download` 返回 409，禁止下载校订版。
- **并发一致性**：发布与校订共用同一把 SQLite 写锁，校订视图与校订版
  下载一次加锁读出，必然对应同一完整源快照及校订状态；`meta` 表中的
  `revision` 在每次发布或校订变动时递增，校订页所有提交都携带
  `base_revision`，页面所依据的源值或校订已变时提交被拒绝（409）
  并提示刷新重载。
- 采集失败不触碰发布数据与校订；校订与未决冲突持久化在 SQLite，
  应用重启后完整保留。

### 并发模型

- 每个批次一个守护线程，网络 IO 使用独立 `httpx.Client`，支持多批并行；
- SQLite 单连接 + 进程内 `RLock`，所有写操作串行化，开启 WAL 模式。

## API 摘要（采集端，前缀 http://localhost:8000）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | 新建批次（探测首页、绑定快照、立即开跑） |
| POST | `/api/batches/{id}/resume` | 续采失败批次（`invalid` 返回 409） |
| GET | `/api/batches` | 批次列表（含状态、已保存页号、是否运行中） |
| GET | `/api/batches/{id}` | 批次详情 |
| GET | `/api/published` | 当前已发布数据集（元数据 + 藏品） |
| GET | `/api/published/download` | 下载完整 JSON（`Content-Disposition: attachment`） |
| GET | `/api/corrected` | 校订视图（源值/校订值/冲突状态 + `revision`） |
| POST | `/api/corrections` | 新增/修改校订（携带 `base_revision`，过期 409） |
| DELETE | `/api/corrections/{item}/{field}` | 撤销校订（`?base_revision=`） |
| POST | `/api/corrections/{item}/{field}/resolve` | 裁决冲突（`choice`: `source`/`corrected`） |
| GET | `/api/corrected/download` | 下载校订版 JSON（有未决冲突返回 409） |
| GET | `/api/config` | 前端预填配置 |
| GET | `/health` | 健康检查（含本次启动恢复的批次 id） |

下载 JSON 结构：

```json
{
  "dataset": "museum-open-catalog",
  "published_at": "2026-11-17T16:29:57+00:00",
  "source_version": "v1",
  "snapshot_token": "snap-v1-...",
  "batch_id": 1,
  "item_count": 6,
  "items": [
    {"id": "A001", "name": "青铜饕餮纹鼎", "era": "商代晚期", "material": "青铜"}
  ]
}
```

藏品字段为编号 `id`、名称 `name`、年代 `era`、材质 `material`。

校订版下载 JSON 结构（`GET /api/corrected/download`）：

```json
{
  "dataset": "museum-open-catalog-corrected",
  "published_at": "2026-11-17T16:29:57+00:00",
  "source_version": "v1",
  "snapshot_token": "snap-v1-...",
  "batch_id": 1,
  "corrections_revision": 3,
  "correction_count": 1,
  "item_count": 6,
  "items": [
    {"id": "A001", "name": "青铜饕餮纹鼎", "era": "商代早期", "material": "青铜",
     "corrected_fields": {"era": {"source_value": "商代晚期", "corrected_value": "商代早期"}}}
  ]
}
```

## 数据模型（SQLite）

- `batches`：批次（源地址、绑定快照令牌、版本、总页数、状态、错误信息）
- `batch_pages`：批次页原始 JSON（页级持久化，续采复用）
- `items`：最近一次发布对应的藏品全集
- `published_items`：对外发布视图（下载/预览读取此表）
- `published_snapshot`：单行表，记录当前发布来自哪个批次与快照
- `corrections`：人工校订（藏品编号 + 字段、所依据的源值、校订值、
  状态 `active`/`conflict`）
- `meta`：单行元数据（`revision` 随每次发布/校订变动递增，用于校订页
  乐观并发控制）

批次状态机：`running → published | failed | invalid | superseded`；
仅 `failed` 可续采。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MUSEUM_DB_PATH` | `data/museum.db`（容器内 `/data/museum.db`） | SQLite 文件路径 |
| `MUSEUM_DEFAULT_SOURCE_URL` | `http://127.0.0.1:8001/catalog` | 网页表单预填地址 |
| `MUSEUM_SOURCE_ADMIN_URL` | `http://127.0.0.1:8001/` | 源控制台跳转地址 |
| `MUSEUM_REQUEST_TIMEOUT` | `15` | 单页请求超时（秒） |
| `MUSEUM_PAGE_PAUSE` | `0.15` | 同批次页间节流（秒） |

## 自测

```bash
.venv/bin/python -m pytest tests/ -q
```

`tests/test_e2e.py` 与 `tests/test_corrections.py` 用真实 uvicorn 进程
（独立线程事件循环）+ HTTP 调用覆盖 14 个关键流程：

采集与发布（test_e2e.py）：

1. 完整采集、自动发布与 JSON 下载字段校验
2. 单页一次性失败 → `failed` → 续采复用已保存页 → 发布
3. 令牌失效 → 续采触发 410 → `invalid` 且拒绝再续采 → 新建批次成功
4. 应用重启恢复（磁盘 `running` → `failed/interrupted`）后续采发布
5. 早批次后完成的 `superseded` 防覆盖保护
6. 版本切换后全量替换、删除消失藏品、同步名称修订（v1→v2→v3）
7. 两个批次并行采集
8. 无发布数据时 404、非法 URL 400、不可达源 502

人工校订（test_corrections.py）：

1. 新增/修改/撤销校订，校订视图与校订版下载，原始发布下载不受影响
2. 发布时核对：源未变沿用、源=校订值自动消除、源偏离成冲突、
   源删除清除校订；未校订字段随源更新
3. 未决冲突禁止下载校订版；逐字段裁决（采用源值 / 保留校订值）后放行
4. 乐观并发控制：旧 `revision` 的校订/撤销/裁决提交一律 409 并提示重载
5. 重启保留校订与未决冲突；采集失败不改变校订结果与已发布数据
6. 参数校验（未知藏品、非法字段、空值、与源值相同等）与空库行为

## 目录结构

```
.
├── app_service/            # 采集端（FastAPI + SQLite + 采集线程 + 网页）
│   ├── main.py             # API 与应用工厂（含校订 API）
│   ├── collector.py        # 批次采集器（后台线程、续采、失效处理）
│   ├── db.py               # SQLite 持久化、事务化发布与校订核对
│   ├── config.py           # 环境变量配置
│   ├── index.html          # 采集台网页
│   └── corrected.html      # 藏品校订页
├── source_service/         # 本地示例目录源
│   ├── app.py              # 分页协议 + 故障注入管理接口
│   ├── catalog_data.py     # 三版示例数据（v2 含年代/材质源更正）
│   └── admin.html          # 故障控制台网页
├── tests/
│   ├── harness.py          # 共享测试设施（uvicorn 服务器 + 临时库）
│   ├── conftest.py         # harness 夹具（每用例重置源状态）
│   ├── test_e2e.py         # 采集与发布端到端自测
│   └── test_corrections.py # 校订端到端自测
├── Dockerfile / Dockerfile.source
├── docker-compose.yml
└── requirements.txt
```

## 已知限制 / 未完成项

- 当前环境未安装 Docker，**镜像未在本机实际构建运行**；Dockerfile 与
  compose 已做静态校验（YAML 语法、文件引用、健康检查），逻辑与本地
  uvicorn 直跑一致，需在有 Docker 的环境执行 `docker compose up --build` 复核。
- 示例源为内存态：源服务重启后故障注入状态与令牌清空（采集端持久化不受
  影响；其批次会在下次续采时收到 410 而转 `invalid`，符合预期）。
- 采集端为单进程多线程部署；SQLite 写入已串行化，足以支撑本演示规模，
  未做多进程/多副本扩展。
