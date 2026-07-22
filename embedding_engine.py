# ============================================================
# Module: Embedding Engine (embedding_engine.py)
# 模块：向量化引擎
#
# Generates embeddings via API,
# stores them in SQLite, and provides cosine similarity search.
# 通过 API 生成 embedding，
# 存储在 SQLite 中，提供余弦相似度搜索。
#
# Depended on by: server.py, bucket_manager.py
# 被谁依赖：server.py, bucket_manager.py
#
# NOTE: No local model loading (prevents OOM in 512MB containers).
# All embedding is done via HTTP API calls.
# ============================================================

import os
import json
import math
import sqlite3
import logging

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

logger = logging.getLogger("ombre_brain.embedding")


class EmbeddingEngine:
    """
    Embedding generation via API + SQLite vector storage + cosine search.
    No local model loading — all embedding is done via API to minimize memory usage.
    通过 API 生成向量 + SQLite 向量存储 + 余弦搜索。
    不加载本地模型，所有向量化通过 API 完成，以降低内存占用。
    """

    def __init__(self, config: dict):
        dehy_cfg = config.get("dehydration", {})
        embed_cfg = config.get("embedding", {})

        self.api_key = (embed_cfg.get("api_key") or dehy_cfg.get("api_key") or os.environ.get("OMBRE_API_KEY", "") or "").strip()
        self.base_url = (
            (embed_cfg.get("base_url") or "").strip()
            or (dehy_cfg.get("base_url") or "").strip()
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self.model = embed_cfg.get("model", "gemini-embedding-001")
        self.enabled = embed_cfg.get("enabled", True)

        # --- SQLite path: buckets_dir/embeddings.db ---
        db_path = os.path.join(config["buckets_dir"], "embeddings.db")
        self.db_path = db_path

        # --- Initialize API client only (no local model) ---
        self.client = None
        
        use_api = embed_cfg.get("use_api", True)
        
        if OPENAI_AVAILABLE and use_api and self.api_key and "deepseek" not in self.base_url.lower():
            try:
                self.client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=30.0,
                )
                logger.info(f"Embedding: Using API client with model {self.model}")
            except Exception as e:
                logger.warning(f"Failed to initialize API client: {e}")
        
        if not self.client:
            logger.warning("Embedding: No API client available, embedding disabled")
            self.enabled = False

        # --- Initialize SQLite ---
        self._init_db()

    # ---------------------------------------------------------
    # WAL-mode connection helper / WAL 模式连接帮助方法
    # ---------------------------------------------------------
    def _get_connection(self) -> sqlite3.Connection:
        """
        Open a SQLite connection with WAL mode enabled.
        WAL mode allows concurrent reads and writes without "database is locked" errors.

        以 WAL 模式打开 SQLite 连接，允许多个读写操作并发执行，避免"database is locked"错误。
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")  # 5s retry on busy
        return conn

    def _init_db(self):
        """Create embeddings table if not exists."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """
        Generate embedding for content and store in SQLite.
        为内容生成 embedding 并存入 SQLite。
        Returns True on success, False on failure.
        """
        if not self.enabled or not content or not content.strip():
            return False

        try:
            embedding = await self._generate_embedding(content)
            if not embedding:
                return False
            self._store_embedding(bucket_id, embedding)
            return True
        except Exception as e:
            logger.warning(f"Embedding generation failed for {bucket_id}: {e}")
            return False

    async def _generate_embedding(self, text: str) -> list[float]:
        """Call API to generate embedding vector. No local model fallback."""
        # Truncate to avoid token limits
        truncated = text[:2000]
        
        if self.client:
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=truncated,
                )
                if response.data and len(response.data) > 0:
                    return response.data[0].embedding
            except Exception as e:
                logger.warning(f"Embedding API call failed: {e}")
        
        return []

    def _store_embedding(self, bucket_id: str, embedding: list[float]):
        """Store embedding in SQLite."""
        from utils import now_iso
        conn = self._get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (bucket_id, embedding, updated_at) VALUES (?, ?, ?)",
            (bucket_id, json.dumps(embedding), now_iso()),
        )
        conn.commit()
        conn.close()

    def delete_embedding(self, bucket_id: str):
        """Remove embedding when bucket is deleted."""
        conn = self._get_connection()
        conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
        conn.commit()
        conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        """Retrieve stored embedding for a bucket. Returns None if not found."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT embedding FROM embeddings WHERE bucket_id = ?", (bucket_id,)
        ).fetchone()
        conn.close()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    async def compute_text_similarity(self, text_a: str, text_b: str) -> float:
        """
        Compute cosine similarity between two arbitrary texts.
        Does NOT store embeddings in the database.
        Returns 0.0 on failure.
        
        计算两段任意文本之间的余弦相似度。
        不会将 embedding 存入数据库。
        """
        if not self.enabled or not text_a or not text_b:
            return 0.0
        try:
            emb_a = await self._generate_embedding(text_a)
            emb_b = await self._generate_embedding(text_b)
            if not emb_a or not emb_b:
                return 0.0
            return self._cosine_similarity(emb_a, emb_b)
        except Exception as e:
            logger.warning(f"Text similarity computation failed: {e}")
            return 0.0

    async def search_similar(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        Search for buckets similar to query text.
        Returns list of (bucket_id, similarity_score) sorted by score desc.
        搜索与查询文本相似的桶。返回 (bucket_id, 相似度分数) 列表。
        """
        if not self.enabled:
            return []

        try:
            query_embedding = await self._generate_embedding(query)
            if not query_embedding:
                return []
        except Exception as e:
            logger.warning(f"Query embedding failed: {e}")
            return []

        # Load all embeddings from SQLite
        conn = self._get_connection()
        rows = conn.execute("SELECT bucket_id, embedding FROM embeddings").fetchall()
        conn.close()

        if not rows:
            return []

        # Calculate cosine similarity
        results = []
        for bucket_id, emb_json in rows:
            try:
                stored_embedding = json.loads(emb_json)
                sim = self._cosine_similarity(query_embedding, stored_embedding)
                results.append((bucket_id, sim))
            except (json.JSONDecodeError, Exception):
                continue

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
