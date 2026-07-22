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

**Cline / 其他 MCP 客户端**：按各客户端文档配置 MCP 服务器，类型选择 `streamable-http`，URL 指向 `http://localhost:8000/mcp`（远程）或 `stdio`（本地）。重启客户端后，工具列表里应出现 `breath`、`hold`、`grow` 等。

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
    │   Final_Score = (3.0×① + 2.0×② + 4.0×③ + 5.0×④ + 1.5×⑤) / 15.5 │
    │                                                                    │
    │ 分层返回规则：                                                     │
    │   Final_Score ≥ 0.7  → 返回完整正文内容（TOP-N）                   │
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
- **Markdown 文件锁**：`bucket_manager.update()` 使用 `threading.Lock` 包裹完整读-改-写周期（`frontmatter.load` → 修改元数据 → `frontmatter.dumps` 写回），确保两个进程同时写入同一文件时互斥

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

- **每日摘要**：管家自动生成的当日记忆总结
- **每周摘要**：管家自动生成的当周事件链合并报告
- **待审批提案**：记忆冲突、清理提案等需要主 AI 裁决的事项

访问地址：**http://localhost:8000/echo-chamber**

### 静默预处理中间件

在用户发送消息、调用主 AI API 之前自动执行：

1. 拦截用户输入，后台自动执行 Query 改写与混合检索（Hybrid Search + Rerank）
2. 自动匹配并拉取最新的【相关事件链 (Event Chain)】与【感官/状态标签 (Feel/Status)】
3. 将检索到的背景记忆以 `<context>` 结构静默拼接到用户 Prompt 头部
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

仅当同一主题跨越多个时间段（如病程跟进、备考、项目开发）被持续提及或跟进时，才会创建或追加至事件链。

### 日/周两级管家任务

**每日管家（自动运行）**：
- 每天凌晨运行
- 对当日对话做轻量总结
- 提炼关键事实并追加到对应 Event Chain
- 检测记忆冲突并提交到回音壁
- **不删除任何数据**

**每周管家（自动运行）**：
- 每周日凌晨运行
- 对一周内的 Event Chain 进行去重与融合
- 扫描过期且无关联的低权重记忆，打上 `pending_delete` 标记
- 生成清理草案，提交至回音壁供主 AI 终审

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
| `run_housekeeper` | 手动触发每日管家任务 |
| `run_weekly_housekeeper` | 手动触发每周管家任务 |
| `review_digest` | 审阅回音壁中的待办提案 |
| `approve_action` | 批准管家提案（执行清理/合并） |
| `reject_action` | 驳回管家提案 |
| `get_event_chains` | 获取所有事件链 |
| `approve_event_chain` | 批准事件链结案 |

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