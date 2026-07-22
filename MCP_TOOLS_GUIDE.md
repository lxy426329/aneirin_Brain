# Ombre Brain MCP 工具使用说明

## 概述

Ombre Brain 是一个基于 FastMCP 的记忆系统服务，提供记忆的存储、检索、关联和分析功能。本说明文档面向 AI 模型，介绍如何调用每个 MCP 工具。

---

## 工具分类总览

| 分类 | 工具名称 | 功能描述 |
|------|----------|----------|
| **记忆检索** | breath, query_memory | 关键词搜索、自动浮现、按条件筛选 |
| **记忆存储** | hold, grow | 存储单条记忆、日记归档 |
| **记忆管理** | trace, manage_record | 修改元数据、CRUD 操作 |
| **批量操作** | memory_batch_delete, smart_organize, weekly_organize | 批量删除、智能整理 |
| **关系管理** | manage_relation, manage_identity_relation, link_events | 建立因果链、身份关系 |
| **专项查询** | get_roster, get_experiences, get_memos, get_anchors, get_timelines, get_event_chains | 名册、经验、备忘录、锚点、时间链、事件链 |
| **AI 分析** | ai_analyze, ai_manage | AI 关联、智能管家 |
| **系统状态** | pulse, analytics, memory_directory | 系统状态、统计分析、目录摘要 |
| **数据导入导出** | memory_export, export_brain, import_brain | 数据导出、大脑备份恢复 |
| **回音壁管理** | review_digest, approve_action, reject_action | 审阅提案、批准/驳回提案 |
| **管家任务** | run_housekeeper, run_weekly_housekeeper | 手动触发每日/每周管家任务 |
| **静默预处理** | inject_context | 自动检索并注入上下文 |

---

## 详细工具说明

### 1. 记忆检索类

#### breath - 检索/浮现记忆

```python
breath(query="", max_tokens=5000, domain="", valence=-1, arousal=-1, 
       max_results=10, importance_min=-1, brief=True, type="", summary_report=True)
```

**参数说明：**
- `query`: 搜索关键词，不传或传空=自动浮现模式，有值=关键词检索模式
- `max_tokens`: 返回总 token 上限（默认 5000，最大 20000）
- `domain`: 按主题域筛选，逗号分隔（如"工作,生活"）
- `valence`: 效价坐标 0~1（-1 忽略），0=负面，1=正面
- `arousal`: 唤醒度坐标 0~1（-1 忽略），0=平静，1=兴奋
- `max_results`: 返回数量上限（默认 10，最大 50）
- `importance_min`: >=1 时按重要度降序返回（不走语义搜索）
- `brief`: 返回格式，true=简洁格式（元数据+摘要），false=完整格式
- `type`: 按层过滤，可选 identity/pattern/event/feel，不传全层返回
- `summary_report`: 对未完全展示的记忆生成快速总结

**使用场景：**
- `breath()` - 自动浮现最近记忆
- `breath(query="工作项目")` - 关键词搜索
- `breath(domain="工作", importance_min=8)` - 按领域和重要度筛选
- `breath(valence=0.8, arousal=0.6)` - 按情感坐标筛选

---

#### query_memory - 通用记忆查询

```python
query_memory(query="", mode="search", **kwargs)
```

**参数说明：**
- `mode`: 模式，可选 search/float/status/directory/recent
  - `search`: 关键词搜索（调用 breath）
  - `float`: 自动浮现（调用 breath，brief=True）
  - `status`: 系统状态（调用 pulse）
  - `directory`: 目录摘要（调用 memory_directory）
  - `recent`: 最近事件（调用 summarize_recent_events）
- `query`: 搜索关键词（search 模式需要）
- `kwargs`: 传递给对应工具的额外参数

**使用场景：**
- `query_memory(mode="status")` - 获取系统状态
- `query_memory(query="会议", mode="search")` - 搜索会议相关记忆

---

### 2. 记忆存储类

#### hold - 存储单条记忆

```python
hold(content, tags="", importance=5, pinned=False, feel=False, 
     task_flag=False, source_bucket="", valence=-1, arousal=-1)
```

**参数说明：**
- `content`: 记忆内容（必填）
- `tags`: 标签，逗号分隔
- `importance`: 重要度 1~10（默认 5）
- `pinned`: 是否钉选（创建永久钉选桶）
- `feel`: 是否存储为 AI 感受模式（不参与普通浮现）
- `task_flag`: 是否标记为任务类记忆（用户脆弱状态时自动屏蔽）
- `source_bucket`: 源记忆桶 ID（feel 模式下标记源记忆为已消化）
- `valence`: 情感效价 0~1（仅 feel 模式有效）
- `arousal`: 情感唤醒度 0~1（仅 feel 模式有效）

**使用场景：**
- `hold(content="今天完成了项目文档")` - 存储普通记忆
- `hold(content="用户现在心情不太好", feel=True, valence=0.3, arousal=0.4)` - 存储 AI 感受
- `hold(content="完成周报", task_flag=True)` - 存储任务类记忆

---

#### grow - 日记归档

```python
grow(content)
```

**参数说明：**
- `content`: 日记内容（必填）

**功能描述：**
自动将长文本日记拆分为多个记忆桶，短内容（<30字）走快速路径。

**使用场景：**
- `grow(content="今天上午开了会议，下午写了代码，晚上和朋友聚餐...")`

---

### 3. 记忆管理类

#### trace - 修改记忆元数据或内容

```python
trace(bucket_id, name="", domain="", valence=-1, arousal=-1, importance=-1, 
      tags="", resolved=-1, force_resolved=-1, pinned=-1, digested=-1, 
      task_flag=-1, content="", delete=False)
```

**参数说明：**
- `bucket_id`: 记忆桶 ID（必填）
- `name`: 修改名称
- `domain`: 修改主题域（逗号分隔）
- `valence`: 修改效价 0~1（-1=不改）
- `arousal`: 修改唤醒度 0~1（-1=不改）
- `importance`: 修改重要度 1~10（-1=不改）
- `tags`: 修改标签（逗号分隔）
- `resolved`: 1=沉底/0=激活（-1=不改）
- `force_resolved`: 强制沉底（用于 task_flag=True 的桶）
- `pinned`: 1=钉选/0=取消（-1=不改）
- `digested`: 1=隐藏/0=取消隐藏（-1=不改）
- `task_flag`: 1=标记任务类/0=取消（-1=不改）
- `content`: 替换桶正文
- `delete`: True=删除该记忆桶

**使用场景：**
- `trace(bucket_id="xxx", resolved=1)` - 沉底记忆
- `trace(bucket_id="xxx", importance=8)` - 提升重要度
- `trace(bucket_id="xxx", delete=True)` - 删除记忆

---

#### manage_record - 通用记录管理

```python
manage_record(action, record_type="", record_id="", **kwargs)
```

**参数说明：**
- `action`: 操作类型，可选 create/update/get/list/delete/apply
- `record_type`: 记录类型，可选 identity/roster/pattern/candlestick/experience/annual_ring
- `record_id`: 记录 ID（仅 get/update/delete/apply 需要）
- `kwargs`: 其他参数，根据 record_type 不同

**支持的 record_type：**

| record_type | 说明 | create 参数 |
|-------------|------|------------|
| `identity` | 身份档案 | name, description, relationships |
| `roster` | 名册（identity 别名） | 同 identity |
| `pattern` | 行为模式 | name, description, triggers |
| `candlestick` | 烛台备忘录 | content, bucket_id, title |
| `experience` | 经验 | content/detail/text, exp_type, title/name, source |
| `annual_ring` | 年轮 | content/detail/text, title/name |

**使用场景：**
- `manage_record(action="create", record_type="identity", name="张三", description="同事")`
- `manage_record(action="list", record_type="experience")`
- `manage_record(action="apply", record_type="experience", record_id="xxx")`

---

### 4. 批量操作类

#### memory_batch_delete - 批量删除

```python
memory_batch_delete(bucket_ids)
```

**参数说明：**
- `bucket_ids`: 多个记忆桶 ID，逗号分隔

**使用场景：**
- `memory_batch_delete(bucket_ids="id1,id2,id3")`

---

#### smart_organize - 智能整理

```python
smart_organize(days=30, importance_drop=2)
```

**参数说明：**
- `days`: 超过多少天未激活视为过期（默认 30）
- `importance_drop`: 权重降低幅度 1~5（默认 2）

**规则：**
- 跳过钉选、已解决、永久型记忆
- 跳过重要度 ≤2 的记忆
- 跳过最近激活的记忆

---

#### weekly_organize - 每周内容整理

```python
weekly_organize()
```

**功能描述：**
生成本周新增记忆报告（仅报告，不调整权重）

---

#### tag_normalize - 标签归一化

```python
tag_normalize(action="run")
```

**参数说明：**
- `action`: run(立即执行), status(查看状态)

**功能描述：**
将非标准标签映射到泛化标签树，后台自动每周或每 50 条记录运行一次

---

### 5. 关系管理类

#### manage_relation - 通用关联管理

```python
manage_relation(action, bucket_id="", target_id="", **kwargs)
```

**参数说明：**
- `action`: 操作类型，可选 link/parent/chain/importance
  - `link`: 建立双向关联
  - `parent`: 建立父子层级
  - `chain`: 添加到事件链
  - `importance`: 评估重要度维度
- `bucket_id`: 源桶 ID
- `target_id`: 目标桶 ID（link/parent/chain 需要）
- `kwargs`: 
  - `position`: 事件链位置（chain 模式）
  - `impact/duration/emotional_intensity/recurrence/interconnectedness`: 重要度维度（0~10）

**使用场景：**
- `manage_relation(action="link", bucket_id="id1", target_id="id2")`
- `manage_relation(action="parent", bucket_id="child", target_id="parent")`
- `manage_relation(action="importance", bucket_id="id1", impact=8, duration=5)`

---

#### manage_identity_relation - 管理身份关系

```python
manage_identity_relation(action, from_id=None, to_id=None, relation_type="朋友", base_weight=5.0)
```

**参数说明：**
- `action`: 操作类型，可选 add/query/update_weight
  - `add`: 建立身份之间的关系
  - `query`: 查询身份的所有关系
  - `update_weight`: 更新关系权重
- `from_id`: 源身份 ID（add/query/update_weight 需要）
- `to_id`: 目标身份 ID（add/update_weight 需要）
- `relation_type`: 关系类型（朋友/同事/家人等，默认"朋友"）
- `base_weight`: 基础权重 1.0~10.0（默认 5.0）

**使用场景：**
- `manage_identity_relation(action="add", from_id="id1", to_id="id2", relation_type="同事")`
- `manage_identity_relation(action="query", from_id="id1")`
- `manage_identity_relation(action="update_weight", from_id="id1", to_id="id2", base_weight=8.0)`

---

#### link_events - 建立因果链

```python
link_events(prev_id, next_id)
```

**参数说明：**
- `prev_id`: 前因事件 ID（更早发生）
- `next_id`: 后果事件 ID（更晚发生）

**使用场景：**
- `link_events(prev_id="cause_id", next_id="effect_id")`

---

#### manage_relation (身份关系) - 管理身份关系

```python
manage_relation(action, from_id=None, to_id=None, relation_type="朋友", base_weight=5.0)
```

**参数说明：**
- `action`: add(建立关系)/query(查询关系)/update_weight(更新权重)
- `from_id`: 源身份 ID
- `to_id`: 目标身份 ID（query 时可省略）
- `relation_type`: 关系类型（朋友/同事/家人等）
- `base_weight`: 基础权重 1.0~10.0

**使用场景：**
- `manage_relation(action="add", from_id="id1", to_id="id2", relation_type="同事")`
- `manage_relation(action="query", from_id="id1")`

---

### 6. 专项查询类

#### get_roster - 查询名册

```python
get_roster(name=None)
```

**参数说明：**
- `name`: 可选，人物姓名或别名，不传返回所有人

**使用场景：**
- `get_roster()` - 获取所有人
- `get_roster(name="张三")` - 精确查找

---

#### get_experiences - 获取经验

```python
get_experiences()
```

**功能描述：**
获取所有经验（年轮）记录，包含应用次数、来源等信息

---

#### get_memos - 获取烛台备忘录

```python
get_memos()
```

**功能描述：**
获取所有烛台（备忘录）记录

---

#### get_anchors - 获取行为与情绪锚点

```python
get_anchors(active_only=False)
```

**参数说明：**
- `active_only`: 是否只返回正在生效的锚点

**功能描述：**
获取所有行为与情绪锚点（触发词+情绪基调+行为禁忌）

---

#### get_timelines - 获取时间链

```python
get_timelines()
```

**功能描述：**
获取所有时间链列表，用于查阅事件发展脉络

---

### 7. AI 分析类

#### ai_analyze - AI 分析工具

```python
ai_analyze(task, bucket_id="", query="")
```

**参数说明：**
- `task`: 任务类型，可选 link/find/chain/summarize/classify
  - `link`: AI 自动建立记忆关联
  - `find`: 查找语义相关记忆
  - `chain`: 构建事件链
  - `summarize`: 总结记忆
  - `classify`: 分类记忆
- `bucket_id`: 记忆桶 ID（link/summarize/classify 需要）
- `query`: 搜索关键词（find 模式需要）

**使用场景：**
- `ai_analyze(task="link", bucket_id="xxx")` - 自动建立关联
- `ai_analyze(task="summarize", bucket_id="xxx")` - 总结记忆

---

#### ai_manage - AI 管家

```python
ai_manage(request)
```

**参数说明：**
- `request`: 自然语言请求

**功能描述：**
智能分析用户需求并自动调用合适的工具，支持多轮工具调用和任务总结

**使用场景：**
- `ai_manage(request="帮我整理一下最近30天的过期记忆")`

---

### 8. 系统状态类

#### pulse - 系统状态

```python
pulse(include_archive=False)
```

**参数说明：**
- `include_archive`: 是否包含归档记忆

**功能描述：**
返回系统状态概览 + 所有记忆桶列表

---

#### analytics - 统计分析

```python
analytics()
```

**功能描述：**
获取记忆库统计分析数据（情绪分布、类型统计、活跃度趋势）

---

#### memory_directory - 记忆目录

```python
memory_directory(detail_level="medium")
```

**参数说明：**
- `detail_level`: brief(仅统计)/medium(详细分类)/full(完整目录)

**功能描述：**
生成记忆库的简洁目录摘要

---

### 9. 数据导入导出类

#### memory_export - 导出记忆

```python
memory_export(export_type="all")
```

**参数说明：**
- `export_type`: 导出类型，可选 all/dynamic/permanent/identity/pattern/feel

**使用场景：**
- `memory_export(export_type="identity")` - 导出所有身份档案

---

#### export_brain - 导出大脑数据

```python
export_brain(output_path="")
```

**参数说明：**
- `output_path`: 输出 zip 文件路径，不指定则自动生成

**功能描述：**
将 buckets/ 目录和 embeddings.db 打包为 zip 文件

---

#### import_brain - 导入大脑数据

```python
import_brain(zip_path, overwrite=False)
```

**参数说明：**
- `zip_path`: zip 文件路径
- `overwrite`: 是否覆盖现有数据

---

### 10. 特殊功能类

#### dream - 自省读取

```python
dream()
```

**功能描述：**
读取最近新增的记忆桶，供 AI 自省。读完后可以 `trace(resolved=1)` 放下，或 `hold(feel=True)` 写感受。

---

#### trace_chain - 追溯因果链

```python
trace_chain(bucket_id, direction="both", max_depth=3)
```

**参数说明：**
- `bucket_id`: 记忆桶 ID
- `direction`: 遍历方向，previous(前因)/next(后果)/both(双向)
- `max_depth`: 最大遍历深度（默认 3）

**功能描述：**
追溯某条记忆的因果链，通过指针直接调出关联事件

---

#### summarize_recent_events - 最近事件概括

```python
summarize_recent_events(days=7, max_events=10)
```

**参数说明：**
- `days`: 天数（默认 7）
- `max_events`: 最大事件数（默认 10）

**功能描述：**
获取最近一段时间内的记忆事件概括，包含 AI 生成的总结

---

## 使用最佳实践

### 1. 检索优先原则
- 先使用 `breath()` 或 `query_memory()` 了解记忆库状态
- 再根据检索结果进行精确操作

### 2. 记忆生命周期管理
- **存储**: `hold()` 或 `grow()`
- **检索**: `breath()` 或 `query_memory()`
- **整理**: `smart_organize()` 或 `weekly_organize()`
- **回顾**: `dream()` 或 `summarize_recent_events()`
- **归档**: `trace(resolved=1)` 或 `trace(digested=1)`

### 3. 关联建立流程
- 使用 `link_events()` 建立事件间因果关系
- 使用 `manage_relation(action="link")` 建立记忆间关联
- 使用 `ai_analyze(task="link")` AI 自动建立语义关联

### 4. 脆弱状态保护
- 系统会自动检测用户脆弱状态（抑郁、生病、疲惫）
- `task_flag=True` 的记忆桶在脆弱状态下会被自动屏蔽
- AI 使用 `feel` 模式存储感受时，会标记源记忆为已消化

### 5. 工具选择建议
- **简单存储**: `hold()`
- **长文本归档**: `grow()`
- **通用查询**: `query_memory()`
- **精确操作**: `trace()` 或 `manage_record()`
- **复杂任务**: `ai_manage()`

---

## 概念映射

| 概念 | 工具/record_type | 说明 |
|------|------------------|------|
| 年轮 | `annual_ring` / `experience` | 从事件中获得的经验教训 |
| 烛台 | `candlestick` / `get_memos` | 重要备忘事项 |
| 名册 | `roster` / `identity` / `get_roster` | 人物身份档案 |
| 锚点 | `get_anchors` | 行为触发条件与情绪边界 |
| 时间链 | `get_timelines` | 事件发展的时间脉络 |
| 因果链 | `trace_chain` / `link_events` | 事件间的因果关系 |
| 感受 | `hold(feel=True)` | AI 的第一人称感受记录 |
| 任务 | `hold(task_flag=True)` | 需要完成的待办事项 |
