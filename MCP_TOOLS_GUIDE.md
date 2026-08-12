# Ombre Brain MCP 工具指南

你（AI）通过以下工具管理用户的长期记忆。记忆桶是基本单元，含正文+元数据（情感坐标、标签、重要度）。非永久记忆随时间衰减。

---

## 检索

breath(query, domain, valence, arousal, importance_min, brief, type, max_results, max_tokens, lightweight, limit, min_score, recent_days)
  空 query=浮现高权重记忆；有 query=三步检索。valence/arousal 0~1 筛选情感，importance_min>=1 按重要度降序。
  lightweight=True=轻量模式：返回稳定JSON字符串，每条仅 summary+bucket_id/valence/arousal/tags/时间/score，省token不做fallback。limit=条数上限(默认5)。min_score=最低相关度(0~1)。recent_days=仅最近N天。
  联动: inject_context()→breath()→回复→hold()

query_memory(query, mode)
  mode=search/float/status/directory/recent，通用入口。

## 外部检索接口 (HTTP)

GET/POST /api/breath — 夜伴等外部系统轻量检索，与 breath(lightweight=True) 同一条路径。
  GET  /api/breath?query=...&limit=5&min_score=0&recent_days=0&type=&domain=&valence=&arousal=
  POST /api/breath  (JSON body 同字段)
  认证: session cookie 或 X-API-Key header（设置 OMBRE_EXTERNAL_API_KEY 后启用）
  返回: {"success":true, "mode":"lightweight", "query":..., "count":N, "results":[{bucket_id, name, summary, valence, arousal, tags, created, score}]}

## 存储

hold(content, importance, tags, pinned, protected, feel, task_flag, source_bucket, source, title, valence, arousal)
  自动情感打标+查重合并。pinned=永久不衰减。protected=受保护(不参与合并/衰减)。feel=True 存AI感受（配合source_bucket标记源记忆已消化）。task_flag=True 在用户脆弱时屏蔽。source=来源标记(如 yeeban)。title=自定义记忆名称。valence/arousal=显式情感坐标(0~1, 提供时优先于自动打标)。

grow(content) 长文本自动拆分存储。

## 外部写入接口 (HTTP)

POST /api/hold — 供夜伴等外部系统稳定写入记忆，与 MCP hold() 完全同一条写入路径。
  请求体: {content(必填), valence(0~1), arousal(0~1), tags(list或逗号字符串), importance(1~10), pinned, protected, task_flag, source(如 yeeban/yeeban_status), title, feel}
  认证: session cookie 或 X-API-Key header（设置 OMBRE_EXTERNAL_API_KEY 环境变量后启用）
  返回: {success, bucket_id, merged, valence, arousal, action, message}

## 管理

trace(bucket_id, resolved, importance, pinned, digested, content, delete, ...)
  日常维护最常用。resolved=1沉底，delete=True删除（移入回收站，24h可恢复）。

manage_record(action, record_type, record_id, ...)
  CRUD操作。record_type: identity/pattern/candlestick/experience/annual_ring。标题为空时自动从内容生成。

## 隐私

lock_memory(bucket_id, password) 锁定，前端需密码查看，AI检索不受影响。密码SHA256存储。
unlock_memory(bucket_id) 解锁。

## 周期

record_cycle(start_date, symptoms, duration, flow_level, pain_level)
  记录例假，自动预测下次日期。距预测0-5天时breath()自动追加提醒。至少2次记录才开始预测。

## 每日日志

complete_journal(date, mood_comment, emotion_tags)
  主AI为指定日期日记补充情绪点评和心情标签。date留空默认今天。管家已生成事件摘要，此工具补充情感层面。
  日记与记忆桶完全隔离，不参与breath()/inject_context()。

query_journal(date, keyword, limit)
  按日期精确查询或关键词搜索日记。date优先；无date和keyword时返回最近条目列表。
  日记不自动浮现，仅在用户询问具体日期或相关话题时主动调用此工具查询。

## 批量

memory_batch_delete(bucket_ids) 批量删除。
smart_organize(days, importance_drop) 降低过期记忆权重。
weekly_organize() 生成周报告。
tag_normalize(action) 标签归一化。

## 关系

link_events(prev_id, next_id) 建立因果链（prev=前因, next=后果）。
manage_relation(action, bucket_id, target_id) link/parent chain/importance。
manage_identity_relation(action, from_id, to_id, relation_type) 人物关系。
trace_chain(bucket_id, direction) 追溯因果链, direction=previous/next/both。

## 查询

get_roster(name) get_experiences() get_memos() get_anchors(active_only) get_timelines() get_event_chains()

## AI分析

ai_analyze(task, bucket_id, query) task=link/find/chain/summarize/classify。
ai_manage(request) 自然语言请求，自动调用工具。

## 系统

pulse() 系统概览。analytics() 统计分析。memory_directory(detail_level) 目录摘要。

## 管家与回音壁

管家仅生成提案，不直接执行任何破坏性操作。所有提案需主AI审批后才执行。记忆衰减是自动机制，无需审批。

run_housekeeper() 每日管家：事件摘要写入日记、事件链自动生成、冲突检测、高频人物收录提案。生成提案到回音壁。
run_weekly_housekeeper() 每周管家：事件链合并提案、过期记忆清理提案。仅提案不执行。
review_digest() 查看待办提案。
approve_action(action_id) 批准并执行（cleanup→删除记忆, conflict→标记旧记忆已解决, chain_merge→合并事件链, identity_proposal→创建身份档案）。
reject_action(action_id) 驳回。
approve_event_chain(chain_id) 事件链结案。

## 其他

dream() 读取最近记忆供自省，读后hold(feel=True, source_bucket=ID)消化或trace(resolved=1)沉底。
summarize_recent_events(days) 最近事件概括。
inject_context(user_input) 静默预处理，自动注入相关记忆到Prompt，并在<context>上下包裹[系统硬性指令]（优先度校验/拒绝无依据联想/逻辑聚焦）与[输出要求]；无相关记忆时返回空字符串。
memory_export(export_type) export_brain(output_path) import_brain(zip_path, overwrite)

---

## 典型工作流

1. 日常对话: inject_context()→breath()→回复→hold()→视情况lock_memory()
2. 长日记: grow()→ai_analyze(task="link")→run_housekeeper()
3. 事件链: hold(A)→hold(B)→link_events(A,B)→trace_chain()追溯→approve_event_chain()结案→manage_record(create,experience)提炼
4. 冲突处理: run_housekeeper()检测→review_digest()查看→approve_action()/reject_action()
5. 人物管理: manage_record(create,identity)→manage_identity_relation(add)→get_roster()
6. 记忆清理: run_weekly_housekeeper()→review_digest()→approve_action()或memory_batch_delete()
7. 敏感信息: hold()→lock_memory()→breath()仍可读取用于关怀
8. 例假关怀: record_cycle()记录→breath()临近时自动提醒→调整回复风格
9. 情感关怀: hold(feel=True)→analytics()→breath(valence=0.2)→主动关怀
10. 自省消化: dream()→hold(feel=True,source_bucket=ID)→trace(resolved=1)
11. 每日日志: run_housekeeper()生成事件摘要→complete_journal()补充情绪点评→后续query_journal()按需查询

---

## 原则

1. 操作前先breath()避免重复存储
2. 单条用hold()，长文本用grow()，备忘用manage_record(candlestick)
3. 敏感信息存储后lock_memory()，AI不受限
4. task_flag=True的记忆在用户脆弱时自动屏蔽
5. link_events: prev=先发生(前因), next=后发生(后果)
6. hold(feel=True,source_bucket=ID)自动标记源记忆已消化
7. 删除走回收站，24h内可restore
8. 记忆过期提醒: breath()浮现模式和inject_context()会自动返回权重<0.3的记忆，AI应在合适时机提醒用户是否保留（trace提升重要度或pinned钉选）
9. 管家仅提案不执行: 所有破坏性操作（删除/合并/清理）需主AI通过approve_action()审批后执行，记忆衰减除外
10. 日记隔离: 每日日志与记忆桶系统完全隔离，不参与breath()/inject_context()，仅通过query_journal()主动查询
11. 事件链自动生成: 管家每日自动检测长效事件并生成/追加事件链，link_events()仅作手动补充
12. 高频人物收录: 管家检测7天内被提及>=3次且未收录的人物，自动提交identity_proposal提案，由主AI审批
