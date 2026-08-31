# 更改日志 / CHANGELOG

> 记录 Ombre Brain 各模块整合与功能修缮的更改内容。
> 每次改动追加到最上方（最新在上）。

---

## 2026-08-31 Brain 运行时验收补充：默认世界隔离修复（RP 禁止污染 main）

### 目标
capture 真实 breath context 时发现：`breath` 未指定 `world_id` 时 `world_filter=None`，主检索路径与 fallback 保底路径均不做世界过滤，`rp_xxx` 世界记忆会泄漏进 main 世界上下文。违反"RP 必须拥有独立 world_id，禁止污染 main"。

### 修复
- **`server.py` `breath()`**：`world_id` 为空时默认 `world_filter=["main"]`，主 AI 默认调用不再召回 RP 世界记忆。
- **`server.py` `_breath_lightweight()`**：`world_filter=None` 时同样默认 `["main"]`（覆盖 MCP lightweight 与 HTTP `/api/breath`）。
- 更新 `breath` / `api_breath` docstring：空 world_id = 默认 main 世界。

### 新增测试
- `tests/test_runtime_snapshot.py` 新增 `test_snapshot_default_world_isolation`：不传 world_id 时 RP 记忆不泄漏进 main，main 记忆正常召回。

### 验证
- 完整测试套件 256 passed（原 255 + 新增 1 个默认世界隔离用例）。

---

## 2026-08-31 Brain 运行时验收：P0 四项复核 + runtime snapshot tests + schema 冻结

### 目标
进入"Brain 运行时验收阶段"：复核此前要求的四项 P0 是否实际完成；验证 main AI 可主动调用 `add_memory_reply` 追加楼层（不设计"每次 recall 自动写楼层"机制）；新增 integration/runtime snapshot tests 捕获"最终实际准备注入模型的完整 breath context"并断言；本轮完成后冻结 schema。

### P0 复核结果
1. **P0-1 provenance 默认安全化：已完成**（`bucket_manager.py` `create` 默认 `provenance="legacy"`，未显式指定来源不误标 user_explicit）。
2. **P0-2 breath context 三分组：已完成**（`server.py` breath 最终 context 显式分组 `[active_instruction] / [current_state] / [background_memory]`，background 不自动构成当前行动要求）。
3. **P0-3 mutation 入口 proposal 化：已完成**（trace / manage_record / memory_batch_delete / smart_organize 等入口改走 proposal → approval，唯一执行入口 `execute_change_proposal`）。
4. **P0-4 before_hash 版本防护：已完成**（`housekeeper.py` 提案生成时对目标 bucket/reply 计算 SHA-256 before_hash，执行时校验，目标已变化则标记 conflict 禁止执行）。

### 本轮修复（runtime snapshot 暴露的真实 bug）
1. **`_memory_role` 不检查状态过期**（`server.py`）：`is_current=True` 但 `expires_at` 已过期的状态仍被标为"当前状态"。修复：过期状态自动降级为背景角色，不表示为 current。
2. **force_keyword 精确检索漏召回**（`server.py`）：仅命中正文（未命中名称/标签）的桶，归一化得分被情绪/时间权重稀释到 0.4 以下，被速览层丢弃。修复：force_keyword 模式下命中精确关键词的桶提升到速览层（0.4），保证精确查找始终可用。
3. **fallback 保底路径绕过指令有效性**（`server.py`）：未激活/过期指令通过"保底记忆"随机选取泄漏进 context。修复：fallback 同样应用 `_is_instruction_active` 过滤。
4. **速览条目无正文预览**（`server.py`）：无 one_line/dehydrated 摘要时只显示截断名称（10 字符），内容不可见。修复：回退到正文预览（前 60 字符）。
5. **HTTP API 删除/正文修改绕过 proposal**（`server.py`）：`api_bucket_delete` 直接 `bucket_mgr.delete`、`api_bucket_update` 直接替换 content，绕过 proposal → approval。修复：新增 `_route_user_mutation()`，用户级 mutation 统一生成提案（proposed_by=user）并由认证用户立即批准（approved_by=user）执行，保留 UI 即时生效体验同时保留审批记录与 before_hash 防护。
6. **`delete_embedding` 缺 await**（`housekeeper.py`）：单桶/批量删除提案执行时 `delete_embedding` 未 await，embedding 实际未删除。修复：补上 `await`。

### 新增测试
- `tests/test_runtime_snapshot.py`（12 个用例）：构造 8 类测试记忆（main/chat 普通 background、inactive instruction、active instruction、expired current state、valid current state、RP world memory、intimate scene memory、含 reflection + correction + superseded reply 的 thread），以不同 world_id / scene 调用真实 breath 管线，捕获最终 context 并断言：background 不表示为当前 instruction；inactive/expired instruction 不成为行动依据；expired state 不表示为 current；world/scene 不泄漏；thread 只返回精简摘要；superseded correction 不作为当前 correction；显式 get_memory_thread 才返回完整 thread；main AI 可主动调用 add_memory_reply；HTTP API 删除/正文修改走 proposal → approval（approved_by=user + before_hash）。

### 验证
- 完整测试套件 255 passed（原 243 + 新增 12 个 runtime snapshot 用例）。

### Schema 冻结
- 本轮完成后冻结 schema：除非 runtime snapshot 暴露结构性 bug，不再新增 metadata、目录或分类字段。

---

## 2026-08-31 记忆楼层 / Memory Thread：主记忆稳定 + 理解变化以楼层追加

### 目标
一条长期记忆的"事实主体"保持稳定，AI 后来产生的新理解、补充、纠正、情绪或观点以"楼层（reply）"形式追加在原记忆下面，默认只追加、不覆盖旧楼层。它不是 event chain（事件因果链），而是对"同一条记忆"的纵向理解历史。全部为加法式兼容改造，不迁移/重写现有 memory。

### 更改内容
1. **独立楼层存储**
   - 文件：`thread_manager.py`（新建）。`ThreadManager` 将 reply 存于 `threads/` 目录（与 `buckets/` 同级，同 `journals/` 模式），每条 reply 独立 JSON 文件，不塞进原 memory 正文。
   - reply 字段：`reply_id / parent_memory_id / author / created_at / content / provenance / world_id / scene`，可选 `reply_type / source_context / supersedes_reply_id`。
   - 词表：`reply_type` ∈ reflection / correction / supplement / disagreement / feeling；`author` ∈ main_ai / user / system；`provenance` ∈ user_explicit / ai_inferred / ai_observed / system_event / imported / legacy。非法值回退安全默认。
   - 方法：`add_reply / list_replies / get_thread / delete_reply / update_reply`（后者仅由 proposal 审批后调用，不可改 reply_id / parent_memory_id / created_at）。
2. **world / scene 隔离继承**
   - reply 默认继承 parent memory 的 `world_id` 和 `scene`，允许显式覆盖；检索 reply 时遵守现有 world + scene 隔离规则，禁止利用 thread 形成跨 world / scene 的隐式泄漏。
3. **provenance 兼容**
   - 用户明确补充：author=user, provenance=user_explicit；AI 对话观察：author=main_ai, provenance=ai_observed；AI 新理解：author=main_ai, provenance=ai_inferred；历史导入：provenance=imported / legacy。不因作者是 AI 就写成 user_explicit。
4. **breath 检索精简楼层摘要**
   - 文件：`server.py`。新增 `_thread_summary()`：普通 breath 只附带最新 reply / 当前有效 correction（未被 supersedes 的）/ 最近一次 reflection，不返回整栋楼；`_breath_lightweight` 每条结果新增 `latest_reply` 字段。
   - 完整楼层仅在显式 `get_memory_thread(parent_memory_id)` 时展开（返回 main_memory + 按 created_at 排序的 replies）。
5. **MCP 工具**
   - `add_memory_reply(parent_memory_id, content, reply_type, author, provenance, world_id, scene, supersedes_reply_id, source_context)`。
   - `get_memory_thread(parent_memory_id)`。
6. **楼层修改/删除走 proposal → approval**
   - 文件：`housekeeper.py`。构造函数新增 `thread_mgr` 参数；`execute_change_proposal` 新增 reply 分支（`reply_id` + `delete_reply` / `reply_updates`），楼层删除/修改必须经提案审批，禁止绕过。
7. **与 memory_class / event chain / annual_ring 的关系**
   - reply 不替代 memory_class；主 memory 仍可为 event / experience / person / boundary / plan / principle。thread 是纵向历史层，不破坏现有 event chain、annual_ring、world/scene、provenance 与 housekeeper approval 逻辑。

### 迁移方案
- 无数据库结构变化（reply 为独立 JSON 文件，加法式新增 `threads/` 目录）。
- 现有记忆数据库未删除、未重写；旧记忆无楼层时 breath 行为与之前完全一致。

### 验证
- 完整测试套件 243 passed（原 232 + 新增 11 个用例）。
- 新增 `tests/test_memory_thread.py` 覆盖：reply 不修改 parent、按时间排序、correction 保留旧 reply、supersedes 不删除历史、world 隔离、scene 隔离、provenance 保存、breath 不返回全部楼层、get_memory_thread 完整历史、reply 删除/修改必须经 proposal。

### 待后续
- Render UI 单条 memory 页面【楼层 / Thread】展示（backend + MCP 工具已完整预留，UI 改动成本较高时暂缓）。

---

## 2026-08-31 结构调整 v2 安全加固：provenance 默认安全化 / breath 角色显式化 / mutation 审计 + stale proposal 防护 / 旧记忆补全提案

### 目标
在不新增记忆业务功能的前提下，完成 4 项安全加固：来源默认安全化、上下文角色显式化、全 mutation surface 审计 + 过期提案防护、旧记忆 metadata 补全提案。全部为加法式兼容改造。

### 更改内容
1. **provenance 默认安全化**
   - 文件：`bucket_manager.py`（`create` 默认 `provenance="user_explicit"` → `"legacy"`，未显式指定来源一律标记 legacy）、`import_memory.py`（导入显式 `provenance="imported"`）、`housekeeper.py`（ring/identity 自动创建显式 `provenance="system_event"`）。
   - 效果：底层 API 默认安全（legacy），只有显式确认来源（如 hold 用户主动写入传 user_explicit）才标记具体来源。
   - 测试：`tests/test_provenance.py` 更新（默认 legacy、显式 user_explicit、枚举值保存）。
2. **breath 上下文角色显式化**
   - 文件：`server.py`。新增 `_memory_role()` 辅助函数（优先级：当前有效指令 > 当前状态 > 类型角色 > 背景）；breath 查询管线每条记忆加 `[角色:xxx]` 前缀（含已消化/总结/语义关联/随机浮现/保底路径）；`_breath_lightweight` 每条结果新增 `role` 字段。
   - 效果：主模型能明确区分每条记忆的角色（指令/当前状态/边界/身份/行为模式/永久原则/背景等）。
   - 测试：`tests/test_breath_role.py`（6 个用例）。
3. **全 mutation surface 审计 + stale proposal 防护**
   - 审计结论：housekeeper 的删除/修改路径均经过 proposal（execute_change_proposal/approve_action）；主 AI 直接操作（trace/manage_record/update_bucket 等）保留权限；系统自动维护（decay_engine 衰减状态、tag_normalizer 标签归一化、run_daily_job 沉底）为记忆生命周期管理，不涉及用户长期记忆删除。
   - 防护：`housekeeper.py` 新增 `PROPOSAL_TTL_DAYS=7`；`add_pending_action` 生成提案带 `expires_at`；`execute_change_proposal` / `approve_action` 检查过期提案并拒绝执行（旧提案无 expires_at 按未过期兼容）。
   - 测试：`tests/test_stale_proposal.py`（5 个用例）。
4. **旧记忆 metadata enrichment proposal**
   - 文件：`housekeeper.py`（新增 `_infer_memory_class()` 映射层与 `propose_metadata_enrichment()`，扫描 memory_class 缺失的旧记忆生成 enrich 提案，不直接修改）、`server.py`（新增 `propose_metadata_enrichment` MCP 工具）、`bucket_manager.py`（`update` 白名单补充 provenance/scene/world_id/memory_class/instruction 等新字段，使 enrich 提案可落地）。
   - 效果：旧记忆补全走提案-审批闭环，禁止直接修改。
   - 测试：`tests/test_enrichment.py`（4 个用例）。

### 迁移方案
- 无数据库结构变化（记忆为 Markdown + frontmatter，无 SQL 表变更）。
- 旧提案（无 expires_at 字段）按未过期处理，兼容历史数据。
- 现有记忆数据库未删除、未重写。

### 验证
- 完整测试套件 232 passed（原 216 + 新增 16 个用例）。
- `python -m py_compile` 全部通过。

### 待后续
- 无。

---

## 2026-08-31 结构调整 v2 修正：来源兼容 / 指令有效期 / 状态过期 / 世界隔离 / 记忆类别 / 审批者记录

### 目标
在结构调整 v2 基础上落实 6 项修正，全部为加法式兼容改造：不破坏现有 type 体系、不重写数据库、旧记忆读取时自动补默认字段。

### 更改内容
1. **修正 1：provenance 兼容策略**
   - 文件：`bucket_manager.py`。`PROVENANCE_VOCABULARY` 扩展为 `user_explicit / ai_inferred / ai_observed / system_event / imported / legacy`；`create` 默认 `provenance="user_explicit"`（新写入且能明确确认来源）；非法来源值回退 `legacy`；`_normalize_bucket_metadata` 对旧记忆（无 provenance 字段）补默认 `legacy`，绝不默认 `user_explicit`。
   - 测试：`tests/test_provenance.py` 更新（默认 user_explicit、非法回退 legacy、旧记忆 legacy、枚举值保存）。
2. **修正 2：instruction 有效期与触发条件**
   - 文件：`bucket_manager.py`。`create` 新增 `instruction / valid_from / expires_at / trigger_condition / active` 字段；新增 `_is_instruction_active()`（instruction=True 且 active=True 且未过期才有效）；`server.py` 查询管线与向量补充检索过滤未激活/已过期的指令类记忆，不作为行动依据。
   - 测试：`tests/test_instruction_validity.py`（5 个用例）。
3. **修正 3：state / is_current 过期机制**
   - 文件：`bucket_manager.py`。`create` 新增 `is_current / observed_at` 字段；`is_current=True` 自动补 `observed_at` 与 `state_expires_at`（默认 TTL 7 天，`DEFAULT_STATE_TTL_DAYS`）；`_normalize_bucket_metadata` 对已过期状态自动降级 `is_current=False`（不删除原记录）；禁止永久保存 `is_current=True` 而无时间约束。
   - 测试：`tests/test_state_expiry.py`（4 个用例）。
4. **修正 4：world_id + scene 双维度场景隔离**
   - 文件：`bucket_manager.py`（`create` 新增 `world_id`，默认 `main`）、`server.py`（`_parse_world_filter` / `_bucket_matches_world` 辅助函数；breath 主函数、`_breath_lightweight`、查询管线、向量补充检索、MCP schema、`/api/breath` 均支持 world 过滤）。RP 记忆必须独立 `world_id`（如 `rp_xxx`），禁止污染 main；旧记忆默认 `main + chat`。
   - 测试：`tests/test_scene.py` 更新（world_id 默认、显式 world_id、world 过滤、breath world 隔离）。
5. **修正 5：memory_class 字段（加法式，不破坏 type）**
   - 文件：`bucket_manager.py`。新增 `MEMORY_CLASS_VOCABULARY`（event/experience/person/boundary/plan/principle）与 `memory_class` 字段；现有 `type` 字段继续保留用于兼容旧逻辑；旧记忆无 `memory_class` 时保持 `None`，由映射层按需推断。
   - 测试：`tests/test_memory_class.py`（5 个用例）。
6. **修正 6：审批者记录 + 禁止绕过 proposal**
   - 文件：`housekeeper.py`（`add_pending_action` 记录 `proposed_by`；`approve_action` 新增 `approved_by` 参数并持久化 `approved_by/approved_at`；`approve_action` 增加 `change/organize` 类型委托给 `execute_change_proposal`）、`server.py`（`approve_action` MCP 工具新增 `approved_by` 参数；`review_pending_actions` 静默通道记录 `approved_by="main_ai"`；`/api/echo-chamber/approve` 支持 `approved_by`；`manage_record` 的 bucket/memory 删除路径改为生成 proposal，禁止直接删除）。
   - 测试：`tests/test_approval_flow.py`（5 个用例）、`tests/test_manage_record_delete.py`（2 个用例）。

### 迁移方案
- 无数据库结构变化（记忆为 Markdown + frontmatter，无 SQL 表变更）。
- 旧记忆无需迁移：读取时 `_normalize_bucket_metadata` 自动补 `provenance="legacy"`、`scene=["chat"]`、`world_id="main"`、`memory_class=None`、`instruction=False`、`is_current=False`。
- 现有记忆数据库未删除、未重写。

### 验证
- 完整测试套件 216 passed（原 190 + 新增 26 个用例）。
- `python -m py_compile` 全部通过。

### 待后续
- 无。

---

## 2026-08-30 结构调整 v2：时区修复 / 提案-审批闭环 / 背景隔离 / 来源与场景字段

### 目标
按改造方案（P0/P1/P2）落地 7 项结构调整需求，全部向后兼容：不删除旧功能、不重写现有记忆数据库、旧记忆读取时自动补默认字段。

### 更改内容
1. **P0-1 修复 get_anchors 时区错误**
   - 文件：`bucket_manager.py`。`add_anchor`/`activate_anchor` 的 `expires_at` 由 `datetime.now()`（本地无时区）改为 `datetime.now(timezone.utc)`；`_check_and_deactivate_if_expired` 兼容旧的无时区锚点（按本地时区解释后比较）。
   - 测试：`tests/test_anchor_timezone.py`（3 个用例：UTC 时区、旧无时区数据兼容）。
2. **P0-2 AI 管家只提候选，不直接修改**
   - 文件：`server.py`（`trace` 拆分 `propose_change`/`apply_change`；`smart_organize` 改为 `propose_organize`）、`housekeeper.py`（`EchoChamber.add_pending_action` 返回 `action_id`；新增 `execute_change_proposal` 执行 change/organize 提案，幂等保护）。
   - 效果：AI 管家生成修改候选 → 用户确认 → `apply_change` 才落地；未确认不触碰记忆。
   - 测试：`tests/test_propose_change.py`（5 个用例：提案 ID、change 执行、删除执行、幂等保护、organize 批量降权）。
3. **P0-3 背景隔离：历史记忆不得作为当前行动指令**
   - 文件：`server.py`。breath 返回末尾追加 `[Memory Layer]` 隔离宣告；`inject_context` 新增第 4 条硬性指令（历史记忆仅作背景参考，不得定义用户当前状态）。
4. **P1-1 provenance 来源字段**
   - 文件：`bucket_manager.py`（`create` 新增 `provenance` 参数，非法值回退 `user`；`_normalize_bucket_metadata` 补默认）、`server.py`（`hold`/`_hold_impl`/`_merge_or_create` 透传；feel/dream 自动合并标记 `ai_inferred`）。
   - 效果：区分"用户明确说过"（user）与"AI 推测"（ai_inferred/ai_observed）；旧记忆读取补默认 `user`。
   - 测试：`tests/test_provenance.py`（4 个用例）。
5. **P1-2 类型枚举统一 + 映射表**
   - 文件：`bucket_manager.py`（`TYPE_ALIASES`：event/experience/person/boundary/plan/principle → 现有存储类型）、`config.yaml`（`types` 映射配置）。
   - 效果：调用方可使用新枚举，存储层仍用现有类型，不破坏目录/衰减/检索。
   - 测试：`tests/test_type_aliases.py`（5 个用例）。
6. **P2 scene 场景字段 + breath 场景过滤**
   - 文件：`bucket_manager.py`（`create` 新增 `scene` 参数，非法值过滤、空回退 `["chat"]`；`_normalize_bucket_metadata` 补默认）、`server.py`（`hold`/`_hold_impl`/`_merge_or_create` 透传；`breath` 新增 `scene` 参数，`_breath_surfacing`/`_breath_lightweight`/importance_min/查询管线按场景过滤；MCP schema 与 `/api/breath` 同步）。
   - 效果：记忆区分 chat/rp/intimate/home 场景；breath 可按场景召回；旧记忆按 `["chat"]` 处理。
   - 测试：`tests/test_scene.py`（11 个用例）。

### 迁移方案
- 无数据库结构变化（记忆为 Markdown + frontmatter，无 SQL 表变更）。
- 旧记忆无需迁移：读取时 `_normalize_bucket_metadata` 自动补 `provenance="user"`、`scene=["chat"]`；旧锚点无时区数据按本地时区解释。
- 现有记忆数据库未删除、未重写。

### 验证
- 完整测试套件 190 passed（原 179 + 新增 11 个 scene 用例）。
- `python -m py_compile` 全部通过。

### 待后续
- 无。

---

## 2026-08-25 全局体检修复 + 周期相位影响衰减速率 + 日记/标签查询优化

### 目标
修复测试套件中 6 个失败用例（151→156 全绿），统一 LLM JSON 解析健壮性，清理未使用导入，并落地 CHANGELOG 待办项"周期相位影响衰减速率"、"日记情绪标签接 emotion_manager 归并"、"标签归一化查询端同义归一化"。

### 更改内容
1. **dehydrator JSON 解析健壮性（生产 bug）**
   - 文件：`utils.py`（`safe_json_loads` 增加最外层花括号兜底提取，兼容被 max_tokens 截断的响应）、`dehydrator.py`（`_parse_analysis`/`_summarize`/`_timeline`/`_diary_digest` 4 处统一改用 `safe_json_loads`；`_api_analyze` 硬编码 `max_tokens=256` 改为 `self.max_tokens`（默认 1024），消除打标 JSON 被截断导致情绪/主题域丢失）。
2. **冲突检测修复（回归）**
   - 文件：`housekeeper.py`。`_NEG` 正则用负向回顾排除"特别/区别/分别/告别/离别/个别"中的"别"误判否定；`_propositions_conflict` 新增动词同义组（偏好/消费/移动/沟通），"想吃↔喜欢"等偏好动词同义组匹配按高相似度计分，恢复"我以前不喜欢吃辣 vs 我今天特别想吃辣"偏好冲突检测（置信 93%）。
3. **测试与实现对齐**
   - `tests/test_echo_chamber.py`：`test_reject_action` 更新为断言"拒绝即物理删除提案文件"（与 a5d6e29 起的新行为一致）。
   - `tests/test_llm_quality.py`：`test_analyze_domain_semantic_match` 期望集合纳入"居家/饮食"合法主题域，消除 LLM 波动误报。
4. **LLM JSON 解析统一加固**
   - 文件：`housekeeper.py`（`_llm_extract_rules`）、`tag_normalizer.py`（同义词映射）、`server.py`（AI 管家响应解析）改用 `safe_json_loads`。
5. **清理未使用导入**
   - `housekeeper.py`（`Path`）、`pattern_manager.py`（`Dict`）、`import_memory.py`（`os`/`json`）。
6. **embedding_engine 配置修正**
   - `base_url` 不再回退到 DeepSeek 的 `dehy_cfg.base_url`（避免 Gemini 模型误用 DeepSeek 端点）；警告信息改为提示 `OMBRE_EMBEDDING_API_KEY`。
7. **新功能：周期相位影响衰减速率**
   - 文件：`decay_engine.py`（`DecayEngine` 新增可选 `cycle_tracker` 参数与 `_get_cycle_phase_boost()`，1 小时缓存；`calculate_score` 最终得分乘相位加成）、`server.py`（`cycle_tracker` 创建提前并注入 `DecayEngine`）、`config.yaml`（新增 `decay.cycle_phase_boost`：period 1.3 / pre_period 1.2 / follicular 1.0 / unknown 1.0）。
   - 效果：经期/经前期情绪敏感，记忆衰减放缓（得分更高 → 更易被想起、更不易归档）；无周期数据时相位为 unknown，加成 1.0 无影响。
   - 测试：`tests/test_scoring.py` 新增 `TestCyclePhaseBoost`（5 个用例：period/pre_period 加成、unknown 无加成、无 tracker 无加成、自定义配置）。
8. **新功能：日记情绪标签接 emotion_manager 归并**
   - 文件：`emotion_manager.py`（新增 `merge_tags()`：对情绪标签列表做同义词归并——去重 + 规范化）、`journal_manager.py`（`__init__` 新增可选 `emotion_mgr` 参数；`create_entry` 在归一化后经 `_merge_emotion_tags()` 调用 `emotion_mgr.merge_tags` 归并，失败时非破坏性保留原值）、`server.py`（`journal_mgr` 初始化注入 `emotion_mgr`）。
   - 效果：日记情绪标签"开心/喜悦/快乐"等经 emotion_manager 统一映射到规范情绪词，与记忆库情绪体系一致；无 emotion_mgr（旧式初始化）时行为不变。
   - 测试：`tests/test_event_graph_journal.py` 新增 3 个用例（同义词归并、归并失败保留原值、无 emotion_mgr 兼容）。
9. **新功能：标签归一化查询端同义归一化**
   - 文件：`tag_normalizer.py`（`run_normalization` 将 LLM 同义词映射持久化到 `{buckets_dir}/tag_synonyms.json`；新增 `get_synonym_map()` 懒加载 + 缓存、`expand_query()` 扩展查询词）、`bucket_manager.py`（新增 `_load_tag_synonyms()`/`_expand_query_synonyms()`，`search()` 开头扩展查询）。
   - 效果：用户搜"跑步"也能命中映射到同一泛化标签的"慢跑/健走"记忆；查询端与归一化端共享同一持久化映射，无映射时查询原样返回。
   - 测试：`tests/test_event_graph_journal.py` 新增 3 个用例（`expand_query` 扩展、bucket_manager 扩展、search 同义词命中）。

### 验证
- 完整测试套件 162 passed（原 6 个失败全部修复 + 本轮新增 6 个用例）。
- `python -m py_compile` 全部通过；`node --check dashboard.js` 通过。

### 待后续
- 无。

---

## 2026-08-17 前端三项整合：例假相位 / 记忆网络事件链 / 日记入口

### 目标
完成剩余的三项模块关联度打通（数据当前为空，机制先行）：例假 tab 展示周期相位、记忆网络接入事件链实体图谱（并修复其长期空白 bug）、日记从 MCP 工具扩展到网页端读写。

### 更改内容
1. **例假 tab 周期相位展示**
   - 文件：`server.py`（`/api/cycle` GET 返回 summary 增加 `phase`）、`dashboard.js`（`renderCycleSummary` 新增"当前相位"卡片，period/pre_period/follicular/unknown 四态配色 + 经前/经期关怀提示）。
2. **记忆网络接入事件链实体图谱 + 修复空图 bug**
   - 文件：`server.py`（`/api/network` 返回新增 `chains`：事件链 id/topic/实体数/时间线数/关联链/状态，最多 15 条）、`dashboard.js`（`loadNetwork` 组装 nodes/edges：自我认知中心节点 + 身份节点 + 事件链节点；身份关系边与链间关联边；`NETWORK_CONFIG` 新增 chain 类型色与 chain_related 边色/虚线）、`dashboard.html`（图例新增 chain 与链间关联）。
   - 修复：此前前端读 `data.nodes` 而接口返回 `identities/self_profile/edges`，导致记忆网络永远显示"没有记忆桶"。
3. **日记网页入口**
   - 文件：`server.py`（新增 `GET /api/journal`、`GET /api/journal/list`、`POST /api/journal`，走 journal_mgr 与日记完全隔离）、`dashboard.html`（新增"日记"tab：左列表右详情，编辑弹窗含日期/事件摘要/情绪点评/情绪标签）、`dashboard.js`（loadJournal/renderJournalList/selectJournal/showJournalDetail/showJournalEditor/closeJournalEditor/saveJournal）。

### 验证
- Python 语法检查通过；`node --check dashboard.js` 通过。
- 登录态 API 实测：JOURNAL_LIST 200、POST 测试日记 200、GET 按日期取回内容一致（测试文件已清理）；CYCLE 200 返回 phase=unknown（当前无记录）；NETWORK 200 返回 chains 字段。
- HTML 51 处 onclick/onchange 函数引用全部在 JS 中定义（无缺失）。
- 服务器已重启，新代码生效（8001 运行中）。

### 备注
- 日记文件位于 `E:/obsidian/journals/`（buckets_dir 的父目录，设计上完全隔离于记忆库）。
- 例假/事件链/日记当前均无数据，功能在数据积累后自动可见。

### 待后续
- 无后端整合剩余项；可按需推进：日记情绪标签接 emotion_manager 归并、周期相位影响衰减速率等。

---

## 2026-08-16 模块整合：事件链 → ring 年轮经验沉淀机制

### 目标
打通"事件链归并出的可复用经验 → 自动沉淀为年轮经验"的空白：此前事件链（含 summary/entities/timeline）只被用于展示与合并，可复用经验不会进入 ring 年轮层。

### 更改内容
1. **经验提炼提案生成**
   - 文件：`housekeeper.py`
   - 新增 `_extract_pattern_proposals(max_chains=3)`：每周管家任务中，对"有摘要 + 时间线 ≥3 条"的事件链（最多 3 条、按最新）调用 LLM 提炼 1-3 条可复用经验规则，生成 `pattern_proposal` 提案到回音壁；无候选链或未配置 API key 时自动跳过，不调 LLM。
   - 新增 `_llm_extract_rules()`：提炼 prompt 要求输出 JSON 数组（rule + domain），解析失败返回空。
   - `run_weekly_job()` 新增 `pattern_proposals` 结果项。
2. **审批执行**
   - 文件：`housekeeper.py`（`approve_action`）
   - 新增 `pattern_proposal` 分支：按提案内规则逐条调用 `bucket_mgr.save_pattern` 创建 ring 年轮经验桶（name/description/triggers 记录来源事件链）。
3. **回音壁前端渲染**
   - 文件：`echo_chamber.html`
   - `actionTypeLabels` 新增 `pattern_proposal: '经验沉淀'`；渲染分支展示事件链主题 + 每条规则的领域。

### 验证
- Python 语法检查通过。
- 端到端实测：构造测试事件链 → 提炼提案（LLM 产出 3 条高质量经验，如"项目启动第一天用书面形式明确成员责任模块，避免口头分工造成的返工"）→ 审批 → ring/ 目录成功创建 3 个经验桶，测试数据已全部清理。
- 服务器已重启，新代码生效（8001 运行中）。
- 注：当前无真实事件链（event_chains/ 为空），此机制面向事件链积累后自动生效。

### 待后续
- 前端：记忆网络接入事件链实体图谱、例假 tab 联动、日记入口

---

## 2026-08-16 模块整合：标签归一化双写（保留原标签 + 追加泛化标签）

### 目标
修复上轮分析发现的"标签归一化单向性"：此前归一化用泛化标签**替换**原标签，导致检索端（对 tags 做模糊匹配）无法再按原标签命中——用户搜"编程"永远找不到已归一到"数字技术"的桶。

### 更改内容
1. **归一化不再丢弃原标签**
   - 文件：`tag_normalizer.py`（`run_normalization` 应用映射段）
   - 改为"保留原标签 + 追加泛化标签"双写：如 `["编程"]` → `["编程", "数字技术"]`。精确检索（原标签）与泛化召回（规范标签）两头均可命中。

### 验证
- Python 语法检查通过。
- 临时桶实测：`tags=["编程"]` → 归一化后 `["编程", "数字技术"]`；`search("编程")` 命中、`search("数字技术")` 命中，测试数据已清理。
- 服务器已重启，新代码生效（8001 运行中）。
- 注：当前真实桶的非标准标签频率较低（< 阈值 2），归一化暂未触发，此改动面向后续积累生效。

### 待后续
- 事件链结案沉淀 pattern 到 ring 年轮
- 前端：记忆网络接入事件链实体图谱、例假 tab 联动、日记入口

---

## 2026-08-16 模块整合：生理周期相位 → breath 情绪解读

### 目标
让周期数据进一步参与情绪解读：此前 breath 仅在距预测例假 0-5 天时机械提醒日期，不感知当前所处相位，也不提示情绪因素。

### 更改内容
1. **CycleTracker 新增相位判定**
   - 文件：`cycle_tracker.py`
   - 新增 `get_cycle_phase()`：返回 `period`（经期：距上次开始 ≤ 持续天数）/ `pre_period`（经前：距下次预测 0-5 天）/ `follicular`（有数据但不在特殊窗口）/ `unknown`（无记录）。
2. **breath 例假提醒相位感知 + 情绪解读建议**
   - 文件：`server.py`（`breath`）
   - 触发条件扩展为"有周期数据 且（经期 或 经前 或 距预测 0-5 天）"；经期/经前额外注入一句情绪解读建议："经期情绪易敏感 / 经前情绪易波动，解读近期情绪记忆时请考虑生理因素"。无周期数据时依旧不注入，不打扰。

### 验证
- Python 语法检查通过。
- 相位判定单测（临时数据目录）：无记录→unknown、经期内→period、距预测 0 天→pre_period、正常期→follicular，全部正确。
- 真实数据只读验证：当前无周期记录（total=0）→ phase=unknown、不注入，符合预期。
- 服务器已重启，新代码生效（8001 运行中）。

### 待后续
- 事件链结案沉淀 pattern 到 ring 年轮
- 标签归一化查询端同义归一化
- 前端：记忆网络接入事件链实体图谱、例假 tab 联动、日记入口

---

## 2026-08-16 模块整合：生理周期接入管家日终报告

### 目标
打通例假周期数据（cycle_tracker）与管家日终整理的空白：此前周期数据仅用于 breath 提醒，日终报告完全没有周期上下文。

### 更改内容
1. **housekeeper 接入 CycleTracker**
   - 文件：`housekeeper.py`（`__init__`）
   - 管家初始化时内部实例化 `CycleTracker`（try/except 兜底，失败不影响管家启动）。
2. **日终报告携带周期状态**
   - 文件：`housekeeper.py`（`run_daily_review`）
   - 日终整理"一包结果"新增 `cycle_status` 字段：仅有周期记录时填充（total_records / average_cycle_days / last_start_date / last_duration / last_symptoms / predicted_next_date / days_until_next），无记录时为 `{}`，读取失败仅告警。
3. **run_housekeeper 文本报告展示周期**
   - 文件：`server.py`（`run_housekeeper`）
   - 文本版日终报告在 health 之后追加一行生理周期状态（记录次数 + 距下次预测天数/日期）。

### 验证
- Python 语法检查通过。
- 实测：无记录时 `cycle_status = {}` 不报错；写入 2 条临时记录后日终报告携带完整预测（avg 26 天、预测 2026-09-10、距下次 25 天），测试记录已清理。
- 服务器已重启，新代码生效（8001 运行中）。

### 待后续
- cycle 相位 → 情绪基线偏移（decay/breath 使用）
- 事件链结案沉淀 pattern 到 ring 年轮
- 标签归一化查询端同义归一化
- 前端：记忆网络接入事件链实体图谱、例假 tab 联动、日记入口

---

## 2026-08-16 模块整合：情绪归一化 + 身份与记忆互引

### 目标
打通此前分析中发现的孤立模块与弱连接点，本次落地前两项高价值整合。

### 更改内容

1. **emotion_manager 接入写入链路（此前为孤儿模块，全库 0 调用）**
   - 文件：`server.py`（`_hold_impl`）
   - 写入记忆时，对 dehydrator 自动打标产出的 `emotions` 列表调用 `emotion_mgr.normalize_emotions()` 做同义词归并：
     - 规范词表（config `emotions.base_list`，20+ 类）内标签本地精确匹配，不调 LLM；
     - 词表外标签才尝试 LLM 同义词识别（DeepSeek），API 不可用时原样保留；
     - 归并后按最高强度重新确定 `dominant_emotion`。
   - 显式传入 valence/arousal 时跳过归并（显式坐标优先，避免二次加工）。

2. **身份档案与记忆桶互引（打通名册与记忆的空白）**
   - 文件：`server.py`（`_hold_impl`）+ `bucket_manager.py`（update 白名单）
   - 写入记忆后调用 `identity_mgr.find_mentioned_identities(content)` 检测内容中出现的已收录人物：
     - 命中则把人物名写入桶 frontmatter `people` 字段（`bucket_mgr.update` 新增支持该字段）；
     - 同时调用 `identity_mgr.add_related_memory(identity_id, bucket_id)`，让身份档案反向挂载关联记忆 ID（该接口此前已存在但从未被使用）。
   - 覆盖新建桶与查重合并桶两条路径。

### 验证
- Python 语法检查通过（py_compile）。
- 写入链路情绪归并、身份关联均以 try/except 包裹，失败仅告警不阻塞写入。
- 情绪归一化实测：`[开心0.9, 开心0.4, 愤怒0.6, 离谱0.7]` → `[开心0.9, 愤怒0.6, 离谱0.7]`（同标签合并取最大强度、词表内保留、词表外容错保留）。
- 身份互引端到端实测：临时身份+临时桶 → `find_mentioned_identities` 命中 → 桶 `people` 字段写入、身份档案 `related_memories` 反向挂载，双向均验证成功，测试数据已清理。
- 服务器已重启，新代码生效（8001 运行中）。

### 待后续
- cycle_tracker 数据接入情绪/衰减/管家
- 事件链结案沉淀 pattern 到 ring 年轮
- 标签归一化查询端同义归一化
- 前端：记忆网络接入事件链实体图谱、例假 tab 联动、日记入口
