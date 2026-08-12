# Ombre Brain

一个基于 MCP 协议的长期情绪记忆系统。基于 Russell 效价/唤醒度坐标打标，Obsidian 做存储层，带遗忘曲线和向量语义检索。**不限于 Claude —— 任何支持 MCP 协议的 AI 客户端均可接入**（Claude Desktop、Claude Code、Cline、Cursor 等）。

> **⚠️ 备用链接**
> Gitea 备用地址（GitHub 访问有问题时用）：
> **https://git.p0lar1s.uk/P0lar1s/Ombre_Brain**

---

## 快速开始（Docker Hub 预构建镜像）

不需要 clone 代码，不需要 build，三步搞定。

### 第零步：装 Docker Desktop

1. 打开 [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
2. 下载对应你系统的版本（Mac / Windows / Linux）
3. 安装、打开，看到 Docker 图标在状态栏里就行了
4. **Windows 用户**：安装时会提示启用 WSL 2，点同意，重启电脑

### 第一步：打开终端

| 系统 | 怎么打开 |
|---|---|
| **Mac** | 按 `⌘ + 空格`，输入 `终端` 或 `Terminal`，回车 |
| **Windows** | 按 `Win + R`，输入 `cmd`，回车；或搜索「PowerShell」 |
| **Linux** | `Ctrl + Alt + T` |

### 第二步：创建工作文件夹

```bash
mkdir ombre-brain && cd ombre-brain
```

### 第三步：获取 API Key（免费）

1. 打开 [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. 用 Google 账号登录
3. 点击 **「Create API key」**
4. 复制生成的 key（一长串字母数字）

> 没有 Google 账号？也行，API Key 留空也能跑，只是脱水压缩效果差一点。

### 第四步：创建配置文件并启动

```bash
# 下载用户版 compose 文件
curl -O https://raw.githubusercontent.com/P0luz/Ombre-Brain/main/docker-compose.user.yml

# 创建 .env 文件——把 your-key-here 换成第三步拿到的 key
echo "OMBRE_API_KEY=your-key-here" > .env

# 拉取镜像并启动（第一次会下载约 500MB）
docker compose -f docker-compose.user.yml up -d
```

### 第五步：验证

```bash
curl http://localhost:8000/health
```

看到类似这样的输出就是成功了：
```json
{"status":"ok","buckets":0,"decay_engine":"stopped"}
```

浏览器打开前端 Dashboard：**http://localhost:8000/dashboard**

### 第六步：接入 MCP 客户端

**Claude Desktop**（Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`）：
```json
{
  "mcpServers": {
    "ombre-brain": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

> 如果你部署在远程服务器（如 Render），使用 `streamable-http` 协议，客户端配置为：
> ```json
> {
>   "mcpServers": {
>     "ombre-brain": {
>       "type": "streamable-http",
>       "url": "https://你的域名/mcp"
>     }
>   }
> }
> ```

**Claude Code**（项目根目录 `.claude/settings.json`）：
```json
{
  "mcpServers": {
    "ombre-brain": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

**Cline / 其他 MCP 客户端**：按各客户端文档配置 MCP 服务器。本地使用 `stdio` 或 `streamable-http`，远程使用 `sse` 协议（URL 指向 `https://你的域名/sse`）。重启客户端后，工具列表里应出现 `breath`、`hold`、`grow` 等。

**回音壁页面**：浏览器打开 **http://localhost:8000/echo-chamber**，查看 AI 管家生成的每日/每周摘要、待审批提案和冲突检测结果。

---

## 从源码部署（Docker）

适合想自己改代码、或者不想用预构建镜像的用户。

**第一步：拉取代码**

```bash
git clone https://github.com/P0luz/Ombre-Brain.git
cd Ombre-Brain
```

**第二步：创建 `.env` 文件**

```
OMBRE_API_KEY=你的API密钥
```

**第三步：配置 `docker-compose.yml`（指向你的 Obsidian Vault）**

找到这一行：
```yaml
- ./buckets:/data
```

改成你的 Obsidian Vault 里 `Ombre Brain` 文件夹的路径，例如：
```yaml
- /Users/你的用户名/Documents/Obsidian Vault/Ombre Brain:/data
```

**第四步：启动**

```bash
docker compose up -d
```

验证：`docker logs ombre-brain`，看到 `Uvicorn running on http://0.0.0.0:8000` 说明成功了。

---

## 核心功能

### 情感坐标打标

每条记忆使用 **Russell 环形情感模型** 的两个连续维度标记情感：

| 维度 | 范围 | 含义 |
|---|---|---|
| **Valence（效价）** | 0.0 ~ 1.0 | 0=负面（难过/愤怒），1=正面（开心/兴奋） |
| **Arousal（唤醒度）** | 0.0 ~ 1.0 | 0=平静/放松，1=激动/紧张 |

系统根据 (valence, arousal) 坐标自动映射到离散情感标签（如 `joy`、`sadness`、`anger`、`calm`），同时保留原始连续坐标用于计算。情感数据同时写入 Markdown 文件 frontmatter 和 `feel` 专用桶（长期情绪分析用）。

存储格式示例：
```yaml
---
valence: 0.82
arousal: 0.65
dominant_emotion: joy
emotions:
  - label: joy
    intensity: 0.8
  - label: calm
    intensity: 0.2
---
```

### 双通道检索

两条检索路径同时进行，结果合并后去重排序：

1. **关键词通道**：使用 `rapidfuzz` 对记忆桶正文 + 标签做模糊匹配，适合精确关键词查找
2. **语义通道**：使用 embedding 模型计算 query 与存储向量的 cosine similarity（3072 维），适合语义联想——"今天很累"能找到"睡眠不足"、"加班"等语义相关记忆

### 自然遗忘（Decay Engine）

基于改进版艾宾浩斯遗忘曲线，后台定时（默认 24h）扫描所有非永久记忆桶：

```
衰减因子 = e^(-λ * Δt)
最终衰减阶段 = 原始得分 × 衰减因子 × 情绪增强因子
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `decay.lambda` | 0.05 | 衰减速率。越大忘得越快 |
| `decay.threshold` | 0.3 | 得分低于此值 → 归档到 `archive/` 目录 |
| `check_interval_hours` | 24 | 每次衰减检查的间隔时间 |
| `arousal_boost` | 0.8 | 高唤醒度记忆的衰减抵抗力加成 |

**衰减阶段**：
- `decay_stage=0`：初始状态，无衰减
- `decay_stage=1`：轻度衰减（保留摘要）
- `decay_stage=2`：中度衰减（保留 one_line_summary）
- `decay_stage=3`：深度衰减（标记 digested，几乎不参与检索）

**永久/钉选/保护/feel 类记忆不受衰减影响**。

### 权重池浮现

记忆不是被动检索的，它们会主动浮现。`breath()` 无参数调用时：

1. 收集最近 7 天未解决（`resolved=False`）的记忆桶
2. 按权重排序：`权重 = 情绪唤醒度 × 0.3 + 显式优先级 × 0.2 + 时间亲近度 × 0.5`
3. 选取 TOP-3 作为主动推送的记忆
4. 若检测到用户处于脆弱状态（见下文任务屏蔽），自动过滤 `task_flag=True` 的桶

### 三步检索管线

`breath(query=...)` 带参数调用时触发完整三步检索：

```
breath(query="今天很累")
         │
    ┌────┴──────────────────────────────────────────────────────────────┐
    │ Step 1: 强锚点检索                                                │
    │ 规则：纯静态判定，不触发 LLM                                       │
    │ 条件：pinned=True OR protected=True（即 is_anchor=True）            │
    │ 返回：所有符合条件的安全锚点（行为准则/核心原则）                    │
    ├───────────────────────────────────────────────────────────────────┤
    │ Step 2: 年轮经验提取                                              │
    │ 操作：用 query 在 pattern / identity 层做语义匹配                  │
    │ 返回：TOP-3 经验模式（带 apply_count / confidence 排序）           │
    │ 冷却：同一桶 5 分钟内不重复注入                                    │
    ├───────────────────────────────────────────────────────────────────┤
    │ Step 3: 记忆桶混合检索                                            │
    │ 操作：在 dynamic/ 目录全量扫描，五维加权评分                       │
    │                                                                    │
    │ 五维得分说明（各维取值范围 [0, 1] 归一化）：                        │
    │   ① Emotion_Arousal  = 当前情绪唤醒度（arousal）                   │
    │   ② Explicit_Priority = 1.0(钉选) / 0.8(高重要性) / 0.0(普通)     │
    │   ③ Vector_Similarity = query 与此桶向量的 cosine similarity      │
    │   ④ Topic_Relevance    = query 与此桶域标签的语义匹配度            │
    │   ⑤ Time_Proximity     = 最近 24h=1.0 → 30天→0.1 的指数衰减      │
    │                                                                    │
    │ 加权求和后除以总权重归一化到 [0, 1]：                               │
    │   Final_Score = (3.0×① + 4.0×② + 3.0×③ + 2.0×④ + 1.5×⑤) / 13.5 │
    │                                                                    │
    │ 分层返回规则：                                                     │
    │   Final_Score ≥ 0.7  → 返回完整正文内容（精排后硬截断 Top 3）      │
    │   0.4 ≤ score < 0.7 → 仅返回 one_line_summary（异步预生成）        │
    │   score < 0.4       → 完全跳过，不返回任何内容                     │
    ├───────────────────────────────────────────────────────────────────┤
    │ Step 3 Fallback: 保底机制                                          │
    │ 条件：Step 3 所有桶的 score < 0.4                                  │
    │ 操作：从最近 7 天 resolved=False 的记忆桶中随机抽取 2 条            │
    │ 标记：返回时标注 [fallback: random recent]                         │
    └───────────────────────────────────────────────────────────────────┘
```

> **one_line_summary 异步生成**：在 `hold()` / `grow()` 写入落库时，后台异步调用 LLM 生成一句话摘要并存入 Markdown frontmatter 的 `one_line_summary` 字段。`breath()` 检索时只做纯字符串读取，**绝不触发任何 LLM 调用**，保证检索延迟可控。

### 任务屏蔽机制

系统区分两种场景，分别采用不同的屏蔽策略：

**被动浮现模式（breath() 无参数）**：
1. 从最新的 `feel` 桶读取情绪坐标 (valence, arousal)
2. 若 `valence < 0.3` 且 `arousal < 0.3`（低唤醒负面情绪）或标签含"生病/疲惫/焦虑"等关键字 → 判定为脆弱状态
3. 脆弱状态下，全局过滤 `task_flag=True` 的记忆桶，防止 AI 主动推送任务

**主动检索模式（breath(query=...) 带参数）**：
1. 计算 query 与概念锚点 "task/todo/job/任务/工作/待办" 的向量相似度
2. 若相似度 ≥ 0.5 → 认定为"用户主动询问任务"
3. 主动询问时**绕过**任务屏蔽，正常返回所有匹配记忆桶
4. 若相似度 < 0.5 → 保持屏蔽（被动检索痕迹，不打扰用户）

### 并发安全

系统涉及两个后台进程可能冲突：

| 进程 | 写入内容 | 冲突对象 |
|---|---|---|
| `decay_engine` | 修改 `decay_stage`、`decay_factor` | 同一 Markdown 文件 |
| `dream` | 修改 `resolved=True` | 同一 Markdown 文件 |

保护措施：
- **SQLite WAL 模式**：`embeddings.db` 所有连接启用 `PRAGMA journal_mode=WAL` + `busy_timeout=5000`，允许多个写操作并发执行，消除 "database is locked" 错误
- **Markdown 文件锁**：`bucket_manager` 的 `update()`、`touch()`、`archive()`、`delete()`、`restore()` 及关联操作均使用 `threading.Lock` 包裹完整读-改-写周期（`frontmatter.load` → 修改元数据 → `frontmatter.dumps` 写回），确保两个进程同时写入同一文件时互斥
- **启动竞态防护**：decay_engine 与 housekeeper 的懒启动加 `asyncio.Lock`，杜绝并发首次调用创建重复后台任务
- **Embedding 熔断重试**：向量 API 失败自动退避重试（0.5s/1s），连续 3 次失败熔断 60 秒，避免拖垮整个系统

### 例假周期追踪

系统内置例假周期追踪功能，支持记录、预测和自动提醒：

**核心功能：**
- **记录例假**：使用 `record_cycle(start_date, symptoms, duration, ...)` 记录每次例假的详细信息
- **自动预测**：基于历史记录自动计算平均周期长度，预测下次例假日期
- **智能提醒**：当距离预测日期还有 0-5 天时，`breath()` 自动显示提醒信息
- **可视化面板**：前端 Dashboard 提供专门的例假追踪视图，展示周期统计和历史记录

**使用方式：**

```python
# 记录例假
record_cycle(start_date="2026-07-03", symptoms="腹痛、腰酸", duration=5)

# 记录详细信息
record_cycle(start_date="2026-08-01", symptoms="轻微腹痛", flow_level="light", pain_level=3)
```

**预测算法：**
- 至少需要 2 次记录才能开始预测
- 过滤异常周期（20-45天范围之外）
- 基于平均周期长度预测下次日期

**提醒机制：**
- 距离预测日期 0 天：红色紧急提醒
- 距离预测日期 1 天：橙色警告提醒
- 距离预测日期 2-5 天：黄色提示提醒

### 隐私记忆库

支持将敏感记忆设为隐私锁定，前端无法直接查看内容：

- **MCP 锁定/解锁**：AI 调用者可使用 `lock_memory(bucket_id, password)` 锁定记忆，使用 `unlock_memory(bucket_id)` 解除锁定
- **前端密码查看**：隐私记忆在列表中显示为 `[隐私记忆]`，点击查看时需输入正确密码才能显示完整内容
- **前端管理**：也可在前端详情页直接设置/解除隐私锁定
- **MCP 完全访问**：AI 调用者通过 `breath`、`get_memos` 等工具仍可正常读取完整内容，不受锁定限制

```python
# 锁定记忆
lock_memory(bucket_id="xxx", password="mypassword")

# 解除锁定
unlock_memory(bucket_id="xxx")
```

### 外部记忆写入接口

提供稳定的 HTTP 写入接口，供夜伴等外部系统直接写入记忆，**不依赖模型自行决定是否调用 MCP**：

- **接口**：`POST /api/hold`
- **认证**：session cookie，或 `X-API-Key` header（设置 `OMBRE_EXTERNAL_API_KEY` 环境变量后启用，适合服务端到服务端调用）
- **复用核心逻辑**：与 MCP `hold()` 完全同一条写入路径（情感打标、查重合并、异步 summary/embedding）

**请求体（JSON）：**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | 是 | 记忆正文 |
| `valence` | float (0~1) | 否 | 效价，提供时优先于自动打标 |
| `arousal` | float (0~1) | 否 | 唤醒度，提供时优先于自动打标 |
| `tags` | list/string | 否 | 标签列表或逗号分隔字符串 |
| `importance` | int (1~10) | 否 | 重要度，默认 5 |
| `pinned` | bool | 否 | 钉选为永久记忆 |
| `protected` | bool | 否 | 受保护（不参与合并/衰减） |
| `task_flag` | bool | 否 | 任务类记忆 |
| `source` | string | 否 | 来源标记，如 `yeeban` / `yeeban_status` / `yeeban_daily` |
| `title` | string | 否 | 自定义记忆名称 |
| `feel` | bool | 否 | 存储为感受型记忆 |

**示例请求：**

```bash
curl -X POST http://localhost:8000/api/hold \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-external-key" \
  -d '{
    "content": "今天和妈妈去公园散步，聊了很多小时候的事",
    "valence": 0.85,
    "arousal": 0.4,
    "tags": ["家庭", "散步"],
    "importance": 7,
    "source": "yeeban_daily"
  }'
```

**示例响应：**

```json
{
  "success": true,
  "bucket_id": "candmz4tlsbpe",
  "merged": false,
  "valence": 0.85,
  "arousal": 0.4,
  "action": "new",
  "message": "新建→与妈妈散步聊起童年 家庭"
}
```

**错误返回**（缺 content、数值越界等均返回 `400` + `success:false`）：

```json
{ "success": false, "error": "valence 越界，必须位于 0~1" }
```

### 轻量记忆检索接口

为外部系统（夜伴）提供省 token 的稳定检索接口，与 MCP `breath(lightweight=True)` 共用同一条路径：

- **接口**：`GET /api/breath` 或 `POST /api/breath`（JSON body 同字段）
- **认证**：session cookie 或 `X-API-Key` header（设置 `OMBRE_EXTERNAL_API_KEY` 后启用）
- **设计**：每条仅返回一句话摘要 + 关键元数据，不返回大段正文；无结果时返回明确空结构，不做 fallback

**请求参数：**

| 参数 | 类型 | 说明 |
|---|---|---|
| `query` | string | 检索词；空 = 被动浮现最近未解决记忆 |
| `limit` | int | 返回条数上限（默认 5，最大 20） |
| `min_score` | float (0~1) | 最低相关度过滤（仅查询模式有效） |
| `recent_days` | int | 仅返回最近 N 天（0 = 不过滤） |
| `type` | string | 类型过滤：identity / pattern / event / feel |
| `domain` | string | 领域过滤（逗号分隔） |
| `valence` / `arousal` | float (0~1) | 情感坐标筛选 |

**示例请求：**

```bash
curl "http://localhost:8000/api/breath?query=压力&limit=3&min_score=0.2&recent_days=30" \
  -H "X-API-Key: your-external-key"
```

**示例响应：**

```json
{
  "success": true,
  "mode": "lightweight",
  "query": "压力",
  "count": 3,
  "results": [
    {
      "bucket_id": "candmz4abc123",
      "name": "项目上线压力大",
      "summary": "本周项目上线压力很大，连续加班三天",
      "valence": 0.25,
      "arousal": 0.7,
      "tags": ["工作", "压力"],
      "created": "2026-07-28",
      "score": 0.83
    }
  ]
}
```

**无结果时**（明确空结构，不 fallback）：

```json
{ "success": true, "mode": "lightweight", "query": "不存在的词", "count": 0, "results": [] }
```

### 数据导出/导入

**导出**：将 `buckets/` 目录（含所有子目录）和 `embeddings.db` 打包为 zip 文件。

```python
export_brain()                                    # 默认输出到 buckets/export/
export_brain(output_path="/path/to/brain.zip")    # 自定义路径
```

**导入**：从 zip 文件恢复数据到本地 buckets 目录。

```python
import_brain(zip_path="/path/to/brain.zip")                  # 不覆盖已有文件
import_brain(zip_path="/path/to/brain.zip", overwrite=True)  # 覆盖已存在的桶
```

### 记忆冲突检测

管家每日扫描新记忆与旧记忆的冲突，检测类型包括：

| 冲突类型 | 检测规则 |
|---|---|
| **偏好冲突** | 新记忆中"喜欢/讨厌"与旧记忆相反 |
| **健康冲突** | 新记忆中"生病/康复"状态与旧记忆矛盾 |
| **状态冲突** | 新记忆中"正在做/已完成"与旧记忆不一致 |
| **事实冲突** | 同一事件的时间、地点、人物描述不一致 |

冲突检测结果提交到回音壁，等待主 AI 审批处理。

### 情绪基调锚定

系统分析每日对话，自动分配情绪标签：

| 标签 | 触发关键词 |
|---|---|
| `anxious` | 焦虑、不安、烦躁、睡不着、压力大 |
| `unwell` | 痛、疼、难受、生病、感冒、发烧、疲惫 |
| `sad` | 难过、伤心、悲伤、失望、想哭 |
| `happy` | 开心、高兴、快乐、兴奋 |
| `angry` | 生气、愤怒、不满、烦躁 |

当检测到连续 3 天低情绪（valence < 0.3），系统会在回音壁生成情绪关怀提案。

### 回音壁（Echo Chamber）

系统级中间态数据存储区，专门存放 AI 管家的工作总结与提案：

- **每日摘要**：管家旧管线（`run_housekeeper` / `daily_review(run_pipeline=True)`）生成的当日记忆总结
- **每周摘要**：管家自动生成的当周事件链合并报告
- **日终扫描报告**：每次 `daily_review` 落一份 `daily_review` 类型扫描报告（候选列表存档，非待审批动作）
- **待审批提案**：记忆冲突、清理提案、事件链合并提案、人物收录提案等需要主 AI 裁决的事项
- **批准即执行**：批准提案时自动执行对应操作——cleanup 删除过期记忆并同步清理向量库、conflict 标记旧记忆 `resolved=True` + `superseded_by`、chain_merge 合并事件链（节点去重 + 实体合并 + 摘要重构）、identity_proposal 在身份层创建人物档案（含主导情绪特征）
- **状态同步**：审批结果（approved / rejected）实时写回提案卡片，Dashboard 60 秒自动刷新，无缓存滞后

访问地址：**http://localhost:8000/echo-chamber**

### 每日日志系统

独立于记忆桶系统的每日日记存储区域，采用主 AI 产出、系统协助结构化的模式：

**协作流程**：
1. **主 AI**（日终或对话中）：依据对话理解写出今日事件摘要 `event_summary`，通过 `complete_journal(date, event_summary, mood_comment, emotion_tags)` 落库
2. **主 AI 补充**：`complete_journal` 可多次调用、幂等合并——情绪点评与标签独立更新，绝不覆盖为空；同一天重复调用不丢数据
3. **查询**：通过 `query_journal(date, keyword)` 按日期或关键词查询，仅在需要时调出

> 管家**不再自动生成日记草稿**：管家看不到聊天客户端中未写入 Brain 的对话全文，无法也不应编造日程。今日事件总结由主 AI 依据对话理解撰写后落库。

**隔离设计**：
- 日记存储在独立的 `journals/` 目录（与 `buckets/` 同级，物理隔离）
- **不参与** `breath()` 浮现、`inject_context()` 上下文注入、`list_all()` 列表
- 仅通过显式日期查询或关键词搜索访问

**幂等与容错**：
- **幂等合并**：同一天重复调用 `complete_journal` 不会丢数据——新值为空时保留旧值（摘要与点评各自独立更新，绝不覆盖为空）
- **日期容错**：`date` 为空或格式错乱时自动降级为当前 UTC 日期；`2026/8/1`、`2026年8月1日` 等格式自动归一化
- **标签容错**：`emotion_tags` 兼容字符串或数组，自动统一 `，`/`、` 分隔符并去重
- **并发安全**：写入采用线程锁包裹读-改-写，杜绝并发丢失更新

**日记结构**：

| 字段 | 说明 |
|---|---|
| `date` | 日期（YYYY-MM-DD） |
| `event_summary` | 事件摘要（主 AI 依据对话生成） |
| `mood_comment` | 情绪点评（主 AI 生成） |
| `emotion_tags` | 情绪标签（主 AI 生成） |
| `housekeeper_generated_at` | 事件摘要写入时间（字段名沿用历史命名） |
| `ai_completed_at` | 主 AI 补充时间 |

### 静默预处理中间件

在用户发送消息、调用主 AI API 之前自动执行：

1. 拦截用户输入，后台自动执行 Query 改写与混合检索（Hybrid Search + Rerank）
2. 自动匹配并拉取最新的【相关事件链 (Event Chain)】与【感官/状态标签 (Feel/Status)】
3. 将检索到的背景记忆以 `<context>` 结构静默拼接到用户 Prompt 头部，并在 `<context>` 上下包裹[系统硬性指令]（优先度校验 / 拒绝无依据联想 / 逻辑聚焦）与[输出要求]，约束主 AI 忠实于注入上下文、禁止臆测发散
4. 主 AI 无需额外发起检索 Tool Call，直接获得完备上下文

### 事件链（Event Chain）

长效跨天事件的持久化结构，包含：

| 字段 | 说明 |
|---|---|
| `chain_id` | 事件链唯一标识 |
| `topic` | 事件主题（AI 生成） |
| `status` | 进行中/已结案 |
| `timeline` | 按时间排序的节点数组，关联原始 memory_id |
| `summary` | 高度概括的事件背景（AI 生成） |
| `entities` | 核心实体词（人物/物品/概念，自动提取） |
| `related_chain_ids` | 共享实体的关联事件链 ID（图谱交叉索引） |

仅当同一主题跨越多个时间段（如病程跟进、备考、项目开发）被持续提及或跟进时，才会创建或追加至事件链。

**实体图谱与交叉索引**：

- **实体提取**：建链与追加节点时，从记忆正文（人物正则）、标签、`[[wikilink]]` 自动提取实体词，无需 LLM 调用
- **图谱融合**：每日/每周管家任务自动扫描全部事件链，两条链存在共享实体即互相加入 `related_chain_ids`（双向、去重、幂等）
- **检索增强**：`get_event_chains(include_related=True)` 附带各链的关联链摘要；`get_event_chain_detail(chain_id)` 返回单链完整详情 + 关联链的共享实体与摘要——"沿着时间线找演进，顺着实体网找关联"

**实体关联示例**：

```text
=== 事件链 chainA ===   [实体: 小明, 备考英语]
   关联链: chainB（备考英语考试）
   共享实体: 小明, 备考英语
```

### 管家与日终整理

> **核心原则**：管家不打断用户与主 AI 的日常对话；日终由主 AI 按提示词约定主动调用；管家只扫描已入库记忆、给出候选与可选方案，不做任何最终删改；最终决策权在主 AI。

**定位**

- 管家**不主动打断**用户与主 AI 的日常对话。
- 日终由主 AI 按提示词约定**主动调用**（`daily_review` 或 `run_housekeeper`）。
- 管家负责**扫描已入库记忆**（过期任务、长期低价值、重复、冲突等），提出候选与可选方案；删除 / 沉底 / 提权 / 合并 / 保留 / 改写等最终操作由主 AI 决定并调用 `trace` / `hold` 等工具执行。
- 管家**无法读取**聊天客户端中未写入 Brain 的对话全文；今日事件总结由主 AI 依据对话理解撰写，再经 `complete_journal` 等落库。

**调用方式**

- **推荐**：主 AI 日终调用 `daily_review`（返回结构化"一包结果"JSON）。
- `run_housekeeper` 为**兼容入口**，等价于 `daily_review(run_pipeline=True)`：在候选扫描之外，额外执行旧管线副作用——每日摘要写入回音壁 digest、事件链更新、冲突提案与身份收录提案提交回音壁；返回文本渲染（结构化结果请用 `daily_review`）。
- 后台自动调度**默认关闭**（`housekeeper.auto_schedule: false`）；如需恢复旧行为，在 `config.yaml` 设置 `housekeeper.auto_schedule: true`。

**单次调用返回内容**（`daily_review`，对照真实结构）

| 字段 | 内容 |
|---|---|
| `summary_guidance` | 今日总结引导：提醒主 AI 依据对话写出事实要点与可选情绪，附 `suggested_fields`（event_summary / mood_comment / emotion_tags）、`hold_template` 字段模板、当日 `journal_status` 与 `journal_note` |
| `candidates` | 记忆维护候选列表：每条含 `bucket_id`、名称/摘要、标出原因 `reason`、可选方案 `options`、默认建议 `default_suggestion`（仅参考）、详情 `detail` |
| `execution_instructions` | 执行说明：主 AI 逐条（或按策略）做出最终决策并调用对应工具执行，**不限于"是/否批准管家建议"** |
| `health` | 异常/健康信息：`errors` / `warnings`，正常时为空数组 |
| `scan_summary` | 扫描统计：`scanned_buckets`、`candidates_total`、按类别计数 `candidates_by_category` |

候选类别与可选方案（默认建议仅供参考，主 AI 可忽略）：

| category | 标出原因 | options | 默认建议 |
|---|---|---|---|
| `expired_task` | 任务型记忆，正文含明确日期且日期已过去 | `mark_resolved` / `archive_or_sink` / `delete` / `keep` | `mark_resolved` |
| `stale` | 30 天未访问 + 低重要度 + 低激活次数 | `keep` / `archive_or_sink` / `raise_importance` / `delete` | 权重 <0.3 时 `archive_or_sink`，否则 `keep` |
| `duplicate` | 内容相似度 ≥88（保守策略，不激进合并） | `merge_with:<older_id>` / `keep` | `merge_with:<older_id>` |
| `conflict` | 偏好 / 健康 / 状态 / 事实冲突 | `keep` / `mark_old_resolved` / `delete_new` | `keep` |

精简 JSON 示例（字段名与代码一致）：

```json
{
  "mode": "daily_review",
  "review_date": "2026-08-12",
  "summary_guidance": {
    "message": "请根据本日对话写出今日事件的事实要点……不要编造对话中未出现的日程或事件。",
    "suggested_fields": { "event_summary": "今日事实要点…", "mood_comment": "可选…", "emotion_tags": "可选…" },
    "hold_template": { "content": "…", "valence": "0.0~1.0", "arousal": "0.0~1.0", "tags": "…", "importance": "1~10", "source": "daily_review" },
    "journal_status": { "exists": false, "has_event_summary": false, "has_mood_comment": false },
    "journal_note": "今日日记尚无事件摘要：请……调用 complete_journal(…) 落库。"
  },
  "candidates": [
    {
      "bucket_id": "bc278144e947",
      "name": "2026-08-03",
      "category": "expired_task",
      "reason": "任务型记忆，正文含明确日期 2026-08-03，已过去 9 天",
      "options": ["mark_resolved", "archive_or_sink", "delete", "keep"],
      "default_suggestion": "mark_resolved",
      "detail": { "created": "…", "importance": 5, "content_preview": "2026-08-03 去医院复诊" }
    }
  ],
  "execution_instructions": "以上候选仅为管家扫描结果与参考建议，最终操作权在你（主 AI）。请逐条……使用对应工具执行，不必局限于管家的默认建议：删除 trace(delete=True)；沉底 trace(resolved=1)；提权 trace(importance=N)；合并 先读取两条内容→hold() 写入合并后完整记忆→trace(delete=True) 删冗余；保留 不做操作。",
  "health": { "errors": [], "warnings": [] },
  "scan_summary": { "scanned_buckets": 5, "candidates_total": 1, "candidates_by_category": { "expired_task": 1 } }
}
```

**日记**：管家**不再自动写日记草稿**。今日事件总结由主 AI 依据对话理解写出，经 `complete_journal(date, event_summary, mood_comment, emotion_tags)` 落库（幂等合并、日期/标签容错）；按日期/关键词用 `query_journal(date, keyword)` 查询。详见上文「每日日志系统」。

**回音壁**

- 每次 `daily_review` 向回音壁写入一份 **`daily_review` 扫描报告**（`digests/` 目录）：含扫描统计与完整候选列表（每条含原因与默认建议），供主 AI 后续对照；**仅为存档，不产生待审批动作**。
- 当 `run_pipeline=True`（即 `run_housekeeper`）时，额外产生**旧管线提案**：每日摘要 digest、事件链更新、冲突提案（conflict）、人物收录提案（identity_proposal）等——这些仍走回音壁 `approve_action` / `reject_action` 审批链路。
- `approve_action` / `reject_action` 仍适用场景：身份收录提案、事件链合并提案、旧管线清理/冲突提案等"是否执行"的快捷决策；而 `daily_review` 的候选更强调主 AI 的自由决策（逐条选择不同操作，非"是/否"）。两者可结合使用：先看 `daily_review` 候选自由决策，再对旧管线提案走审批。

**明确不做什么**

- **不编造未入库对话**：管家看不到对话全文，今日总结不自动生成，绝不虚构日程。
- 日终仅返回候选与建议，**不做任何自动删除 / 合并 / 沉底**。
- 开新窗口由管家接手日终流程：**未实现 / 后续规划**。

> **每周管家**（随自动调度关闭，可开启）：扫描相似 Event Chain 生成合并提案（不直接合并）；扫描过期且无关联的低权重记忆生成清理提案；所有提案提交回音壁，由主 AI 终审。

---

## 架构

```
任何 MCP 客户端 ←→ MCP Protocol（stdio / streamable-http / SSE）
                         │
                    server.py
                     (MCP Server)
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        bucket_manager   dehydrator     decay_engine
         (CRUD + 搜索)    (压缩 + 打标)   (遗忘曲线)
         ┌─ 文件锁             │
         │  (threading.Lock)   │
         ▼                     ▼
   Obsidian Vault       embedding_engine
   (Markdown files)     (向量语义检索)
                              │
                         embeddings.db
                         (SQLite, WAL mode,
                          3072-dim, 并发安全)
                              │
                     ┌────────┴────────┐
                     │                 │
              housekeeper        echo_chamber
               (定时任务)         (管家存储区)
                     │                 │
              ┌──────┴──────┐     event_chains
              │             │     (跨天事件链)
         每日管家       每周管家
              │             │
         生成摘要      去重融合
         冲突检测      清理提案
```

### MCP 工具

| 工具 | 作用 |
|---|---|
| `breath` | 浮现或检索记忆。无参数 → 推送权重池；有参数 → 三步检索管线 |
| `hold` | 存储单条记忆。自动情感打标 + 语义查重合并 + 异步生成 embedding + 异步生成 one_line_summary |
| `grow` | 日记归档。自动拆分长内容为多个记忆桶，逐条执行 hold 流程 |
| `trace` | 修改元数据（resolved / tags / importance / emotions 等），删除记忆桶 |
| `inject_context` | 静默预处理中间件：自动检索并注入上下文到用户 Prompt |
| `daily_review` | 日终整理（推荐，主 AI 主动调用）：返回结构化"一包结果"——今日总结引导 + 记忆维护候选列表（过期任务/长期低价值/重复/冲突，含原因与可选方案）+ 执行说明 + 健康信息 |
| `run_housekeeper` | 管家日终整理（兼容入口，文本渲染；等价于 daily_review 加旧管线副作用） |
| `run_weekly_housekeeper` | 手动触发每周管家任务 |
| `review_digest` | 审阅回音壁中的待办提案 |
| `approve_action` | 批准管家提案并自动执行操作（清理/合并/冲突解决） |
| `reject_action` | 驳回管家提案 |
| `get_event_chains` | 获取所有事件链，`include_related=True` 时附带关联链摘要（实体 + 关联链主题/节点数） |
| `get_event_chain_detail` | 获取单条事件链完整详情，含关联链的共享实体与摘要 |
| `approve_event_chain` | 批准事件链结案 |
| `lock_memory` | 将记忆桶标记为隐私，设置密码锁定 |
| `unlock_memory` | 解除记忆桶的隐私锁定 |
| `record_cycle` | 记录例假周期数据，自动预测下次日期 |
| `complete_journal` | 主AI为指定日期日记补充情绪点评和心情标签 |
| `query_journal` | 按日期或关键词查询每日日志 |

---

## 安装（本地 Python）

```bash
git clone https://github.com/P0luz/Ombre-Brain.git
cd Ombre-Brain

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp config.example.yaml config.yaml
export OMBRE_API_KEY="your-api-key"
OMBRE_TRANSPORT=streamable-http python server.py
```

---

## 配置

所有参数在 `config.yaml`（从 `config.example.yaml` 复制）。关键的几个：

| 参数 | 说明 | 默认 |
|---|---|---|
| `transport` | `stdio`（本地）/ `streamable-http`（远程）| `stdio` |
| `buckets_dir` | 记忆桶存储路径 | `./buckets/` |
| `dehydration.model` | 脱水用的 LLM 模型 | `deepseek-chat` |
| `embedding.enabled` | 启用向量语义检索 | `true` |
| `embedding.model` | Embedding 模型 | `gemini-embedding-001` |
| `decay.lambda` | 衰减速率，越大越快忘 | `0.05` |

敏感配置用环境变量：
- `OMBRE_API_KEY` — LLM API 密钥
- `OMBRE_TRANSPORT` — 覆盖传输方式
- `OMBRE_BUCKETS_DIR` — 覆盖存储路径
- `OMBRE_DASHBOARD_PASSWORD` — Dashboard 访问密码

---

## 测试

```bash
python -m pytest
```

测试套件覆盖规格书所有场景以及回归测试，所有测试运行在临时目录，绝不触碰真实记忆数据。

---

## License

MIT