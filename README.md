# 双语文稿定版服务（Python + FastAPI + SQLite）

把有序的原文段与一种人工译文维护为可回看的双语**定版**。支持原文改写、
带版本依据的译文提交 / 确认、失效待复核、乐观修订号校验，以及原子发布
不可变的整篇双语快照。

## 领域模型与规则

- **稿件（manuscript）**：拥有一个工作稿修订号 `revision`，初始为 1，
  仅在某段原文内容**真正改变**时 +1。
- **原文段（segment）**：按 `position` 有序排列，每段有独立的
  `source_version`（从 1 起）。
- **译文（translation）**：提交时必须注明 `source_version`（所依据的
  原文版本）。状态为 `submitted` / `confirmed`；段状态额外有
  `missing` / `stale`（待复核）。
- 依据**过期原文版本**的译文提交或确认一律 `409 Conflict` 拒绝。
- 原文段改版时，**仅该段**已确认译文翻转为 `stale`（待复核），
  其他段不受影响；需要基于新原文版本重新提交、确认。
- **发布定版**必须携带 `expected_revision`：
  - 与当前修订号不一致 → `409`（修订冲突）；
  - 存在未准备好的段（无匹配当前原文版本的已确认译文）→ `422`，
    响应体列出阻塞段；两种失败都**不增加版号**。
  - 全部满足时，在单个立即写事务中原子生成新定版，内容是发布瞬间的
    整篇双语文本 JSON 快照。发布与原文改写通过 SQLite 写锁互斥，
    快照不可能混入不同工作稿修订的数据。
- **定版不可变**：按版号读取的永远是发布时的快照，后续改写 / 复核
  不会改变它。

## 单条命令启动（Docker Compose）

```bash
docker compose up --build
```

服务监听在本机 **http://localhost:8000**（容器内 8000，SQLite 数据持久化
在命名卷 `bilingual-data` 的 `/data/bilingual.db`）。

- 健康检查：http://localhost:8000/health
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs

停止并保留数据：`docker compose down`；连数据卷一起删除：
`docker compose down -v`。

## 本地无容器运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./bilingual.db uvicorn app.main:app --reload --port 8000
```

## 可执行的 HTTP 调用示例

以下示例可直接复制到终端顺序执行（依赖 [jq](https://stedolan.github.io/jq/)
提取 id；不使用 jq 时可从响应里手工读取 `manuscript_id`）。

### 1. 创建稿件（录入有序原文段）

```bash
curl -sS -X POST http://localhost:8000/manuscripts \
  -H 'Content-Type: application/json' \
  -d '{"title": "示例", "segments": ["第一段原文。", "第二段原文。"]}'
# => {"manuscript_id":1,"revision":1,"segment_count":2}

MID=1
```

### 2. 查询工作稿（各段版本与状态）

```bash
curl -sS http://localhost:8000/manuscripts/$MID/draft | jq .
# 初始每段 status 为 "missing"，source_version 为 1
```

### 3. 提交译文（注明所依据的原文版本）

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/0/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "Source paragraph one.", "source_version": 1}'
# => {"status":"submitted","source_version":1,...}
```

### 4. 确认译文

```bash
curl -sS -X POST http://localhost:8000/manuscripts/$MID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}'
# => {"status":"confirmed",...}

curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/1/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "Source paragraph two.", "source_version": 1}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$MID/segments/1/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}'
```

### 5. 发布定版（携带预期工作稿修订号）

```bash
curl -sS -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}' | jq .
# => 201，edition=1，包含整篇 segments 双语内容
```

### 6. 改写原文：旧确认译文失效待复核，仅影响该段

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/0/source \
  -H 'Content-Type: application/json' \
  -d '{"text": "第一段原文（修订）。", "expected_revision": 1}'
# => {"source_version":2,"revision":2,"changed":true}

curl -sS http://localhost:8000/manuscripts/$MID/draft | jq '.segments[] | {position, source_version, status}'
# 第 0 段: status="stale"（旧译文在 stale_translation 中待复核）
# 第 1 段: 仍是 "confirmed"，不受影响
```

此时再发布：

```bash
# 用旧修订号 -> 409 修订冲突
curl -i -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}'

# 用新修订号但第 0 段尚未重新确认 -> 422，列出阻塞段，不增加版号
curl -i -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# 422 body: {"detail":{"reason":"segments_not_confirmed",
#   "blocking_segments":[{"position":0,"source_version":2}]}}

# 依据过期版本提交/确认 -> 409
curl -i -X POST http://localhost:8000/manuscripts/$MID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}'
```

补齐第 0 段译文并确认后，再发布第二版：

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/0/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "First paragraph (revised).", "source_version": 2}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$MID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 2}' >/dev/null

curl -sS -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}' | jq '.edition, .revision'
# => 2, 2
```

### 7. 按版号回看旧定版（永不改变）

```bash
curl -sS http://localhost:8000/manuscripts/$MID/publications/1 | jq .
# 仍是第 1 版发布时的原文与译文，revision=1

curl -sS http://localhost:8000/manuscripts/$MID/publications | jq '.[].edition'
# 列出全部定版：1, 2
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/manuscripts` | 创建稿件并录入有序原文段 |
| GET | `/manuscripts/{id}` | 稿件元数据（含当前修订号） |
| GET | `/manuscripts/{id}/draft` | 工作稿：各段原文版本、译文、状态 |
| PUT | `/manuscripts/{id}/segments/{p}/source` | 改写原文（带 `expected_revision`） |
| PUT | `/manuscripts/{id}/segments/{p}/translation` | 提交/改写译文（带 `source_version`） |
| POST | `/manuscripts/{id}/segments/{p}/confirmation` | 确认译文（带 `source_version`） |
| POST | `/manuscripts/{id}/publications` | 原子发布定版（带 `expected_revision`） |
| GET | `/manuscripts/{id}/publications` | 定版列表 |
| GET | `/manuscripts/{id}/publications/{edition}` | 按版号回看定版快照 |
| GET | `/health` | 健康检查 |

主要状态码：`200/201` 成功；`404` 稿件/段/定版不存在；`409` 修订冲突或
依据过期原文版本；`422` 请求体校验失败，或发布时存在阻塞段。

## 测试

```bash
pip install pytest httpx
PYTHONPATH=. python -m pytest tests -q
```

测试覆盖状态流转、过期版本拒绝、改版仅影响单段、发布的冲突与阻塞段、
失败不增版号、定版快照不可变，以及改写与发布并发下快照不跨修订拼装
（含多轮并发压测）。
