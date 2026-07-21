# ============================================================
# Module: Identity Manager (identity_manager.py)
# 模块：身份管理器
#
# Manages identity layer - person profiles with relationships.
# 管理身份层 —— 人物身份信息和关系描述。
#
# Identity fields:
#   name: 姓名
#   aliases: 别名数组
#   basic_info: 键值对（身高、体重、年龄等）
#   core_traits: 性格关键词数组
#   relationships: 与其他identity的关系描述数组
#
# Depended on by: server.py
# ============================================================

import os
import logging
import json
import math
from datetime import datetime
from typing import Optional, Dict, List

import frontmatter

from utils import generate_bucket_id, sanitize_name, safe_path, now_iso

logger = logging.getLogger("ombre_brain.identity")


class IdentityManager:
    """
    Identity manager — CRUD operations for person identity profiles.
    Identity files are stored as Markdown with YAML frontmatter.
    身份管理器 —— 人物身份信息的增删改查。
    """

    def __init__(self, config: dict):
        self.base_dir = os.path.join(config["buckets_dir"], "identity")
        os.makedirs(self.base_dir, exist_ok=True)
        
        # --- Relationship graph storage / 关系图存储 ---
        # Simple JSON adjacency list structure:
        # {
        #   "identity_id": [
        #     {
        #       "target_id": "other_identity_id",
        #       "relation_type": "朋友",
        #       "base_weight": 5.0,
        #       "last_mentioned": "2026-07-21T10:00:00",
        #       "created": "2026-07-21T10:00:00",
        #       "mention_count": 1
        #     }
        #   ]
        # }
        self.relationships_file = os.path.join(self.base_dir, "relationships.json")
        os.makedirs(self.base_dir, exist_ok=True)
        if not os.path.exists(self.relationships_file):
            with open(self.relationships_file, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        
        # --- Decay constants / 衰减常量 ---
        self.relation_half_life_days = 30.0  # Half-life for relationship weight decay
        self.min_weight = 0.1  # Minimum weight before relationship becomes inactive

    async def create(
        self,
        name: str,
        aliases: List[str] = None,
        basic_info: Dict[str, str] = None,
        core_traits: List[str] = None,
        relationships: List[str] = None,
        content: str = "",
    ) -> str:
        """
        Create a new identity profile.
        
        Args:
            name: 姓名
            aliases: 别名数组（昵称、外号等）
            basic_info: 基础信息键值对（如身高、体重、年龄等）
            core_traits: 性格特征关键词数组
            relationships: 关系描述数组（如"与张三是朋友"）
            content: 补充描述内容
        
        Returns:
            identity_id
        """
        if not name or not name.strip():
            raise ValueError("姓名不能为空")

        identity_id = generate_bucket_id()
        aliases = aliases or []
        basic_info = basic_info or {}
        core_traits = core_traits or []
        relationships = relationships or []

        metadata = {
            "id": identity_id,
            "name": sanitize_name(name),
            "aliases": aliases,
            "basic_info": basic_info,
            "core_traits": core_traits,
            "relationships": relationships,
            "type": "identity",
            "created": now_iso(),
            "last_active": now_iso(),
            "pinned": False,
            "protected": False,
            "related_memories": [],
            "activation_count": 0,
        }

        post = frontmatter.Post(content or "", **metadata)

        filename = f"{sanitize_name(name)}_{identity_id}.md"
        file_path = safe_path(self.base_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.info(f"Created identity / 创建身份: {identity_id} ({name})")
        return identity_id

    async def get(self, identity_id: str) -> Optional[dict]:
        """
        Get identity by ID.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return None
        return self._load_identity(file_path)

    async def get_by_name(self, name: str) -> Optional[dict]:
        """
        Get identity by name (fuzzy match).
        """
        identities = await self.list_all()
        for ident in identities:
            meta = ident.get("metadata", {})
            if name == meta.get("name"):
                return ident
            if name in meta.get("aliases", []):
                return ident
        return None

    async def find_mentioned_identities(self, text: str) -> list[dict]:
        """
        Passive trigger: find identities mentioned in text.
        Only returns identities whose name or aliases appear in the text.
        
        Also updates relationship last_mentioned timestamps when identities are mentioned.

        被动触发：在文本中查找被提及的身份档案。
        只有当文本中出现名册实体的名称或别名时才返回对应档案。
        
        同时更新被提及身份的关系 last_mentioned 时间戳，用于权重衰减计算。
        """
        if not text or not text.strip():
            return []

        identities = await self.list_all()
        mentioned = []

        for ident in identities:
            meta = ident.get("metadata", {})
            name = meta.get("name", "")
            aliases = meta.get("aliases", [])
            identity_id = meta.get("id", "")

            # Check if name or any alias appears in text
            if name and name in text:
                mentioned.append(ident)
                # --- Touch relationships when identity is mentioned ---
                # --- 身份被提及时触碰所有关系 ---
                await self._touch_all_relations(identity_id)
                continue
            if aliases:
                for alias in aliases:
                    if alias and alias in text:
                        mentioned.append(ident)
                        # --- Touch relationships when identity is mentioned ---
                        # --- 身份被提及时触碰所有关系 ---
                        await self._touch_all_relations(identity_id)
                        break

        return mentioned
    
    async def _touch_all_relations(self, identity_id: str):
        """
        Touch all relations connected to this identity (both directions).
        
        触碰与该身份相关的所有关系（双向），用于权重衰减刷新。
        """
        rels = self._load_relationships()
        touched = False
        
        # --- Touch outgoing relations ---
        # --- 触碰出向关系 ---
        if identity_id in rels:
            for rel in rels[identity_id]:
                rel["last_mentioned"] = now_iso()
                rel["mention_count"] = rel.get("mention_count", 0) + 1
                touched = True
        
        # --- Touch incoming relations ---
        # --- 触碰入向关系 ---
        for from_id, from_rels in rels.items():
            for rel in from_rels:
                if rel.get("target_id") == identity_id:
                    rel["last_mentioned"] = now_iso()
                    rel["mention_count"] = rel.get("mention_count", 0) + 1
                    touched = True
        
        if touched:
            self._save_relationships(rels)
            logger.debug(f"Touched all relations for / 触碰所有关系: {identity_id}")

    async def list_pinned(self) -> list[dict]:
        """
        List only pinned/protected identities (core profiles).
        只列出钉选/保护的身份档案（核心档案）。
        """
        identities = await self.list_all()
        return [
            ident for ident in identities
            if ident.get("metadata", {}).get("pinned")
            or ident.get("metadata", {}).get("protected")
        ]

    async def update(
        self,
        identity_id: str,
        name: str = None,
        aliases: List[str] = None,
        basic_info: Dict[str, str] = None,
        core_traits: List[str] = None,
        relationships: List[str] = None,
        content: str = None,
        pinned: bool = None,
        protected: bool = None,
    ) -> bool:
        """
        Update identity profile.
        Only passed fields will be updated.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)

        if name:
            post["name"] = sanitize_name(name)
        if aliases is not None:
            post["aliases"] = aliases
        if basic_info is not None:
            post["basic_info"] = basic_info
        if core_traits is not None:
            post["core_traits"] = core_traits
        if relationships is not None:
            post["relationships"] = relationships
        if content is not None:
            post.content = content
        if pinned is not None:
            post["pinned"] = pinned
        if protected is not None:
            post["protected"] = protected
        post["last_active"] = now_iso()

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.info(f"Updated identity / 更新身份: {identity_id}")
        return True

    async def toggle_pin(self, identity_id: str) -> bool:
        """
        Toggle pinned status for an identity.
        Pinned identities will never decay.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        current_pinned = post.get("pinned", False)
        post["pinned"] = not current_pinned
        post["last_active"] = now_iso()

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.info(f"Identity pinned toggled / 身份钉选状态切换: {identity_id} -> {not current_pinned}")
        return True

    async def add_related_memory(self, identity_id: str, memory_id: str) -> bool:
        """
        Add a related memory bucket to an identity.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        related_memories = post.get("related_memories", [])
        if memory_id not in related_memories:
            related_memories.append(memory_id)
            post["related_memories"] = related_memories
            post["last_active"] = now_iso()
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            logger.info(f"Added related memory to {identity_id}: {memory_id}")
        return True

    async def remove_related_memory(self, identity_id: str, memory_id: str) -> bool:
        """
        Remove a related memory bucket from an identity.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        related_memories = post.get("related_memories", [])
        if memory_id in related_memories:
            related_memories.remove(memory_id)
            post["related_memories"] = related_memories
            post["last_active"] = now_iso()
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            logger.info(f"Removed related memory from {identity_id}: {memory_id}")
        return True

    async def increment_activation(self, identity_id: str) -> bool:
        """
        Increment activation count for an identity.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        post["activation_count"] = post.get("activation_count", 0) + 1
        post["last_active"] = now_iso()

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        return True

    async def delete(self, identity_id: str) -> bool:
        """
        Delete identity.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        os.remove(file_path)
        logger.info(f"Deleted identity / 删除身份: {identity_id}")
        return True

    # ---------------------------------------------------------
    # Relationship graph operations / 关系图操作
    # ---------------------------------------------------------
    def _load_relationships(self) -> dict:
        """
        Load relationships from JSON file.
        从 JSON 文件加载关系图。
        """
        if not os.path.exists(self.relationships_file):
            return {}
        try:
            with open(self.relationships_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load relationships / 加载关系图失败: {e}")
            return {}

    def _save_relationships(self, data: dict):
        """
        Save relationships to JSON file.
        保存关系图到 JSON 文件。
        """
        with open(self.relationships_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def calculate_decayed_weight(
        self,
        base_weight: float,
        last_mentioned: str,
        is_pinned: bool = False,
    ) -> float:
        """
        Calculate decayed weight based on time since last mentioned.
        
        基于上次提及时间计算衰减后的权重。
        
        Formula: weight = base_weight * exp(-days_since_last_mentioned / half_life)
        
        Args:
            base_weight: Base weight of the relationship (1.0 ~ 10.0)
            last_mentioned: ISO timestamp of last mention
            is_pinned: If True, weight never decays
        
        Returns:
            Decayed weight (clamped between min_weight and base_weight)
        """
        if is_pinned:
            return base_weight
        
        if not last_mentioned:
            return self.min_weight
        
        try:
            last_time = datetime.fromisoformat(last_mentioned.replace("Z", "+00:00"))
            now = datetime.now()
            days_since = (now - last_time).total_seconds() / (24 * 3600)
            
            if days_since <= 0:
                return base_weight
            
            decayed = base_weight * math.exp(-days_since / self.relation_half_life_days)
            return max(self.min_weight, decayed)
        except Exception as e:
            logger.warning(f"calculate_decayed_weight failed / 权重衰减计算失败: {e}")
            return self.min_weight

    async def add_relation(
        self,
        from_id: str,
        to_id: str,
        relation_type: str = "朋友",
        base_weight: float = 5.0,
    ) -> bool:
        """
        Add a relationship between two identities.
        
        在两个身份之间建立关系。
        
        Args:
            from_id: Source identity ID
            to_id: Target identity ID
            relation_type: Type of relationship (e.g., "朋友", "同事", "家人")
            base_weight: Base weight (1.0 ~ 10.0)
        
        Returns:
            True if successful
        """
        if from_id == to_id:
            logger.warning("Cannot create self-relation / 不能创建自关系")
            return False
        
        # --- Validate both identities exist ---
        # --- 验证两个身份都存在 ---
        if not await self.get(from_id) or not await self.get(to_id):
            logger.warning("add_relation failed: identity not found / 建立关系失败：身份不存在")
            return False
        
        rels = self._load_relationships()
        
        # --- Initialize if needed ---
        if from_id not in rels:
            rels[from_id] = []
        
        # --- Check if relation already exists ---
        # --- 检查关系是否已存在 ---
        existing = None
        for rel in rels[from_id]:
            if rel.get("target_id") == to_id:
                existing = rel
                break
        
        now = now_iso()
        if existing:
            # --- Update existing relation ---
            # --- 更新已有关系 ---
            existing["relation_type"] = relation_type
            existing["base_weight"] = base_weight
            existing["last_mentioned"] = now
            existing["mention_count"] = existing.get("mention_count", 0) + 1
            logger.info(f"Updated relation / 更新关系: {from_id} -> {to_id} ({relation_type})")
        else:
            # --- Create new relation ---
            # --- 创建新关系 ---
            rels[from_id].append({
                "target_id": to_id,
                "relation_type": relation_type,
                "base_weight": base_weight,
                "last_mentioned": now,
                "created": now,
                "mention_count": 1,
            })
            logger.info(f"Created relation / 创建关系: {from_id} -> {to_id} ({relation_type}, weight={base_weight})")
        
        self._save_relationships(rels)
        return True

    async def get_relations(
        self,
        identity_id: str,
        include_decayed: bool = False,
    ) -> list[dict]:
        """
        Get all relationships for an identity, with decayed weights.
        
        获取身份的所有关系，包含衰减后的权重。
        
        Args:
            identity_id: Identity ID
            include_decayed: If False, exclude relations with weight below min_weight
        
        Returns:
            List of relationship dicts with calculated effective_weight
        """
        rels = self._load_relationships()
        identity_rels = rels.get(identity_id, [])
        
        result = []
        identity = await self.get(identity_id)
        is_pinned = identity.get("metadata", {}).get("pinned", False) if identity else False
        
        for rel in identity_rels:
            effective_weight = self.calculate_decayed_weight(
                rel.get("base_weight", 5.0),
                rel.get("last_mentioned", ""),
                is_pinned,
            )
            
            # --- Skip decayed relationships ---
            # --- 跳过已衰减的关系 ---
            if not include_decayed and effective_weight <= self.min_weight:
                continue
            
            # --- Get target identity name ---
            # --- 获取目标身份名称 ---
            target_name = rel.get("target_id", "")
            target_identity = await self.get(rel.get("target_id"))
            if target_identity:
                target_name = target_identity.get("metadata", {}).get("name", rel.get("target_id"))
            
            result.append({
                "target_id": rel.get("target_id"),
                "target_name": target_name,
                "relation_type": rel.get("relation_type", ""),
                "base_weight": rel.get("base_weight", 5.0),
                "effective_weight": effective_weight,
                "last_mentioned": rel.get("last_mentioned", ""),
                "created": rel.get("created", ""),
                "mention_count": rel.get("mention_count", 0),
            })
        
        # --- Sort by effective_weight descending ---
        # --- 按有效权重降序排序 ---
        result.sort(key=lambda x: x["effective_weight"], reverse=True)
        return result

    async def touch_relation(self, from_id: str, to_id: str):
        """
        Touch a relationship (update last_mentioned timestamp).
        
        触碰关系（更新上次提及时间戳），用于被动触发时。
        
        Args:
            from_id: Source identity ID
            to_id: Target identity ID
        """
        rels = self._load_relationships()
        
        if from_id not in rels:
            return
        
        for rel in rels[from_id]:
            if rel.get("target_id") == to_id:
                rel["last_mentioned"] = now_iso()
                rel["mention_count"] = rel.get("mention_count", 0) + 1
                logger.debug(f"Touched relation / 触碰关系: {from_id} -> {to_id}")
                self._save_relationships(rels)
                return

    async def update_relation_weight(self, from_id: str, to_id: str, base_weight: float):
        """
        Update the base weight of a relationship.
        
        更新关系的基础权重。
        
        Args:
            from_id: Source identity ID
            to_id: Target identity ID
            base_weight: New base weight (1.0 ~ 10.0)
        """
        rels = self._load_relationships()
        
        if from_id not in rels:
            return False
        
        for rel in rels[from_id]:
            if rel.get("target_id") == to_id:
                rel["base_weight"] = base_weight
                rel["last_mentioned"] = now_iso()
                self._save_relationships(rels)
                logger.info(f"Updated relation weight / 更新关系权重: {from_id} -> {to_id} = {base_weight}")
                return True
        
        return False

    async def list_all(self) -> list[dict]:
        """
        List all identities.
        """
        identities = []
        if not os.path.exists(self.base_dir):
            return identities

        for filename in os.listdir(self.base_dir):
            if not filename.endswith(".md"):
                continue
            file_path = os.path.join(self.base_dir, filename)
            identity = self._load_identity(file_path)
            if identity:
                identities.append(identity)

        return identities

    async def add_relationship(self, identity_id: str, relationship: str) -> bool:
        """
        Add a relationship to an identity.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        relationships = post.get("relationships", [])
        if relationship not in relationships:
            relationships.append(relationship)
            post["relationships"] = relationships
            post["last_active"] = now_iso()
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            logger.info(f"Added relationship to {identity_id}: {relationship}")
        return True

    async def add_trait(self, identity_id: str, trait: str) -> bool:
        """
        Add a core trait to an identity.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        traits = post.get("core_traits", [])
        if trait not in traits:
            traits.append(trait)
            post["core_traits"] = traits
            post["last_active"] = now_iso()
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            logger.info(f"Added trait to {identity_id}: {trait}")
        return True

    async def add_basic_info(self, identity_id: str, key: str, value: str) -> bool:
        """
        Add or update a basic info field.
        """
        file_path = self._find_identity_file(identity_id)
        if not file_path:
            return False

        post = frontmatter.load(file_path)
        basic_info = post.get("basic_info", {})
        basic_info[key] = value
        post["basic_info"] = basic_info
        post["last_active"] = now_iso()
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))
        logger.info(f"Added basic info to {identity_id}: {key}={value}")
        return True

    def _find_identity_file(self, identity_id: str) -> Optional[str]:
        """
        Find identity file by ID.
        """
        if not identity_id:
            return None
        for filename in os.listdir(self.base_dir):
            if not filename.endswith(".md"):
                continue
            name_part = filename[:-3]
            if name_part == identity_id or name_part.endswith(f"_{identity_id}"):
                return os.path.join(self.base_dir, filename)
        return None

    def _load_identity(self, file_path: str) -> Optional[dict]:
        """
        Load identity data from file.
        """
        try:
            post = frontmatter.load(file_path)
            return {
                "id": post.get("id", os.path.splitext(os.path.basename(file_path))[0]),
                "metadata": dict(post.metadata),
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(f"Failed to load identity file / 加载身份文件失败: {file_path}: {e}")
            return None