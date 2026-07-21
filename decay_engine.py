# ============================================================
# Module: Memory Decay Engine (decay_engine.py)
# 模块：记忆衰减引擎
#
# Simulates human forgetting curve; auto-decays inactive memories and archives them.
# 模拟人类遗忘曲线，自动衰减不活跃记忆并归档。
#
# Three-stage decay based on Ebbinghaus forgetting curve:
# 基于艾宾浩斯遗忘曲线的三阶段衰减：
#   Stage 1 (0-7 days): Full content - 完整内容描述
#   Stage 2 (7-30 days): Summarized content - 总结性描述（AI生成）
#   Stage 3 (30+ days): Digested - 已消化（知识内化）
#
# Core formula (improved Ebbinghaus + emotion coordinates):
# 核心公式（改进版艾宾浩斯遗忘曲线 + 情感坐标）：
#   Score = Importance × (activation_count^0.3) × e^(-λ×days) × emotion_weight
#
# Emotion weight (continuous coordinate, not discrete labels):
# 情感权重（基于连续坐标而非离散列举）：
#   emotion_weight = base + (arousal × arousal_boost)
#   Higher arousal → higher emotion weight → slower decay
#   唤醒度越高 → 情感权重越大 → 记忆衰减越慢
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import math
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("ombre_brain.decay")


class DecayEngine:
    """
    Memory decay engine — periodically scans all dynamic buckets,
    calculates decay scores, auto-archives low-activity buckets
    to simulate natural forgetting.
    记忆衰减引擎 —— 定期扫描所有动态桶，
    计算衰减得分，将低活跃桶自动归档，模拟自然遗忘。
    """

    def __init__(self, config: dict, bucket_mgr):
        # --- Load decay parameters / 加载衰减参数 ---
        decay_cfg = config.get("decay", {})
        self.decay_lambda = decay_cfg.get("lambda", 0.05)
        self.threshold = decay_cfg.get("threshold", 0.3)
        self.check_interval = decay_cfg.get("check_interval_hours", 24)

        # --- Emotion weight params (continuous arousal coordinate) ---
        # --- 情感权重参数（基于连续 arousal 坐标）---
        emotion_cfg = decay_cfg.get("emotion_weights", {})
        self.emotion_base = emotion_cfg.get("base", 1.0)
        self.arousal_boost = emotion_cfg.get("arousal_boost", 0.8)

        self.bucket_mgr = bucket_mgr

        # --- Background task control / 后台任务控制 ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the decay engine is running in the background.
        衰减引擎是否正在后台运行。"""
        return self._running

    # ---------------------------------------------------------
    # Core: calculate decay score for a single bucket
    # 核心：计算单个桶的衰减得分
    #
    # Higher score = more vivid memory; below threshold → archive
    # 得分越高 = 记忆越鲜活，低于阈值则归档
    # Permanent buckets never decay / 固化桶永远不衰减
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Freshness bonus: continuous exponential decay
    # 新鲜度加成：连续指数衰减
    # bonus = 1.0 + 1.0 × e^(-t/36), t in hours
    # t=0 → 2.0×, t≈25h(半衰) → 1.5×, t≈72h → ≈1.14×, t→∞ → 1.0×
    # ---------------------------------------------------------
    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        """
        Freshness bonus multiplier: 1.0 + e^(-t/36), t in hours.
        新鲜度加成乘数：刚存入×2.0，~36小时半衰，72小时后趋近×1.0。
        """
        hours = days_since * 24.0
        return 1.0 + 1.0 * math.exp(-hours / 36.0)

    def _calc_emotion_arousal(self, metadata: dict) -> float:
        """
        Calculate emotion arousal score (0.0~1.0 continuous).
        计算情绪唤醒度得分，用于衰减计算中的基础权重。
        """
        try:
            emotion_metrics = metadata.get("emotion_metrics", {})
            if isinstance(emotion_metrics, dict) and "arousal" in emotion_metrics:
                return max(0.0, min(1.0, float(emotion_metrics.get("arousal", 0.3))))
            
            if "emotions" in metadata and metadata["emotions"]:
                emotions = metadata["emotions"]
                max_intensity = max(float(e.get("intensity", 0.0)) for e in emotions)
                return max(0.0, min(1.0, max_intensity))
            
            arousal = float(metadata.get("arousal", 0.3))
            return max(0.0, min(1.0, arousal))
        except (ValueError, TypeError):
            return 0.3

    def calculate_score(self, metadata: dict) -> float:
        """
        Calculate current activity score for a memory bucket.
        计算一个记忆桶的当前活跃度得分。

        New model: continuous multi-dimensional scoring system.
        新模型：连续多维评分系统。
        - Emotion_Arousal (0.0~1.0): 情绪唤醒度，越高衰减越慢
        - Explicit_Priority (0/1): 显式优先级（钉选），已在前置判断中处理
        - Activation_Count: 激活次数，越多衰减越慢
        - Time_Decay: 时间衰减
        """
        if not isinstance(metadata, dict):
            return 0.0

        # --- Pinned/protected buckets: never decay ---
        if metadata.get("pinned") or metadata.get("protected"):
            return 999.0

        # --- Permanent buckets never decay ---
        if metadata.get("type") == "permanent":
            return 999.0

        # --- Feel buckets: never decay, fixed moderate score ---
        if metadata.get("type") == "feel":
            return 50.0

        # --- Experience (年轮) buckets: reinforcement-based decay ---
        # 经验桶：基于强化频次和时间衰减的动态权重计算
        if metadata.get("type") == "experience":
            emotion_arousal = self._calc_emotion_arousal(metadata)
            base_weight = 1.0 + emotion_arousal * 9.0
            hit_count = max(0, int(metadata.get("hit_count", 0)))
            
            last_hit_str = metadata.get("last_hit", metadata.get("created", ""))
            try:
                last_hit = datetime.fromisoformat(str(last_hit_str))
                if last_hit.tzinfo is None:
                    last_hit = last_hit.replace(tzinfo=timezone.utc)
                days_since_hit = max(0.0, (datetime.now(timezone.utc) - last_hit).total_seconds() / 86400)
            except (ValueError, TypeError):
                days_since_hit = 0
            
            exp_decay_lambda = 0.02
            
            if hit_count > 0:
                reinforcement_factor = 1 + math.log(hit_count)
            else:
                reinforcement_factor = 1.0
            
            time_decay = math.exp(-exp_decay_lambda * days_since_hit)
            
            score = base_weight * reinforcement_factor * time_decay
            
            return round(max(1.0, score), 4)

        # --- Pattern buckets: never decay normally, but superseded patterns get low score ---
        if metadata.get("type") == "pattern":
            if metadata.get("superseded_by") is not None:
                return 0.1
            return 999.0

        # --- Continuous base weight: 1.0 + emotion_arousal * 9.0 (maps 0.0~1.0 to 1~10) ---
        emotion_arousal = self._calc_emotion_arousal(metadata)
        base_weight = 1.0 + emotion_arousal * 9.0
        activation_count = max(1.0, float(metadata.get("activation_count", 1)))

        # --- Days since last activation ---
        last_active_str = metadata.get("last_active", metadata.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            days_since = max(0.0, (datetime.now(timezone.utc) - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days_since = 30

        # --- Identity (名册) buckets: slower decay, very high base weight ---
        # 名册人物：衰减较慢，基础权重很高（保证刚创建时得分 >= 50）
        if metadata.get("type") == "identity":
            if metadata.get("pinned") or metadata.get("protected"):
                return 999.0
            identity_decay_lambda = self.decay_lambda * 0.3
            identity_base_weight = 51.0
            return (
                identity_base_weight
                * (activation_count ** 0.3)
                * math.exp(-identity_decay_lambda * days_since)
            )

        # --- Emotion weight with intensity ---
        # 情绪权重 = 基础值 + 唤醒度 * 唤醒度加成 * 情绪波动强度
        try:
            arousal = emotion_arousal
            valence = max(0.0, min(1.0, float(metadata.get("valence", 0.5))))
        except (ValueError, TypeError):
            arousal = 0.3
            valence = 0.5
        
        emotion_intensity = arousal * (1.0 + abs(valence - 0.5))
        emotion_weight = self.emotion_base + arousal * self.arousal_boost * emotion_intensity

        # --- Time weight ---
        time_weight = self._calc_time_weight(days_since)

        # --- Short-term vs Long-term weight separation ---
        if days_since <= 3.0:
            combined_weight = time_weight * 0.7 + emotion_weight * 0.3
        else:
            combined_weight = emotion_weight * 0.7 + time_weight * 0.3

        # --- Base score ---
        base_score = (
            base_weight
            * (activation_count ** 0.3)
            * math.exp(-self.decay_lambda * days_since)
            * combined_weight
        )

        # --- Weight pool modifiers ---
        resolved = metadata.get("resolved", False)
        digested = metadata.get("digested", False)
        if resolved and digested:
            resolved_factor = 0.02
        elif resolved:
            resolved_factor = 0.05
        else:
            resolved_factor = 1.0
        urgency_boost = 1.5 if (arousal > 0.7 and not resolved) else 1.0

        return round(base_score * resolved_factor * urgency_boost, 4)

    # ---------------------------------------------------------
    # Execute one decay cycle
    # 执行一轮衰减周期
    # Scan all dynamic buckets → score → archive those below threshold
    # 扫描所有动态桶 → 算分 → 低于阈值的归档
    # ---------------------------------------------------------
    def calculate_decay_stage(self, metadata: dict) -> int:
        """
        Calculate decay stage based on Ebbinghaus forgetting curve:
        根据艾宾浩斯遗忘曲线计算衰减阶段：
        Stage 1 (0-7 days): Full content - 完整内容描述
        Stage 2 (7-30 days): Summarized content - 总结性描述（AI生成）
        Stage 3 (30+ days): Digested - 已消化（知识内化）
        
        Returns: 1, 2, or 3
        """
        if not isinstance(metadata, dict):
            return 1
        
        last_active_str = metadata.get("last_active", metadata.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            days_since = max(0.0, (datetime.now(timezone.utc) - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days_since = 0
        
        if days_since < 7:
            return 1
        elif days_since < 30:
            return 2
        else:
            return 3

    async def run_decay_cycle(self) -> dict:
        """
        Execute one decay cycle: iterate dynamic buckets, archive those
        scoring below threshold, and manage three-stage decay.
        执行一轮衰减：遍历动态桶，归档得分低于阈值的桶，管理三阶段衰减。

        Returns stats: {"checked": N, "archived": N, "lowest_score": X}
        """
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for decay / 衰减周期列桶失败: {e}")
            return {"checked": 0, "archived": 0, "lowest_score": 0, "error": str(e)}

        checked = 0
        archived = 0
        auto_resolved = 0
        stage_1_to_2 = 0
        stage_2_to_3 = 0
        lowest_score = float("inf")

        for bucket in buckets:
            meta = bucket.get("metadata", {})

            # Skip permanent / pinned / protected / feel buckets
            # 跳过固化桶、钉选/保护桶、feel桶
            # identity类型参与衰减，但衰减较慢（30天完全消退）
            # experience类型参与衰减，基于强化频次和时间衰减
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue

            checked += 1

            current_stage = meta.get("decay_stage", 1)
            new_stage = self.calculate_decay_stage(meta)

            # --- Stage transition: 1 → 2 (7 days) ---
            # --- 阶段转换：完整内容 → 总结性描述 ---
            if current_stage == 1 and new_stage == 2:
                try:
                    await self.bucket_mgr.update(bucket["id"], decay_stage=2)
                    stage_1_to_2 += 1
                    logger.info(
                        f"Decay stage 1→2 / 衰减阶段1→2: "
                        f"{meta.get('name', bucket['id'])}"
                    )
                except Exception as e:
                    logger.warning(f"Stage 1→2 transition failed / 阶段1→2转换失败: {e}")

            # --- Stage transition: 2 → 3 (30 days) ---
            # --- 阶段转换：总结性描述 → 已消化 ---
            elif current_stage == 2 and new_stage == 3:
                try:
                    await self.bucket_mgr.update(bucket["id"], decay_stage=3, digested=True)
                    stage_2_to_3 += 1
                    logger.info(
                        f"Decay stage 2→3 / 衰减阶段2→3 (digested): "
                        f"{meta.get('name', bucket['id'])}"
                    )
                except Exception as e:
                    logger.warning(f"Stage 2→3 transition failed / 阶段2→3转换失败: {e}")

            # --- Auto-resolve: emotion_arousal <= 0.33 (equivalent to old imp≤4) + >30 days + not resolved → auto resolve ---
            # --- 自动结案：情绪唤醒度≤0.33（等价于旧重要度≤4）+ 超过30天 + 未解决 → 自动 resolve ---
            if not meta.get("resolved", False):
                emotion_arousal = self._calc_emotion_arousal(meta)
                last_active_str = meta.get("last_active", meta.get("created", ""))
                try:
                    last_active = datetime.fromisoformat(str(last_active_str))
                    if last_active.tzinfo is None:
                        last_active = last_active.replace(tzinfo=timezone.utc)
                    days_since = (datetime.now(timezone.utc) - last_active).total_seconds() / 86400
                except (ValueError, TypeError):
                    days_since = 999
                if emotion_arousal <= 0.33 and days_since > 30:
                    try:
                        await self.bucket_mgr.update(bucket["id"], resolved=True)
                        meta["resolved"] = True  # refresh local meta so resolved_factor applies this cycle
                        auto_resolved += 1
                        logger.info(
                            f"Auto-resolved / 自动结案: "
                            f"{meta.get('name', bucket['id'])} "
                            f"(arousal={emotion_arousal:.2f}, days={days_since:.0f})"
                        )
                    except Exception as e:
                        logger.warning(f"Auto-resolve failed / 自动结案失败: {e}")

            try:
                score = self.calculate_score(meta)
            except Exception as e:
                logger.warning(
                    f"Score calculation failed for {bucket.get('id', '?')} / "
                    f"计算得分失败: {e}"
                )
                continue

            lowest_score = min(lowest_score, score)

            # --- Below threshold → archive (simulate forgetting) ---
            # --- 低于阈值 → 归档（模拟遗忘）---
            if score < self.threshold:
                try:
                    success = await self.bucket_mgr.archive(bucket["id"])
                    if success:
                        archived += 1
                        logger.info(
                            f"Decay archived / 衰减归档: "
                            f"{meta.get('name', bucket['id'])} "
                            f"(score={score:.4f}, threshold={self.threshold})"
                        )
                except Exception as e:
                    logger.warning(
                        f"Archive failed for {bucket.get('id', '?')} / "
                        f"归档失败: {e}"
                    )

        # --- Timeline decay: decay timelines that are too old ---
        # --- 时间链衰减：衰减过旧的时间链，用一句话总结替换完整内容 ---
        timelines_decayed = 0
        try:
            timelines = await self.bucket_mgr.get_timelines()
            for timeline in timelines:
                if timeline.get("decayed"):
                    continue
                
                created_str = timeline.get("created", "")
                try:
                    created = datetime.fromisoformat(str(created_str))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    days_since = (datetime.now(timezone.utc) - created).total_seconds() / 86400
                except (ValueError, TypeError):
                    days_since = 999
                
                if days_since > 30:
                    success = await self.bucket_mgr.decay_timeline(timeline["id"])
                    if success:
                        timelines_decayed += 1
                        logger.info(f"Timeline decayed / 时间链已衰减: {timeline['title']} (days={days_since:.0f})")
        except Exception as e:
            logger.warning(f"Timeline decay failed / 时间链衰减失败: {e}")

        result = {
            "checked": checked,
            "archived": archived,
            "auto_resolved": auto_resolved,
            "stage_1_to_2": stage_1_to_2,
            "stage_2_to_3": stage_2_to_3,
            "timelines_decayed": timelines_decayed,
            "lowest_score": lowest_score if checked > 0 else 0,
        }
        logger.info(f"Decay cycle complete / 衰减周期完成: {result}")
        return result

    # ---------------------------------------------------------
    # Background decay task management
    # 后台衰减任务管理
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """
        Ensure the decay engine is started (lazy init on first call).
        确保衰减引擎已启动（懒加载，首次调用时启动）。
        """
        if not self._running:
            await self.start()

    async def start(self) -> None:
        """Start the background decay loop.
        启动后台衰减循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Decay engine started, interval: {self.check_interval}h / "
            f"衰减引擎已启动，检查间隔: {self.check_interval} 小时"
        )

    async def stop(self) -> None:
        """Stop the background decay loop.
        停止后台衰减循环。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Decay engine stopped / 衰减引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: run decay → sleep → repeat.
        后台循环体：执行衰减 → 睡眠 → 重复。"""
        while self._running:
            try:
                await self.run_decay_cycle()
            except Exception as e:
                logger.error(f"Decay cycle error / 衰减周期出错: {e}")
            # --- Wait for next cycle / 等待下一个周期 ---
            try:
                await asyncio.sleep(self.check_interval * 3600)
            except asyncio.CancelledError:
                break
