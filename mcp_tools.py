# ============================================================
# Module: MCP Strategy-Efficacy Tools (mcp_tools.py)
# 模块：MCP 策略有效性评估工具
#
# 双 Agent 协作体系下的"主 AI 静默反馈通道"：
#   台前主 AI（祁桉）在对话中或会话结束时，调用 mcp_submit_efficacy_feedback
#   向幕后管家提交"策略有效性评估报告"（strategy_type / effect_score / improvement_note）。
#   管家收到后自动更新对应 ring/（年轮经验/认知桶）的权重与置信度，
#   无需管家离线盲猜——效果好坏由主 AI 本人拍板。
#
# Depended on by: server.py (register_tools)
# 被谁依赖：server.py（register_tools 注册进 MCP 服务）
# ============================================================

import logging

from utils import now_iso, safe_float

logger = logging.getLogger("ombre_brain.mcp_tools")


def register_tools(mcp, ctx):
    """把策略有效性评估工具注册到 MCP 服务器上。

    ctx 需要暴露以下属性（server.py 以 SimpleNamespace 传入）：
      pattern_mgr — PatternManager 实例（ring/ 年轮经验层）
      bucket_mgr  — BucketManager 实例（用于写 efficacy 通用字段）
    """

    async def _match_strategy_bucket(strategy_type: str, strategy_id: str):
        """优先按 strategy_id 精确定位；否则按 strategy_type 在 ring/ 中模糊匹配未被取代的桶。"""
        pattern_mgr = getattr(ctx, "pattern_mgr", None)
        if strategy_id:
            target = await pattern_mgr.get(strategy_id) if pattern_mgr else None
            if target and target.get("metadata", {}).get("superseded_by") is None:
                return target
        if not pattern_mgr or not strategy_type or not strategy_type.strip():
            return None
        tokens = [t.strip() for t in strategy_type.replace("，", ",").split(",") if t.strip()]
        candidates = [p for p in (await pattern_mgr.list_all() or [])
                      if p.get("metadata", {}).get("superseded_by") is None]
        for p in candidates:
            meta = p.get("metadata", {})
            haystack = " ".join([str(meta.get("name", "")), str(meta.get("summary", "")), " ".join(meta.get("tags", []) or [])])
            if any(tok in haystack for tok in tokens if tok):
                return p
        # --- Fallback: name / summary substring match ---
        for p in candidates:
            meta = p.get("metadata", {})
            if strategy_type in str(meta.get("name", "")) or strategy_type in str(meta.get("summary", "")):
                return p
        return None

    @mcp.tool()
    async def mcp_submit_efficacy_feedback(
        strategy_type: str,
        effect_score: float,
        improvement_note: str = "",
        strategy_id: str = "",
    ) -> str:
        """提交"策略有效性评估报告"：主 AI 在对话中或会话结束时调用，告诉管家某条 ring/ 策略的实际效果。
        管家据此自动更新对应认知桶的权重与置信度，不再离线盲猜。
        strategy_type=策略类型/场景（如"安抚焦虑"、"转移注意力"、"讲道理"），用于匹配 ring/ 认知桶。
        effect_score=情绪平复效果评分(0~1, 1=完全有效)。
        improvement_note=改进笔记(可选)。
        strategy_id=可选策略桶ID；不传则按 strategy_type 在 ring/ 中自动匹配。"""
        try:
            score = max(0.0, min(1.0, safe_float(effect_score, 0.5)))
            pattern_mgr = getattr(ctx, "pattern_mgr", None)
            bucket_mgr = getattr(ctx, "bucket_mgr", None)
            if pattern_mgr is None or bucket_mgr is None:
                return "策略反馈通道未初始化，无法处理。"

            target = await _match_strategy_bucket(strategy_type, strategy_id)
            if target is None:
                return (
                    f"未找到匹配的 ring/ 策略桶（strategy_type={strategy_type!r}），权重未更新。"
                    f"可先用 hold 建立对应策略桶后重试。"
                )

            pattern_id = target["id"]
            meta = target.get("metadata", {})
            name = meta.get("name", pattern_id)

            # --- EMA 平滑更新 efficacy_score；追加评估报告（上限 10 条） ---
            old_efficacy = safe_float(meta.get("efficacy_score"), None)
            new_efficacy = round(score if old_efficacy is None else old_efficacy * 0.7 + score * 0.3, 4)
            reports = list(meta.get("efficacy_reports", []) or [])
            reports.append({"at": now_iso(), "score": round(score, 2), "note": improvement_note or ""})
            reports = reports[-10:]

            ok1 = await bucket_mgr.update(
                pattern_id,
                efficacy_score=new_efficacy,
                efficacy_reports=reports,
            )

            # --- confidence 向实测效果收敛（0.8/0.2 加权） ---
            old_conf = safe_float(meta.get("confidence"), 0.5)
            new_conf = round(max(0.0, min(1.0, old_conf * 0.8 + score * 0.2)), 4)
            ok2 = await pattern_mgr.update(pattern_id, confidence=new_conf)

            if not (ok1 or ok2):
                return f"策略桶 {name} 权重更新失败。"
            logger.info(
                f"Efficacy feedback applied / 有效性反馈已生效: {name} "
                f"score={score} efficacy={new_efficacy} confidence={new_conf}"
            )
            return (
                f"已更新策略桶「{name}」：effect_score={score:.2f}，"
                f"efficacy_score={new_efficacy:.3f}（EMA），confidence={new_conf:.3f}，"
                f"累计报告 {len(reports)} 条。"
            )
        except Exception as e:
            logger.error(f"mcp_submit_efficacy_feedback failed / 有效性反馈处理失败: {e}")
            return f"策略有效性评估报告处理失败: {e}"
