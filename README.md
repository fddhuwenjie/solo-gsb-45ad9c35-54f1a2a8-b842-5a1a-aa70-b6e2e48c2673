# 双语稿件定版服务（Bilingual Manuscript API）

把**有序的原文段落**与**人工译文**发布为可回看的**双语定版**。
Python + FastAPI + SQLite 实现，单文件数据库，零外部服务依赖。

## 核心规则

- **稿件创建**：录入有序原文段，初始工作稿修订号 `revision = 1`，每段原文版本 `source_version_no = 1`。
- **原文改写**：`PUT` 改写某一段原文 → 该段原文版本号 +1、工作稿修订号 +1；
  该段已确认译文自动失效并标记为 `pending_review`，**其他段不受影响**。
  改写需携带 `expected_revision`（乐观锁），与当前修订号不符返回 `409 revision_conflict`。
- **译文提交 / 确认**：必须注明所依据的 `source_version_no`；
  依据过期原文版本的提交或确认一律返回 `409 stale_source_version`。
- **定版发布**：请求携带 `expected_revision`；
  **只有每段都有“匹配当前原文版本”的 `confirmed` 译文**时，才在一个事务内原子生成
  包含整篇双语文本的不可变快照，定版 `version_no` +1。
  - 修订号不符 → `409 revision_conflict`；
  - 存在未达标段 → `422 blocked_segments`，响应列出全部阻塞段；
  - 两种失败都**不会增加定版版号**。
- **并发安全**：所有写事务使用 `BEGIN IMMEDIATE` + SQLite WAL；
  发布在同一事务内按提交时的工作稿修订号读取全部段并复制快照，
  原文改写与发布并发时，定版**不会混入不同工作稿修订的数据**。
- **定版不可变**：定版保存原文/译文文本快照；后续改写、复核、重新发布
  都不影响按版号读取的旧定版。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build
```

服务监听在 **http://localhost:8000**（容器内 8000，宿主机同为 8000），
SQLite 数据持久化在命名卷 `api-data`（容器内 `/data/data.db`）。

交互式文档： http://localhost:8000/docs

### 不使用 Docker（本地 Python 3.11）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可用环境变量 `DB_PATH=/path/to/data.db` 指定数据库文件（默认 `./data.db`）。

## HTTP 调用示例（可直接执行）

以下示例使用 `curl`，服务已在 `localhost:8000` 运行。

### 1. 创建稿件（有序原文段）

```bash
curl -s -X POST localhost:8000/manuscripts \
  -H 'Content-Type: application/json' \
  -d '{
        "title": "小王子（节选）",
        "segments": [
          {"text": "你好。"},
          {"text": "你是谁？"}
        ]
      }'
```

返回体包含每段 `segment_id`、`position`、`source_version_no`、`status`
（`empty` / `submitted` / `confirmed` / `pending_review`）。

### 2. 查询工作稿（显示各段版本与状态）

```bash
curl -s localhost:8000/manuscripts/1 | python3 -m json.tool
```

### 3. 提交译文（注明所依据的原文版本）

```bash
# 段 1（segment_id=1）提交基于原文 v1 的译文
curl -s -X PUT localhost:8000/segments/1/translations \
  -H 'Content-Type: application/json' \
  -d '{"target_text": "Bonjour.", "source_version_no": 1}'
```

### 4. 确认译文

```bash
curl -s -X POST localhost:8000/segments/1/translations/confirm \
  -H 'Content-Type: application/json' \
  -d '{"target_text": "Bonjour.", "source_version_no": 1}'
```

段 2 同理（`segment_id=2`，译文 `"Qui es-tu ?"`）。

### 5. 发布定版（每段都已确认才成功）

```bash
curl -s -X POST localhost:8000/manuscripts/1/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1}'
```

若有段未确认，返回 `422` 并在 `detail.blocked_segments` 列出阻塞段；
若 `expected_revision` 过期，返回 `409 revision_conflict`；两种情况均不增加版号。

### 6. 改写原文（旧译文自动失效为 pending_review）

```bash
curl -s -X PUT localhost:8000/manuscripts/1/segments/1/source \
  -H 'Content-Type: application/json' \
  -d '{"text": "你好呀。", "expected_revision": 1}'
```

响应中可看到：段 1 变为 `source_version_no=2`、状态 `pending_review`，
段 2 仍为 `confirmed`，工作稿 `revision` 变为 2。

此时用旧版本号提交/确认译文会被拒绝：

```bash
curl -i -X POST localhost:8000/segments/1/translations/confirm \
  -H 'Content-Type: application/json' \
  -d '{"target_text": "x", "source_version_no": 1}'
# HTTP/1.1 409 ... {"detail":{"code":"stale_source_version", ...}}
```

### 7. 重新提交、确认并发布下一版

```bash
curl -s -X PUT localhost:8000/segments/1/translations \
  -H 'Content-Type: application/json' \
  -d '{"target_text": "Salut.", "source_version_no": 2}'

curl -s -X POST localhost:8000/segments/1/translations/confirm \
  -H 'Content-Type: application/json' \
  -d '{"target_text": "Salut.", "source_version_no": 2}'

curl -s -X POST localhost:8000/manuscripts/1/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2}'
```

### 8. 按版号回看定版（旧版永不改变）

```bash
curl -s localhost:8000/manuscripts/1/publications       # 定版列表
curl -s localhost:8000/manuscripts/1/publications/1     # 第 1 版双语快照
curl -s localhost:8000/manuscripts/1/publications/2     # 第 2 版双语快照
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/manuscripts` | 创建稿件（`title` + 有序 `segments`） |
| `GET` | `/manuscripts` | 稿件列表 |
| `GET` | `/manuscripts/{id}` | 查询工作稿：各段原文版本、译文与状态 |
| `PUT` | `/manuscripts/{id}/segments/{position}/source` | 改写原文（乐观锁 `expected_revision`） |
| `PUT` | `/segments/{segment_id}/translations` | 提交译文（注明 `source_version_no`） |
| `POST` | `/segments/{segment_id}/translations/confirm` | 确认译文（注明 `source_version_no`） |
| `POST` | `/manuscripts/{id}/publish` | 原子发布定版（`expected_revision`） |
| `GET` | `/manuscripts/{id}/publications` | 定版列表 |
| `GET` | `/manuscripts/{id}/publications/{version_no}` | 按版号读取不可变双语定版 |
| `GET` | `/health` | 健康检查 |

## 数据模型要点

- `manuscript.revision`：工作稿当前修订号（每次原文改写 +1）。
- `segment_version`：原文版本只增不改，永久保留历史版本。
- `translation`：每段一行，重提交即覆盖，记录所依据的原文版本号；
  原文改写时 `confirmed → pending_review`。
- `publication` + `publication_segment`：发布瞬间的整篇双语**文本快照**，
  与工作稿后续变化完全解耦。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

测试包含完整业务流程（过期拒绝、单段失效、阻塞发布、修订冲突、旧版不可变）
以及基于真实 uvicorn 服务的多线程**改写/发布并发测试**
（4 个改写线程 + 1 个发布线程，校验每个定版内所有译文都与同版原文匹配，绝不混版）。
