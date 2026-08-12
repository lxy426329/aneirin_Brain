# Ombre Brain — 架构全景文档 / ARCHITECTURE

> 面向使用者和开发者：系统存储层、标记类型、工具全景与后台任务的总览。
> 与 [INTERNALS.md](./INTERNALS.md)（开发细节）互为补充；行为约束见 [BEHAVIOR_SPEC.md](./BEHAVIOR_SPEC.md)。
> 最后更新：2026-08-12

---

## 0. 系统总览

一个基于 **MCP 协议** 的长期情绪记忆系统。核心思想：

- 每条记忆 = 一个 **Markdown 文件**（YAML frontmatter 存元数据，正文存内容），天然兼容 Obsidian 浏览/编辑
- 用 **Russell 效价/唤醒度** 二维坐标打情感标签，同时保留离散情绪词
- 记忆会随 **遗忘曲线** 自然衰减、归档、结案
- 双通道检索：关键词模糊匹配 + 语义向量（embedding）余弦相似度，配 Rerank 精排
- **管家（Housekeeper）** 负责扫描已入库记忆、提出候选与提案，最终决策权在主 AI
- 不限于 Claude——任何支持 MCP 的 AI 客户端（Claude Desktop/Code、Cline、Cursor 等）均可接入

```
             MCP 客户端（Claude / Cline / Cursor ...）
                          │  streamable-http / sse / stdio
                          ▼
             ┌──────────────────────┐
             │      server.py       │  51 个 MCP 工具 + HTTP API
             └──────┬───────────┬───┘
                    │           │
          ┌─────────▼───┐   ┌───▼──────────┐
          │ bucket_mgr  │   │  housekeeper │  后台：decay / tag_normalizer / embedding
          └─────────┬───┘   └───┬──────────┘
                    │           │
          ┌─────────▼───────────▼──────────┐
          │  buckets/（记忆桶，Markdown）   │
          │  journals/（日记，JSON）        │
          │  embeddings.db（SQLite 向量库） │
          └────────────────────────────────┘
```

---

## 1. 存储层

以 `buckets_dir`（默认 `./buckets/`）为根，全部为普通文件，无外部云数据库。

```
<root>/
├── buckets/                          # 记忆主存储（默认目录）
│   ├── permanent/                    # 固化记忆（不衰减）
│   │   └── <领域>/name_id.md
│   ├── dynamic/                      # 动态记忆（默认桶，会衰减）
│   │   └── <领域>/name_id.md         # 领域子目录：日常/情感/编程/工作…
│   ├── archive/                      # 已遗忘/归档（衰减 score<0.3 自动移入）
│   ├── feel/沉淀物/                  # 模型自身感受（第一人称，不参与浮现）
│   ├── identity/                     # 身份档案（人物）
│   ├── ring/                         # 年轮/经验模式（原 pattern/，旧数据自动迁移）
│   ├── milestone/                    # 重要时刻/纪念日（高情绪浓度，永不衰减）
│   ├── voice/                        # 说话习惯/称呼/相处方式（按需检索，不衰减）
│   ├── anchor/                       # 行为/情绪锚点（规则与指导）
│   ├── boundary/                     # 底线共识（认知共识/逻辑底线/原则，永不衰减）
│   ├── faded/                        # 模糊印象标签（衰减记忆降维产物，轻量级）
│   ├── ephemeral/                    # 短期绝密暂存区（半衰期 24h，dream() 中自然蒸发）
│   ├── timeline/                     # 时间链（含 previous/next 因果指针）
│   ├── candlestick/                  # 烛台备忘录（重要备忘）
│   ├── event_chains/                 # 事件链（含 entities/related_chain_ids 图谱）
│   └── echo_chamber/                 # 回音壁（系统级中间态）
│       ├── digests/                  # 每日/每周摘要 + daily_review 扫描报告
│       ├── pending_actions/          # 待审批提案
│       └── .housekeeper_state.json   # 管家上次运行时间状态
├── journals/                         # 每日日志（与 buckets 同级，物理隔离）
│   └── YYYY-MM-DD.json
├── .trash/                           # 回收站（软删除，24h 后自动清理）
└── embeddings.db                     # SQLite 向量库（WAL + busy_timeout=5000）
```

### 各层原始设计用途

| 存储层 | 原始设计用途 |
|---|---|
| `permanent/` | 固化记忆。钉选（pinned）桶强制入此目录，importance 锁 10，**永不衰减/合并**，始终作为"核心准则"浮现 |
| `dynamic/` | 普通事件记忆（默认桶型）。受遗忘曲线衰减，按领域分子目录，是 breath() 三层检索的主体 |
| `archive/` | 已遗忘记忆。衰减得分低于阈值(0.3)自动移入；默认不参与检索 |
| `feel/沉淀物/` | 模型自身的第一人称感受。写入时自动标记源记忆 digested；不参与普通浮现；≥3 条相似 feel 可提示升级为钉选准则 |
| `identity/` | 身份档案。管家扫描近 7 天高频人物（≥3 次）提交 identity_proposal 提案，主 AI 审批后创建 |
| `ring/` | 年轮/经验层（原 pattern/，旧数据自动迁移）。可复用经验模式，带 apply_count/confidence，供三步检索 Step 2"年轮经验提取" |
| `milestone/` | 重要时刻层。纪念日、重要事件等高情绪浓度记忆，**永不衰减**，检索与 permanent 分开（不参与普通浮现，可按 type 单独检索） |
| `voice/` | 说话方式层。说话习惯、称呼、相处方式，**开局不无差别强制注入**，仅在主动检索时按需精准拉取；不走评分、不走衰减 |
| `anchor/` | 行为/情绪锚点。只存规则与指导（触发词+情绪基调+行为禁忌），事件细节仅留 bucket_id 指针 |
| `boundary/` | 底线共识层。双方确立的认知共识、逻辑底线与原则，**永不衰减**；当用户出现严重认知偏差（valence<0.25）或极度消极（arousal>0.8）时高优先级激活，确保主 AI 保持独立立场与理性引导 |
| `faded/` | 模糊印象层。久远普通 dynamic 记忆衰减后不物理删除，降维压缩为轻量 `faded_memory` 模糊标签（原名+摘要截取+decay_score），供主 AI 表达自然的沧桑感与模糊回忆 |
| `ephemeral/` | 短期绝密暂存区。纯发泄吐槽内容，**半衰期 24h**；未被再次引用（检索刷新 last_accessed）的内容在 nightly `dream()` 中自然蒸发（软删除移入 .trash/ 并清理向量） |
| `timeline/` | 时间链。每条含 previous_event_id/next_event_id 因果指针，支持 trace_chain 双向追溯 |
| `candlestick/` | 烛台备忘录。独立于普通记忆桶的重要备忘区 |
| `event_chains/` | 事件链。管家把零散事件归并为链（timeline+summary+source_bucket_ids），entities/related_chain_ids 构成实体图谱 |
| `echo_chamber/` | 回音壁。管家工作总结（每日/每周摘要）与待审批提案（清理/冲突/合并/身份收录）。**提案仅存独立临时缓存，严禁拼入 breath() 返回值**，主 AI 用 review_pending_actions / approve_action / reject_action 静默审阅后由管家执行落库 |
| `journals/` | 每日日志。与记忆桶系统完全隔离：不参与 breath()/inject_context()/list_all()，仅 query_journal 显式查询 |
| `.trash/` | 回收站。删除为软删除，文件加时间戳前缀移入，24h 后自动清理 |
| `embeddings.db` | 语义向量库。embedding_engine 写入向量，支持混合检索（BM25 + 向量 + Rerank） |

---

## 2. 标记类型（frontmatter 元数据）

### 2.1 桶类型 `type`

| 类型 | 目录 | 设计意图 |
|---|---|---|
| `dynamic` | dynamic/ | 默认事件桶，会衰减 |
| `permanent` | permanent/ | 固化不衰减（钉选自动升级为此） |
| `feel` | feel/沉淀物/ | 模型感受，不浮现 |
| `identity` | identity/ | 身份档案 |
| `pattern` | ring/ | 年轮经验（type 仍为 pattern，文件存 ring/） |
| `milestone` | milestone/ | 重要时刻（纪念日/重要事件，永不衰减） |
| `voice` | voice/ | 说话方式（按需检索，不衰减） |
| `anchor` | anchor/ | 行为锚点 |
| `boundary` | boundary/ | 底线共识（认知共识/逻辑底线/原则，永不衰减） |
| `faded_memory` | faded/ | 模糊印象（衰减记忆降维产物，轻量标签） |
| `ephemeral` | ephemeral/ | 短期吐槽暂存（半衰期 24h，dream() 蒸发） |
| `timeline` | timeline/ | 时间链 |
| `candlestick` | candlestick/ | 烛台备忘 |
| `archived` | archive/ | 已遗忘（衰减触发） |

### 2.2 核心元数据字段

| 字段 | 含义（原始设计） |
|---|---|
| `id` | 12 位短 UUID 桶 ID |
| `name` | 可读名（≤80 字，宁长勿空，保留数值与因果） |
| `tags` | 关键词（10~15 个） |
| `domain` | 主题域（1~2 个，8 大类 30+ 细分域） |
| `valence` | Russell 效价 0~1（0=负面，1=正面） |
| `arousal` | 唤醒度 0~1（0=平静，1=激动） |
| `dominant_emotion` / `emotions[]` | 主情绪 + 带强度的情绪列表 |
| `importance` | 重要度 1~10 |
| `pinned` | 钉选（→ permanent，importance 锁 10） |
| `protected` | 受保护（不参与合并/衰减） |
| `task_flag` | 任务类记忆（用户脆弱状态时自动屏蔽，防催任务） |
| `resolved` | 已解决/沉底（权重 ×0.05）；task_flag 桶需 force_resolved=1 |
| `force_resolved` | 强制解决（绕过任务屏蔽保护） |
| `digested` | 已消化（写过 feel 后标记） |
| `decay_stage` / `decay_factor` | 衰减阶段（0~3）/ 衰减因子 |
| `activation_count` / `hit_count` | 被想起次数 / 命中次数 |
| `last_accessed` / `created` / `updated` | 时间戳 |
| `one_line_summary` | 一句话摘要（异步 LLM 生成，检索只读不调 LLM） |
| `is_private` / `privacy_password` | 隐私锁定 + SHA256 哈希密码（前端需密码查看） |
| `merged` / `superseded_by` | 查重合并标记 / 被谁取代 |
| `status` / `superseded_at` | 版本控制状态（`superseded`=已失效） / 取代时间戳；检索自动屏蔽失效旧桶 |
| `efficacy_score` / `efficacy_reports` | 策略有效性评分（EMA 平滑） / 评估报告列表（≤10 条），由主 AI 反馈更新 |
| `previous_event_id` / `next_event_id` | 因果链双向指针 |
| `source` | 来源标记（yeeban / daily_review…） |
| `weight` | 综合权重（供检索排序） |

### 2.3 情绪标签体系

基础情绪词表 20 类：开心/难过/愤怒/焦虑/平静/兴奋/委屈/感动/失落/期待/恐惧/惊喜/愧疚/骄傲/嫉妒/羡慕/悲伤/快乐/抑郁/孤独。写入时自动归并同义词；另有 `dominant_emotion` 离散映射（joy/sadness/anger/calm…）。

---

## 3. MCP 工具全景（51 个）

### A. 记忆读写核心

| 工具 | 设计用途 |
|---|---|
| `hold` | 存储单条记忆，自动情感打标 + 相似查重合并。pinned/protected/feel/task_flag/source/title/显式 valence-arousal 可控；`boundary=True` 存入 boundary/ 底线共识层（永不衰减）；`ephemeral=True` 存入 ephemeral/ 短期绝密暂存区（半衰期 24h，dream() 自然蒸发） |
| `grow` | 日记归档：一段长内容自动拆分为多桶（<30 字走 hold 快速路径） |
| `breath` | 检索/呼吸：无 query=被动浮现（最近未解决+权重 TOP-3）；带 query=三步检索管线（强锚点→年轮经验→五维加权混合检索+Rerank 硬截断 Top3）。**Salience Gate 相关度门槛**：向量/主题匹配度过低且情绪唤醒度不高时跳过深层检索；**主题相关度优先**：Topic/Vector 匹配度极低时高 Priority 也无法硬推；**boundary 激活**：检测到严重认知偏差或极度消极时高优先级注入底线共识；**时空感知**：返回开头带 `[Context]` 轻量时段/生活状态标记 |
| `trace` | 修改记忆元数据/内容：resolved 沉底、pinned 钉选、digested 隐藏、task_flag、content 替换、delete=True 删除。主 AI 执行日终决策的主要写工具 |
| `dream` | 做梦自省：读取最近新增记忆供 AI 自省；读后可 trace(resolved=1) 放下或 hold(feel=True) 写感受。**已与开局流程解耦**，仅在晚间总结或后台闲置时调用；同时扫描未解决高焦虑/极低情绪问题，生成临时 `emotional_suspension_flag` 供下次开局主动关怀；并对过期未引用的 ephemeral/ 暂存内容执行蒸发 |
| `pulse` | 系统状态 + 记忆桶列表（身份/年轮/模式概览） |

### B. 检索 / 分析 / 上下文

| 工具 | 设计用途 |
|---|---|
| `inject_context` | 静默预处理中间件：用户发消息前调用，自动检索相关记忆以 `<context>` 结构注入 Prompt 头部（含硬指令模板）；无上下文返回空串 |
| `query_memory` | 通用查询（search/float/status/directory/recent 五模式） |
| `analytics` | 统计分析：情绪分布、类型统计、活跃度趋势 |
| `memory_directory` | 记忆库目录摘要（brief/medium/full） |
| `summarize_recent_events` | 最近 N 天事件概括（可 LLM 润色成状态报告） |
| `get_experiences` | 年轮（经验）记录查询 |

### C. 特殊类型记忆查询

| 工具 | 设计用途 |
|---|---|
| `get_anchors` | 查询行为/情绪锚点（active_only 只看生效中） |
| `get_timelines` | 查询时间链列表（动态相对时间描述） |
| `get_memos` | 查询烛台备忘录 |
| `get_roster` | 查询名册（人物身份档案） |
| `recall_faded` | 模糊回忆：读取衰减后降维压缩的 `faded_memory` 轻量标签（默认 5 条），供主 AI 表达自然的沧桑感与模糊印象 |
| `get_event_chains` | 查询事件链草案（include_related=True 附带实体关联链摘要） |
| `get_event_chain_detail` | 单链详情 + 共享实体关联链 |

### D. 关联 / 因果 / AI 分析

| 工具 | 设计用途 |
|---|---|
| `trace_chain` | 追溯因果链：沿 previous/next 双向指针调出前因后果（默认 3 层防无限遍历） |
| `link_events` | 手动建立两个事件的因果链（管家每日自动生成的补充手段） |
| `manage_relation` | 通用关联管理：link 关联 / parent 父子 / chain 事件链 / importance 重要度五维 |
| `manage_identity_relation` | 身份关系管理：建立/查询/更新人物关系权重 |
| `ai_analyze` | LLM 分析：link/find/chain/summarize/classify |
| `ai_manage` | AI 管家代理：自然语言请求 → 自动理解意图并调用合适工具（多轮工具调用） |
| `mcp_submit_efficacy_feedback` | **策略有效性评估（mcp_tools.py）**：主 AI 在对话中或会话结束时主动提交评估报告（strategy_type / effect_score 0~1 / improvement_note），管家自动更新对应 ring/ 认知桶的 `efficacy_score`（EMA 平滑）、`confidence` 与评估报告列表，不再离线盲猜 |

### E. 管家 / 日终 / 回音壁

| 工具 | 设计用途 |
|---|---|
| `daily_review` | 日终整理（推荐入口）：返回结构化"一包结果"（今日总结引导 + 维护候选[过期任务/低价值/重复/冲突] + 执行说明 + 健康信息）。管家不删改，最终操作主 AI 用 trace/hold 执行 |
| `run_housekeeper` | 日终兼容入口 = daily_review(run_pipeline=True)，额外跑旧管线（每日摘要/事件链/冲突/身份提案） |
| `run_weekly_housekeeper` | 每周管家：事件链合并 + 清理提案生成 |
| `check_echo_chamber` | 查看回音壁内容（摘要 + 待审批提案） |
| `review_digest` | 审阅回音壁待办，行使主 AI 最高裁决权 |
| `approve_action` / `reject_action` | 批准/驳回提案（cleanup 删向量、conflict 标记 resolved+status=superseded+superseded_by、chain_merge 合并实体、identity 建档案；批准即执行） |
| `review_pending_actions` | **静默决策通道**：主 AI 以 JSON 数组批量审阅待审批提案（approve/reject），管家执行落库；不打断用户 |
| `check_suspension_flag` | **主动关怀标记（Emotional Suspension）**：一次性读取 `emotional_suspension_flag`（dream() 生成），用于下次会话开局自然主动问候；读取即删除，无标记返回空 |
| `get_cleanup_proposals` / `approve_cleanup_proposal` / `reject_cleanup_proposal` | 清理提案三件套（审批后由衰减周期执行） |
| `approve_event_chain` | 批准事件链并标记结案 |

### F. 管理 / 维护 / 导入导出

| 工具 | 设计用途 |
|---|---|
| `manage_record` | 通用记录管理（create/update/get/list/delete/apply），覆盖 identity/roster/pattern/candlestick/experience/annual_ring |
| `smart_organize` | 智能整理：识别过期记忆并批量降权 |
| `memory_export` | 导出记忆（按类型过滤） |
| `memory_batch_delete` | 批量删除记忆桶 |
| `tag_normalize` | 标签归一化任务（run/status），后台每周或每 50 条自动跑 |
| `export_brain` / `import_brain` | 全库打包导出/恢复（buckets + embeddings.db） |

### G. 隐私 / 周期 / 日记

| 工具 | 设计用途 |
|---|---|
| `lock_memory` / `unlock_memory` | 隐私锁定/解锁（密码 SHA256 哈希存储，仅 MCP 调用者操作） |
| `record_cycle` | 例假周期记录（症状/流量/疼痛度），≥2 次自动预测下次日期，breath 自动提醒 |
| `complete_journal` | 主 AI 为指定日期日记补充情绪点评与标签（幂等合并，与记忆桶隔离） |
| `query_journal` | 日记查询：按日期精确或关键词搜索 |

---

## 4. 后台任务与管线

| 后台任务 | 触发 | 作用 |
|---|---|---|
| `decay_engine` | 懒启动，默认 24h 间隔 | 艾宾浩斯遗忘曲线衰减、自动归档、自动结案；**Fading Memory**：久远低分 dynamic 不物理删除，降维压缩为 `faded/` 模糊印象标签；进程内文件锁互斥 |
| `tag_normalizer` | 每周或每 50 条记录 | 非标准标签映射到泛化标签树 |
| `embedding_engine` | 写入时异步 | 向量生成与检索；API 失败退避重试 + 熔断（连续 3 次失败熔断 60s） |
| `housekeeper` | 默认关闭自动调度（`auto_schedule: false`），由主 AI 主动调用 | 日终扫描、每日/每周摘要、事件链更新、冲突/身份提案 |
| `wikilink 注入` | 写入时 | Obsidian 双链自动注入（标签/领域/自动关键词） |

---

## 5. 外部接口（HTTP，非 MCP）

| 接口 | 设计用途 |
|---|---|
| `POST /api/hold` | 外部系统（夜伴）稳定写入记忆；认证走 session cookie 或 `X-API-Key`（`OMBRE_EXTERNAL_API_KEY`） |
| `GET/POST /api/breath` | 轻量检索（每条仅一句话摘要+元数据，默认 3~5 条省 token） |
| `/api/run-housekeeper` | 日终触发（结构化返回） |
| `/dashboard` | 前端记忆库仪表板 |
| `/echo-chamber` | 回音壁前端（每日/每周摘要、待审批提案、冲突检测） |

---

## 6. 配置速查

见 [config.example.yaml](./config.example.yaml)，关键项：

- `transport`：stdio / streamable-http / sse
- `dehydration`：脱水压缩 LLM（DeepSeek/Ollama/Gemini 等 OpenAI 兼容 API）
- `embedding`：向量模型，独立可配
- `scoring_weights`：五维检索权重（emotion 3.0 / priority 4.0 / vector 3.0 / topic 2.0 / time 1.5）
- `hybrid_search`：BM25+Vector+Rerank 混合检索
- `decay`：衰减速率/阈值
- `housekeeper`：`auto_schedule`（默认 false，日终由主 AI 主动调用）、`auto_journal_draft`（默认 false，日记草稿不再自动生成）
- `echo_chamber`：回音壁启用/清理策略

---

## 7. 设计约束速记

- 管家不打断对话、不擅自最终删改；日终由主 AI 主动调用
- 回音壁提案独立缓存，**严禁拼入 breath() 返回值**；主 AI 走 review_pending_actions 静默 JSON 审阅
- 开局 breath() default max_tokens ≤ 2000，仅精准拉取与情境最相关的少量 anchor/ 与 permanent/，不强制注入 voice/ 全量
- 记忆返回末尾附 `[Memory Layer]` 隔离宣告：系统数据严禁直接复述，始终保持自然口语风格进行交互
- 高情绪（milestone/anchor）与习惯（voice/）脱水**关闭 LLM 摘要**，Raw Text Slice 原文截取入库；普通 dynamic 脱水硬性去 AI 腔（第一人称事实记录）
- Fading Memory：久远低分 dynamic 降维为 `faded/` 模糊印象标签，不彻底物理删除；boundary/ 永不衰减
- 记忆版本控制：事实更替/习惯变更时旧桶标注 `status=superseded` + `superseded_by` 指向新桶，检索自动屏蔽失效旧桶，衰减低分自然归档
- 策略反馈：主 AI 用 `mcp_submit_efficacy_feedback` 提交实测效果，管家 EMA 更新 ring/ 桶权重，禁止离线盲猜
- ephemeral/ 短期绝密暂存区：半衰期 24h，未被再次引用（last_accessed 未刷新）的内容在 nightly dream() 中蒸发
- 主动关怀：dream() 扫描高焦虑/极低情绪生成 `emotional_suspension_flag`，下次开局 check_suspension_flag 一次性读取即删
- 日记与记忆桶物理隔离，不污染 breath() 向量空间
- 隐私密码只存 SHA256 哈希，前端不能明文显示
- 删除走 `.trash/` 软删除 + 24h 自动清理
- 并发安全：SQLite WAL + 文件线程锁 + asyncio 启动锁
- 所有 API 请求携带 credentials（前端 `credentials: 'include'`）
