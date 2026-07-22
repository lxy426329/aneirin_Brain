# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
import httpx
import datetime


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from housekeeper import Housekeeper
from embedding_engine import EmbeddingEngine
from emotion_manager import EmotionManager
from identity_manager import IdentityManager
from import_memory import ImportEngine
from pattern_manager import PatternManager
from tag_normalizer import TagNormalizer
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, detect_vulnerable_state

# --- Load .env file / 加载 .env 文件 ---
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
housekeeper = Housekeeper(config, bucket_mgr)         # Housekeeper / 记忆管家
identity_mgr = IdentityManager(config)               # Identity manager / 身份管理器
emotion_mgr = EmotionManager(config)                 # Emotion manager / 情绪管理器
pattern_mgr = PatternManager(config)                 # Pattern manager / 模式管理器
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
tag_normalizer = TagNormalizer(config, bucket_mgr, dehydrator)  # Tag normalizer / 标签归一化引擎

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
    # Disable DNS rebinding protection: this service is meant for remote access
    # via Cloudflare Tunnel / Render / ngrok, where the Host header is an
    # external domain that would never match the default localhost allowlist
    # (which returns 421 Misdirected Request). CORS is already open below.
    # 禁用 DNS rebinding 防护：本服务专供远程访问（Cloudflare Tunnel / Render / ngrok），
    # Host 头是外部域名，不可能匹配默认的 localhost 白名单（会返回 421）。
    # 下方已配置 CORS allow_origins=["*"]。
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception as e:
        logger.warning(f"Failed to load password hash: {e}")
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "tag_normalizer": "running" if tag_normalizer.is_running else "stopped",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)

        parts = []
        token_budget = 20000

        # --- Passive trigger: only load pinned/protected identities ---
        # --- 被动触发：只加载钉选/保护的名册（核心档案）---
        # Non-pinned identities are loaded only when mentioned in query (检索模式)
        # 未钉选的名册只在检索模式被提及时才注入
        pinned_identities = await identity_mgr.list_pinned()
        if pinned_identities:
            parts.append("=== 核心名册 ===")
            for ident in pinned_identities:
                meta = ident.get("metadata", {})
                name = meta.get("name", ident["id"])
                aliases = ", ".join(meta.get("aliases", []))
                traits = ", ".join(meta.get("core_traits", []))

                parts_ident = [f"[钉选] [{name}]"]
                if aliases:
                    parts_ident.append(f"别名: {aliases}")
                if traits:
                    parts_ident.append(f"特征: {traits}")

                content = strip_wikilinks(ident.get("content", ""))
                if content:
                    parts_ident.append(content)

                parts.append("\n".join(parts_ident))
                token_budget -= 500

        patterns = [b for b in all_buckets if b["metadata"].get("type") == "pattern"]
        if patterns:
            pattern_parts = []
            for b in patterns:
                # --- Cooldown check: skip or decay frequently injected patterns ---
                # --- 冷却检查：跳过或降权频繁注入的模式 ---
                in_cooldown, weight_factor = bucket_mgr.check_cooldown(b["id"])
                if in_cooldown and weight_factor < 0.3:
                    continue  # Too soon, skip entirely

                name = b["metadata"].get("name", b["id"])
                dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
                if dehydrated_summary:
                    summary = dehydrated_summary
                else:
                    summary = strip_wikilinks(b["content"])[:100]

                # Mark as cooldown-degraded if in cooldown
                cooldown_tag = " [冷却中]" if in_cooldown else ""
                pattern_parts.append(f"[{name}]{cooldown_tag}\n{summary}")
                bucket_mgr.record_injection(b["id"])
                token_budget -= 300

            if pattern_parts:
                parts.append("\n=== 模式 ===")
                parts.append("\n".join(pattern_parts))

        experiences = [b for b in all_buckets if b["metadata"].get("type") == "experience"]
        if experiences:
            exp_parts = []
            for exp in experiences:
                # --- Cooldown check: skip or decay frequently injected experiences ---
                # --- 冷却检查：跳过或降权频繁注入的年轮经验 ---
                in_cooldown, weight_factor = bucket_mgr.check_cooldown(exp["id"])
                if in_cooldown and weight_factor < 0.3:
                    continue  # Too soon, skip entirely

                name = exp["metadata"].get("name", exp["id"])
                exp_type = exp["metadata"].get("exp_type", "")
                apply_count = exp["metadata"].get("apply_count", 0)
                entry = f"[{name}]"
                if exp_type:
                    entry += f" #{exp_type}"
                if apply_count > 0:
                    entry += f" (应用{apply_count}次)"
                if in_cooldown:
                    entry += " [冷却中]"
                content = strip_wikilinks(exp.get("content", ""))
                if content:
                    entry += f"\n{content}"
                exp_parts.append(entry)
                bucket_mgr.record_injection(exp["id"])
                token_budget -= 300

            if exp_parts:
                parts.append("\n=== 年轮(经验) ===")
                parts.append("\n".join(exp_parts))

        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        if pinned:
            parts.append("\n=== 核心准则 ===")
            for b in pinned:
                dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
                if dehydrated_summary:
                    summary = dehydrated_summary
                else:
                    summary = strip_wikilinks(b["content"])[:100]
                parts.append(f"[核心准则] {summary}")
                token_budget -= 300

        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel", "identity", "pattern", "experience", "candlestick")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)
        
        if scored:
            parts.append("\n=== 动态记忆 ===")
            candidates = list(scored)
            if len(candidates) > 1:
                top1 = [candidates[0]]
                pool = candidates[1:min(20, len(candidates))]
                random.shuffle(pool)
                candidates = top1 + pool + candidates[min(20, len(candidates)):]
            candidates = candidates[:10]
            
            for b in candidates:
                if token_budget <= 0:
                    break
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                decay_stage = b["metadata"].get("decay_stage", 1)
                name = b["metadata"].get("name", b["id"])
                domains = ", ".join(b["metadata"].get("domain", []))
                
                try:
                    arousal = float(b["metadata"].get("arousal", 0.3))
                except (ValueError, TypeError):
                    arousal = 0.3
                
                content = strip_wikilinks(b["content"])
                related_identities = []
                for ident_name, ident in identity_names.items():
                    if ident_name in content and ident_name not in related_identities:
                        related_identities.append(ident)
                
                if decay_stage == 3:
                    parts.append(f"[已消化] [{name}] [主题:{domains}] - 知识已内化")
                    token_budget -= 30
                    continue
                
                if arousal > 0.7:
                    emotion_str = ", ".join(f"{e['label']}({e['intensity']:.1f})" for e in b["metadata"].get("emotions", []))
                    entry = f"[高情绪] [{name}] [主题:{domains}] [情感:{emotion_str}]\n{content}"
                else:
                    dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
                    if dehydrated_summary:
                        summary = dehydrated_summary
                    else:
                        summary = content[:100]
                    if decay_stage == 2:
                        entry = f"[总结] [{name}] [主题:{domains}]\n{summary}"
                    else:
                        entry = f"[{name}] [主题:{domains}]\n{summary}"
                
                if related_identities:
                    entry += "\n[关联人物]:"
                    for ident in related_identities:
                        ident_name = ident["metadata"].get("name", "")
                        ident_traits = ", ".join(ident["metadata"].get("core_traits", []))
                        ident_basic = ident["metadata"].get("basic_info", {})
                        ident_info = []
                        if ident_traits:
                            ident_info.append(f"特征: {ident_traits}")
                        if ident_basic.get("age"):
                            ident_info.append(f"年龄: {ident_basic['age']}")
                        if ident_basic.get("职业"):
                            ident_info.append(f"职业: {ident_basic['职业']}")
                        if ident_info:
                            entry += f"\n  - {ident_name} ({', '.join(ident_info)})"
                        else:
                            entry += f"\n  - {ident_name}"
                    token_budget -= 100
                
                parts.append(entry)
                token_budget -= 200

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


def _extract_event_context(content: str) -> str:
    """
    Extract event context from content: time, location, state, and events.
    从内容中提取事件背景：时间、地点、状态、发生的事件。
    
    Returns a formatted context string like:
    "事件背景：顾尘今日身体不适去医院/身体隐痛"
    """
    if not content or not content.strip():
        return ""
    
    text = content.strip()
    context_parts = []
    
    import re
    
    date_patterns = [
        r'(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日号]?)',
        r'(\d{4}[-]\d{2}[-]\d{2})',
        r'(今天|今日|昨天|昨日|前天|明日|明天)',
        r'(早晨|早上|上午|中午|下午|晚上|深夜)',
    ]
    for pattern in date_patterns:
        matches = re.findall(pattern, text)
        if matches:
            context_parts.append("时间: " + matches[0])
            break
    
    location_patterns = [
        r'(在\s*[\u4e00-\u9fff]+)',
        r'(从\s*[\u4e00-\u9fff]+到[\u4e00-\u9fff]+)',
        r'(去\s*[\u4e00-\u9fff]+)',
        r'(医院|公司|家|学校|办公室)',
    ]
    for pattern in location_patterns:
        matches = re.findall(pattern, text)
        if matches:
            context_parts.append("地点: " + matches[0])
            break
    
    state_patterns = [
        r'(身体不适|生病|感冒|发烧|头痛|胃痛|疲劳|疲惫|累)',
        r'(开心|难过|生气|焦虑|烦躁|郁闷|沮丧|兴奋)',
        r'(加班|工作|学习|开会|休息|睡觉)',
    ]
    for pattern in state_patterns:
        matches = re.findall(pattern, text)
        if matches:
            context_parts.append("状态: " + matches[0])
            break
    
    event_patterns = [
        r'(发生了|遇到了|经历了|做了)\s*([^\。！？]+)',
        r'(去\s*[\u4e00-\u9fff]+\s*[做办]了\s*[^\。！？]+)',
        r'(和\s*[\u4e00-\u9fff]+[^\。！？]+)',
    ]
    for pattern in event_patterns:
        matches = re.findall(pattern, text)
        if matches:
            if isinstance(matches[0], tuple):
                context_parts.append("事件: " + matches[0][0] + matches[0][1])
            else:
                context_parts.append("事件: " + matches[0])
            break
    
    if context_parts:
        return "事件背景：" + "；".join(context_parts)
    return ""


def _detect_noise_content(content: str) -> bool:
    """
    Detect low-value "noise" content that should have short TTL.
    检测低价值"噪音"内容，应设置短期 TTL。
    
    Returns True if content is noise.
    """
    if not content or not content.strip():
        return True
    
    text = content.strip()
    
    noise_patterns = [
        r'^(我去|我要|我先|我得)\s*(洗|吃|喝|睡|走|离开|忙|做事|干活)\s*[了吗呢]?$',
        r'^(好|行|可以|没问题|知道了|明白了|收到)$',
        r'^(等一下|等会儿|一会儿|马上)$',
        r'^(拜拜|再见|晚安)$',
        r'^(嗯嗯|啊啊|哦哦|呵呵|哈哈)$',
        r'^(在吗|在不在|有人吗)$',
        r'^[^\u4e00-\u9fff]{0,5}$',
    ]
    
    import re
    for pattern in noise_patterns:
        if re.match(pattern, text):
            return True
    
    if len(text) <= 5 and not re.search(r'[\u4e00-\u9fff]{2,}', text):
        return True
    
    return False


def _extract_status_key(content: str) -> str | None:
    """
    Extract status key for state override mechanism.
    提取状态键，用于状态覆盖机制。
    
    Returns a normalized status key or None.
    """
    if not content or not content.strip():
        return None
    
    text = content.strip()
    
    status_patterns = [
        r'(肚子痛|胃痛|腹痛|痛经)',
        r'(头痛|头晕|发烧|感冒|咳嗽)',
        r'(身体不适|不舒服|难受)',
        r'(病好了|不痛了|恢复了|痊愈了)',
        r'(开心|高兴|愉快)',
        r'(难过|伤心|沮丧)',
        r'(生气|愤怒|烦躁)',
        r'(疲劳|疲惫|累)',
        r'(加班|工作中|学习中)',
        r'(休息|睡觉|放假)',
    ]
    
    import re
    for pattern in status_patterns:
        matches = re.findall(pattern, text)
        if matches:
            return matches[0]
    
    return None


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float = None,
    arousal: float = None,
    emotions: list = None,
    dominant_emotion: str = "",
    emotion_metrics: dict = None,
    name: str = "",
    task_flag: bool = False,
    dehydrator=None,
    context_metadata: dict = None,
    ttl: int = None,
    status_key: str = None,
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    """
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)

                update_kwargs = {
                    "content": merged,
                    "tags": list(set(bucket["metadata"].get("tags", []) + tags)),
                    "importance": max(bucket["metadata"].get("importance", 5), importance),
                    "domain": list(set(bucket["metadata"].get("domain", []) + domain)),
                }

                # --- Propagate task_flag on merge ---
                # --- 合并时传递 task_flag ---
                if task_flag:
                    update_kwargs["task_flag"] = True

                if emotions:
                    update_kwargs["emotions"] = emotions
                    if dominant_emotion:
                        update_kwargs["dominant_emotion"] = dominant_emotion
                    if emotion_metrics:
                        update_kwargs["emotion_metrics"] = emotion_metrics
                elif valence is not None or arousal is not None:
                    update_kwargs["valence"] = valence if valence is not None else 0.5
                    update_kwargs["arousal"] = arousal if arousal is not None else 0.3

                await bucket_mgr.update(bucket["id"], **update_kwargs)

                try:
                    await embedding_engine.generate_and_store(bucket["id"], merged)
                except Exception as e:
                    logger.warning(f"Failed to store embedding after merge: {e}")

                # Generate one-line summary asynchronously after merge
                asyncio.create_task(_generate_one_line_summary_async(bucket["id"], merged))

                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    create_kwargs = {
        "content": content,
        "tags": tags,
        "importance": importance,
        "domain": domain,
        "name": name or None,
        "task_flag": task_flag,
        "dehydrator": dehydrator,
        "context_metadata": context_metadata,
        "ttl": ttl,
        "status_key": status_key,
    }

    if emotions:
        create_kwargs["emotions"] = emotions
        if dominant_emotion:
            create_kwargs["dominant_emotion"] = dominant_emotion
        if emotion_metrics:
            create_kwargs["emotion_metrics"] = emotion_metrics
    elif valence is not None or arousal is not None:
        create_kwargs["valence"] = valence if valence is not None else 0.5
        create_kwargs["arousal"] = arousal if arousal is not None else 0.3

    bucket_id = await bucket_mgr.create(**create_kwargs)

    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception as e:
        logger.warning(f"Failed to store embedding: {e}")

    # Generate one-line summary asynchronously after creation
    asyncio.create_task(_generate_one_line_summary_async(bucket_id, content))

    return bucket_id, False


# =============================================================
# Breath helper functions
# breath 辅助函数
# =============================================================
async def _breath_identity(max_tokens: int) -> str:
    """Load all identities for breath."""
    try:
        identities = await identity_mgr.list_all()
        if not identities:
            return "暂无身份档案。"
        
        results = []
        token_used = 0
        for ident in identities:
            if token_used >= max_tokens:
                break
            meta = ident.get("metadata", {})
            name = meta.get("name", ident["id"])
            aliases = ", ".join(meta.get("aliases", []))
            traits = ", ".join(meta.get("core_traits", []))
            
            parts = []
            parts.append(f"📋 [{name}]")
            if aliases:
                parts.append(f"别名: {aliases}")
            if traits:
                parts.append(f"特征: {traits}")
            if ident.get("content"):
                content_preview = strip_wikilinks(ident["content"])[:500]
                parts.append(f"描述: {content_preview}")
            
            entry = "\n".join(parts)
            t = count_tokens_approx(entry)
            if token_used + t > max_tokens:
                break
            results.append(entry)
            token_used += t
        
        return "=== 身份档案 ===\n" + "\n---\n".join(results)
    except Exception as e:
        logger.error(f"_breath_identity failed: {e}")
        return "读取身份档案失败。"


async def _breath_feel(max_tokens: int) -> str:
    """Load all feels with event context."""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
        feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        if not feels:
            return "没有留下过 feel。"
        
        results = []
        for f in feels:
            meta = f["metadata"]
            created = meta.get("created", "")
            context_metadata = meta.get("context_metadata", {})
            event_context = context_metadata.get("event_context", "")
            
            content = strip_wikilinks(f["content"])
            
            if event_context:
                entry = f"[{created}] [bucket_id:{f['id']}]\n{event_context}\n{content}"
            else:
                entry = f"[{created}] [bucket_id:{f['id']}]\n{content}"
            
            results.append(entry)
            if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                break
        
        return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
    except Exception as e:
        logger.error(f"_breath_feel failed: {e}")
        return "读取 feel 失败。"


async def _generate_summary_report(summarized_buckets: list, query: str = "") -> str:
    """Generate a summary report for buckets that weren't fully displayed."""
    if not summarized_buckets:
        return ""
    
    domain_counts = {}
    emotion_stats = {"high": 0, "medium": 0, "low": 0}
    recent_count = 0
    important_count = 0
    
    for b in summarized_buckets:
        meta = b["metadata"]
        
        domains = meta.get("domain", []) or []
        for d in domains:
            domain_counts[d] = domain_counts.get(d, 0) + 1
        
        arousal = float(meta.get("arousal", 0.3))
        valence = float(meta.get("valence", 0.5))
        intensity = arousal * (1.0 + abs(valence - 0.5))
        if intensity > 0.7:
            emotion_stats["high"] += 1
        elif intensity > 0.4:
            emotion_stats["medium"] += 1
        else:
            emotion_stats["low"] += 1
        
        importance = int(meta.get("importance", 5))
        if importance >= 8:
            important_count += 1
        
        try:
            created = meta.get("created", "")
            if created:
                created_time = datetime.datetime.fromisoformat(str(created))
                days = (datetime.datetime.now() - created_time).days
                if days <= 3:
                    recent_count += 1
        except (ValueError, TypeError) as e:
            logger.debug(f"Failed to parse created time: {e}")
    
    lines = []
    
    if domain_counts:
        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        domain_str = ", ".join([f"{d}({c})" for d, c in top_domains])
        lines.append(f"📁 主题分布: {domain_str}")
    
    if emotion_stats["high"] > 0 or emotion_stats["medium"] > 0:
        lines.append(f"❤️ 情绪强度: 高({emotion_stats['high']}) 中({emotion_stats['medium']}) 低({emotion_stats['low']})")
    
    if recent_count > 0:
        lines.append(f"⏰ 近期记忆: {recent_count}条(3天内)")
    
    if important_count > 0:
        lines.append(f"⭐ 重要记忆: {important_count}条(重要度≥8)")
    
    if query:
        lines.append(f"🔍 搜索词: {query}")
    
    return "\n".join(lines)


async def _breath_surfacing(
    max_tokens: int,
    max_results: int,
    brief: bool,
    type_filter: str = None,
    summary_report: bool = True,
    mask_tasks: bool = False,
    inject_candlestick_flavor: bool = False,
) -> str:
    """Surfacing mode — passive identity injection (pinned only).

    浮现模式 —— 名册被动触发注入（仅钉选）。

    Non-pinned identities are NOT loaded in surfacing mode.
    They are only injected when explicitly mentioned in query (检索模式).
    未钉选的名册不会在浮现模式加载，只在检索模式被提及时才注入。

    Args:
        mask_tasks: If True, filter out task_flag=True buckets.
                    Set when user is in a vulnerable state.
                    当用户处于脆弱状态时设为 True，屏蔽任务类桶。
        inject_candlestick_flavor: If True, inject candlesticks as low-priority
                                   flavor for casual chat, but ONLY if no strong
                                   anchors triggered.
                                   如果为 True，将烛台作为低优先级调味料注入，
                                   但仅在没有强锚点触发时才注入。
    """
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
        return "记忆系统暂时无法访问。"

    # --- Mask task_flag buckets if requested (vulnerable state) ---
    # --- 屏蔽任务类桶（脆弱状态：生病/疲惫/情绪化）---
    if mask_tasks:
        all_buckets = bucket_mgr._mask_task_buckets(all_buckets)

    parts = []

    if not type_filter or type_filter == "identity":
        try:
            # --- Passive trigger: only load pinned identities in surfacing mode ---
            # --- 被动触发：浮现模式只加载钉选/保护的名册 ---
            pinned_identities = await identity_mgr.list_pinned()
            if pinned_identities:
                ident_results = []
                for ident in pinned_identities:
                    meta = ident.get("metadata", {})
                    name = meta.get("name", ident["id"])
                    aliases = ", ".join(meta.get("aliases", []))
                    traits = ", ".join(meta.get("core_traits", []))
                    identity_id = meta.get("id", "")
                    
                    parts_ident = [f"📋 [{name}]"]
                    if aliases:
                        parts_ident.append(f"别名: {aliases}")
                    if traits:
                        parts_ident.append(f"特征: {traits}")
                    
                    # --- Add relationship weight information ---
                    # --- 添加关系权重信息 ---
                    try:
                        relations = await identity_mgr.get_relations(identity_id)
                        if relations:
                            warm_relations = []
                            for rel in relations:
                                weight = rel["effective_weight"]
                                # --- Weight tier: high/middle/low ---
                                # --- 权重等级：高/中/低 ---
                                if weight >= 3.0:
                                    tier = "高"
                                elif weight >= 1.0:
                                    tier = "中"
                                else:
                                    tier = "低"
                                warm_relations.append(f"{rel['target_name']}({rel['relation_type']}):{tier}")
                            if warm_relations:
                                parts_ident.append(f"关系热度: {', '.join(warm_relations)}")
                    except Exception as e:
                        logger.debug(f"Failed to load relations for {identity_id}: {e}")
                    
                    ident_results.append("\n".join(parts_ident))
                if ident_results:
                    parts.append("=== 核心名册 ===\n" + "\n---\n".join(ident_results))
        except Exception as e:
            logger.warning(f"Failed to load pinned identities: {e}")

    pinned_buckets = [
        b for b in all_buckets
        if b["metadata"].get("pinned") or b["metadata"].get("protected")
    ]
    pinned_results = []
    for b in pinned_buckets:
        if type_filter and b["metadata"].get("type") != type_filter:
            continue
        try:
            dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
            if dehydrated_summary:
                summary = dehydrated_summary
            else:
                summary = strip_wikilinks(b["content"])[:100]
            pinned_results.append(f"📌 [核心准则] [bucket_id:{b['id']}] {summary}")
        except Exception as e:
            logger.warning(f"Failed to process pinned bucket / 钉选桶处理失败: {e}")
            continue

    priority_buckets = [
        b for b in all_buckets
        if b["metadata"].get("type") in ("experience", "permanent")
        and not b["metadata"].get("resolved", False)
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]
    
    unresolved = [
        b for b in all_buckets
        if not b["metadata"].get("resolved", False)
        and b["metadata"].get("type") not in ("permanent", "feel", "identity", "experience")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]
    
    if type_filter:
        unresolved = [b for b in unresolved if b["metadata"].get("type") == type_filter]

    logger.info(
        f"Breath surfacing: {len(all_buckets)} total, "
        f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
    )

    scored = sorted(
        unresolved,
        key=lambda b: decay_engine.calculate_score(b["metadata"]),
        reverse=True,
    )

    if scored:
        top_scores = [(b["metadata"].get("name", b["id"]), decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
        logger.info(f"Top unresolved scores: {top_scores}")

    cold_start = [
        b for b in unresolved
        if int(b["metadata"].get("activation_count", 0)) == 0
        and int(b["metadata"].get("importance", 0)) >= 8
    ][:2]
    cold_start_ids = {b["id"] for b in cold_start}
    scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
    
    priority_scored = sorted(
        priority_buckets,
        key=lambda b: decay_engine.calculate_score(b["metadata"]),
        reverse=True,
    )
    
    scored_with_cold = cold_start + priority_scored + scored_deduped

    token_budget = max_tokens
    for p in parts:
        token_budget -= count_tokens_approx(p)
    for r in pinned_results:
        token_budget -= count_tokens_approx(r)

    candidates = list(scored_with_cold)
    if len(candidates) > 1:
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        if len(non_cold) > 1:
            top1 = [non_cold[0]]
            pool = non_cold[1:min(20, len(non_cold))]
            random.shuffle(pool)
            non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
        candidates = cold_start + non_cold
    candidates = candidates[:max_results * 2]

    dynamic_results = []
    summarized_buckets = []
    shown_count = 0
    
    for b in candidates:
        if shown_count >= max_results or token_budget <= 0:
            summarized_buckets.append(b)
            continue
        try:
            clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
            decay_stage = b["metadata"].get("decay_stage", 1)
            
            score = decay_engine.calculate_score(b["metadata"])
            
            if decay_stage == 3:
                summary = f"[已消化] {b['metadata'].get('name', '')} - 知识已内化"
                summary_tokens = count_tokens_approx(summary)
                if summary_tokens > token_budget:
                    summarized_buckets.append(b)
                    continue
                dynamic_results.append(f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}")
                token_budget -= summary_tokens
                shown_count += 1
                continue
            
            one_line_summary = b["metadata"].get("one_line_summary", "")
            dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
            
            if score >= 7.0:
                if dehydrated_summary:
                    if decay_stage == 2:
                        summary = f"[总结] {dehydrated_summary}"
                    else:
                        summary = dehydrated_summary
                else:
                    summary = b["metadata"].get("name", "")[:50]
            else:
                if one_line_summary:
                    summary = f"[摘要] {b['metadata'].get('name', '')} - {one_line_summary}"
                elif dehydrated_summary:
                    summary = f"[摘要] {dehydrated_summary[:50]}"
                else:
                    summary = b["metadata"].get("name", "")[:30]
            
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                summarized_buckets.append(b)
                continue
            dynamic_results.append(f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}")
            token_budget -= summary_tokens
            shown_count += 1
        except Exception as e:
            logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
            continue

    if pinned_results:
        parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
    if dynamic_results:
        parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))

    if not type_filter:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            # --- Apply task_flag masking for experience section too ---
            # --- 年轮经验部分也应用 task_flag 屏蔽 ---
            if mask_tasks:
                all_buckets = bucket_mgr._mask_task_buckets(all_buckets)
            experiences = [b for b in all_buckets
                           if b.get("metadata", {}).get("domain") and "经验" in b.get("metadata", {}).get("domain")]
            if experiences:
                recent_experiences = sorted(experiences, key=lambda e: e["metadata"].get("created", ""), reverse=True)[:5]
                experience_results = []
                for exp in recent_experiences:
                    # --- Cooldown check: skip frequently injected experiences ---
                    # --- 冷却检查：跳过频繁注入的年轮经验 ---
                    in_cooldown, weight_factor = bucket_mgr.check_cooldown(exp["id"])
                    if in_cooldown and weight_factor < 0.3:
                        continue

                    meta = exp.get("metadata", {})
                    name = meta.get("name", exp["id"])
                    content = exp.get("content", "")
                    exp_type = meta.get("exp_type", "")
                    apply_count = meta.get("apply_count", 0)
                    entry = f"🌳 [{name}]"
                    if exp_type:
                        entry += f" #{exp_type}"
                    if apply_count > 0:
                        entry += f" (应用{apply_count}次)"
                    if in_cooldown:
                        entry += " [冷却中]"
                    entry += f"\n  {content}"
                    experience_results.append(entry)
                    bucket_mgr.record_injection(exp["id"])
                if experience_results:
                    parts.append("=== 年轮经验 ===\n" + "\n---\n".join(experience_results))
        except Exception as e:
            logger.warning(f"Failed to load experiences: {e}")

    if summary_report and summarized_buckets:
        summary_section = await _generate_summary_report(summarized_buckets)
        if summary_section:
            parts.append(f"\n📋 记忆速览（共{len(summarized_buckets)}条未完全展示）:\n{summary_section}")

    # --- Low-priority candlestick flavor injection ---
    # --- 低优先级烛台调味料注入（仅闲聊时，无强锚点触发）---
    # Candlesticks are pure emotional/non-task data, never participate in
    # forced behavior rule constraints. Only inject when chatting casually
    # and no strong anchors triggered, as "flavor" for tone expression.
    # 烛台是纯感性/非任务数据，绝不参与强制的行为规则约束。
    # 只有在闲聊且没有强锚点触发时，做极低权重的随机采样注入，作为语气表达的"调味料"。
    if inject_candlestick_flavor:
        # --- Only inject if no strong anchors triggered ---
        # --- 只有当没有强锚点触发时才注入 ---
        has_strong_anchors = bool(pinned_results) or bool(dynamic_results and any("高情绪" in r or "核心准则" in r for r in parts))
        
        if not has_strong_anchors:
            try:
                flavor_candles = await bucket_mgr.retrieve_candlesticks_for_flavor(
                    query="",
                    max_count=2,
                    random_probability=0.3,
                )
                if flavor_candles:
                    flavor_parts = []
                    for candle in flavor_candles:
                        title = candle.get("title", "")
                        content = candle.get("content", "")
                        created = candle.get("created", "")[:10] if candle.get("created") else ""
                        entry = f"🕯️ [{title or '感想'}]"
                        if created:
                            entry += f" [{created}]"
                        if content:
                            entry += f"\n  {content[:80]}"
                        flavor_parts.append(entry)
                    if flavor_parts:
                        parts.append("\n=== 语气调味（烛台）===\n" + "\n---\n".join(flavor_parts))
            except Exception as e:
                logger.warning(f"Candlestick flavor injection failed / 烛台调味料注入失败: {e}")

    if not parts:
        return "权重池平静，没有需要处理的记忆。"

    return "\n".join(parts)


# --- Casual chat keywords (for candlestick flavor injection) ---
# --- 闲聊关键词（用于烛台调味料注入判断）---
_CASUAL_CHAT_KEYWORDS = [
    # Greetings / 问候
    "你好", "嗨", "哈喽", "hi", "hello", "嘿", "嘿呀",
    # Casual questions / 随意提问
    "在吗", "在干嘛", "忙吗", "有空吗",
    # Emotion sharing / 情绪分享
    "心情", "开心", "难过", "无聊", "郁闷", "烦躁",
    # Small talk / 闲聊
    "今天", "天气", "吃饭", "吃了吗", "睡了吗", "早安", "晚安",
    # Conversation starters / 开启话题
    "聊聊", "说说", "谈谈",
    # Casual expressions / 随意表达
    "哦", "嗯", "啊", "哦~", "好吧", "好的",
]

# --- Task-oriented keywords (exclude from casual chat) ---
# --- 任务导向关键词（排除闲聊）---
_TASK_KEYWORDS = [
    "任务", "工作", "项目", "报告", "文档", "代码",
    "完成", "提交", "进度", "deadline", "截止",
    "需要", "应该", "必须", "要做", "得做",
    "计划", "方案", "策略", "安排",
]

# --- Task query keywords (for bypassing task masking) ---
# --- 任务查询关键词（用于绕过任务屏蔽）---
_TASK_QUERY_KEYWORDS = [
    "任务", "工作", "待办", "todo", "待完成",
    "进度", "deadline", "截止", "计划",
    "完成", "未完成", "还需要", "还要做",
    "项目", "报告", "文档",
]


def is_task_query(text: str) -> bool:
    """
    [Legacy] Keyword-based task query detection.
    Kept for fallback if embedding engine is disabled.
    
    基于关键词的任务查询检测（旧版）。
    当 embedding 引擎不可用时作为回退方案。
    """
    if not text or not text.strip():
        return False
    text_lower = text.lower()
    for kw in _TASK_QUERY_KEYWORDS:
        if kw in text_lower:
            return True
    return False


_TASK_CONCEPT_TEXT = (
    "任务 工作 待办事项 需要完成的事情 未完成的工作 "
    "todo tasks work items pending jobs unfinished business "
    "我还有什么没做的 有哪些任务 工作进度"
)


async def _detect_active_task_query(query: str) -> bool:
    """
    Detect if user is actively asking about tasks using vector similarity.
    Returns True if query is semantically similar to task/todo concept.
    
    使用向量相似度检测用户是否主动询问任务。
    如果查询与任务概念语义相似则返回 True。
    
    Uses embedding engine for semantic comparison:
    - similarity >= 0.55 → active task query (bypass masking)
    - similarity < 0.55 → not a task query
    - embedding disabled → fall back to keyword matching
    """
    if not query or not query.strip():
        return False
    
    try:
        sim = await embedding_engine.compute_text_similarity(query, _TASK_CONCEPT_TEXT)
        if sim >= 0.55:
            logger.info(
                f"Vector task query detected (sim={sim:.3f}) / "
                f"向量任务查询检测到: query={query[:30]}"
            )
            return True
        return False
    except Exception as e:
        logger.warning(f"Vector task query detection failed, falling back to keywords: {e}")
        return is_task_query(query)


_SICK_TAGS = ["生病", "发烧", "感冒", "头痛", "胃痛", "不舒服", "sick", "fever", "pain"]


async def _detect_vulnerable_from_feels() -> dict:
    """
    Detect vulnerable state from latest feel buckets (no-query mode).
    Reads the most recent feel bucket's emotion coordinates and tags.
    
    从最近的 feel 记忆桶检测脆弱状态（无 query 模式）。
    读取最新 feel 桶的情绪坐标和标签。
    
    Returns:
        {"is_vulnerable": bool, "state": str, "reason": str}
    """
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
        if not feels:
            return {"is_vulnerable": False, "state": "normal", "reason": "no feels available"}
        
        feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        latest = feels[0]
        meta = latest["metadata"]
        
        valence = meta.get("valence", 0.5)
        arousal = meta.get("arousal", 0.5)
        tags = [t.lower() for t in meta.get("tags", [])]
        content = (latest.get("content", "") or "").lower()
        
        # Check 1: emotional valence < 0.4 AND low arousal < 0.4 (depressed/exhausted)
        if valence < 0.4 and arousal < 0.4:
            return {
                "is_vulnerable": True,
                "state": "emotional",
                "reason": f"latest feel shows low valence({valence:.2f}) and low arousal({arousal:.2f})",
            }
        
        # Check 2: sick/tired tags in feel
        for tag in tags:
            if tag in _SICK_TAGS:
                return {
                    "is_vulnerable": True,
                    "state": "sick",
                    "reason": f"latest feel has sick tag: {tag}",
                }
        
        # Check 3: sick/tired keywords in feel content
        sick_kws = ["生病", "发烧", "感冒", "头痛", "sick", "fever", "痛", "不舒服", "累", "疲惫", "tired", "exhausted"]
        for kw in sick_kws:
            if kw in content:
                return {
                    "is_vulnerable": True,
                    "state": "sick",
                    "reason": f"latest feel content mentions: {kw}",
                }
        
        return {"is_vulnerable": False, "state": "normal", "reason": "feel indicates normal state"}
    except Exception as e:
        logger.warning(f"Feel-based vulnerability detection failed: {e}")
        return {"is_vulnerable": False, "state": "normal", "reason": f"detection error: {e}"}


def is_casual_chat(text: str) -> bool:
    """
    Detect if the user's message is casual chat.
    Used to decide whether to inject candlestick flavor.
    
    检测用户消息是否为闲聊。
    用于决定是否注入烛台调味料。
    
    Rules:
    - Short message (≤ 15 chars) → likely casual
    - Contains casual chat keywords → casual
    - Contains task keywords → NOT casual
    - No query at all (empty) → casual (default surfacing)
    
    规则：
    - 短消息（≤15字符）→ 很可能是闲聊
    - 包含闲聊关键词 → 闲聊
    - 包含任务关键词 → 非闲聊
    - 无 query（空）→ 闲聊（默认浮现模式）
    """
    if not text or not text.strip():
        return True
    
    text_lower = text.lower()
    text_stripped = text.strip()
    
    # --- Task keywords take priority: if any, NOT casual ---
    # --- 任务关键词优先级最高：如果有，不是闲聊 ---
    for kw in _TASK_KEYWORDS:
        if kw in text_lower:
            return False
    
    # --- Short message → likely casual ---
    # --- 短消息 → 很可能是闲聊 ---
    if len(text_stripped) <= 15:
        return True
    
    # --- Check casual chat keywords ---
    # --- 检查闲聊关键词 ---
    for kw in _CASUAL_CHAT_KEYWORDS:
        if kw in text_lower:
            return True
    
    return False


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(
    query: str = "",
    max_tokens: int = 5000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 10,
    importance_min: int = -1,
    brief: bool = True,
    type: str = "",
    summary_report: bool = True,
    force_keyword: bool = False,
) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。max_tokens控制返回总token上限(默认5000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认10,最大50)。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。brief控制返回格式: true=简洁格式(仅元数据头+summary), false=完整格式(含core_facts/todos/keywords)。无参数浮现时brief默认true,有关键词检索时brief默认false。type参数按层过滤: identity/pattern/event/feel, 不传则全层返回。summary_report=true时对未完全展示的记忆生成快速总结报告。force_keyword=True强制使用精确关键字匹配模式。"""
    await decay_engine.ensure_started()
    await housekeeper.ensure_started()
    
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # --- Housekeeper prefetch: pull event chain drafts and cleanup proposals ---
    # --- 管家预取：拉取事件链草案和清理提案 ---
    housekeeper_prefetch = ""
    try:
        proposals = await housekeeper.get_cleanup_proposals(status="pending")
        if proposals != "暂无待清理提案。":
            housekeeper_prefetch = f"\n管家提案:\n{proposals[:500]}"
    except Exception as e:
        logger.warning(f"Housekeeper prefetch failed: {e}")

    # --- Vulnerable state detection (auto-mask task_flag buckets) ---
    # --- 脆弱状态检测（自动屏蔽 task_flag 桶，防止 KPI 机器式催任务）---
    #
    # Strategy:
    # - No query (passive push): detect vulnerable state from latest feel
    #   emotion coordinates (valence < 0.4 & arousal < 0.4 → depressed;
    #   sick/tired tags → sick). These are persistent state signals, not
    #   single-message keyword matches.
    # - With query (active retrieval): use vector similarity between query
    #   and task concept text to determine if user is actively asking about
    #   tasks. Bypass masking if sim >= 0.55.
    #
    # 策略：
    # - 无 query（被动浮现）：从最新 feel 的效价/唤醒度坐标检测脆弱状态
    #   （valence < 0.4 & arousal < 0.4 → 抑郁；病痛标签 → 生病）
    # - 有 query（主动检索）：计算 query 与任务概念向量的相似度。
    #   相似度 >= 0.55 认定为主动询问任务，绕过屏蔽。
    if query and query.strip():
        # Active retrieval mode: use vector similarity
        # 主动检索模式：使用向量相似度
        is_task_query_flag = await _detect_active_task_query(query)
        vulnerable = detect_vulnerable_state(query) if query else None
    else:
        # Passive push mode: detect from emotion state
        # 被动浮现模式：从情绪状态检测
        is_task_query_flag = False
        state = await _detect_vulnerable_from_feels()
        vulnerable = {
            "is_vulnerable": state["is_vulnerable"],
            "state": state["state"],
            "matched_keywords": [],
            "reason": state["reason"],
        } if state["is_vulnerable"] else None
    
    # Task masking logic:
    # 1. Only mask if vulnerable state detected
    # 2. NEVER mask if user actively asks about tasks (vector-based detection)
    # 3. Passive push always uses feel-based detection
    mask_tasks = bool(vulnerable and vulnerable.get("is_vulnerable")) and not is_task_query_flag
    
    if mask_tasks:
        logger.info(
            f"Breath: vulnerable state detected, task_flag buckets will be masked / "
            f"检测到脆弱状态: state={vulnerable['state']}, "
            f"reason={vulnerable['reason']}"
        )
    elif is_task_query_flag:
        logger.info(
            f"Breath: active task query detected, bypassing task masking / "
            f"检测到主动任务查询，绕过任务屏蔽: query={query[:30] if query else '(passive)'}"
        )

    type_filter = type.strip().lower() if type else None

    if query and query.strip():
        brief = False

    if type_filter == "identity":
        return await _breath_identity(max_tokens)

    if type_filter == "feel":
        return await _breath_feel(max_tokens)

    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        # --- Apply task_flag masking in importance_min mode too ---
        # --- importance_min 模式下同样应用 task_flag 屏蔽 ---
        if mask_tasks:
            all_buckets = bucket_mgr._mask_task_buckets(all_buckets)
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
        ]
        if type_filter:
            filtered = [b for b in filtered if b["metadata"].get("type") == type_filter]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
                if dehydrated_summary:
                    summary = dehydrated_summary
                else:
                    summary = strip_wikilinks(b["content"])[:100]
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                imp = b["metadata"].get("importance", 0)
                results.append(f"[importance:{imp}] [bucket_id:{b['id']}] {summary}")
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    if not query or not query.strip():
        # --- Casual chat detection: inject candlestick flavor only in casual mode ---
        # --- 闲聊检测：仅在闲聊模式下注入烛台调味料 ---
        inject_candlestick_flavor = is_casual_chat(query)
        return await _breath_surfacing(
            max_tokens, max_results, brief, type_filter, summary_report, mask_tasks,
            inject_candlestick_flavor=inject_candlestick_flavor
        )

    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not feels:
                return "没有留下过 feel。"
            results = []
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}] [bucket_id:{f['id']}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    # =========================================================
    # Three-step retrieval pipeline / 三步检索管线
    # =========================================================
    pipeline_results = []
    pipeline_exclude_ids = set()

    # --- Step 1: Strong anchor retrieval (pinned + protected ONLY, static rules) ---
    # --- 步骤1：强锚点检索（仅钉选 + 保护记忆，纯静态规则）---
    try:
        strong_anchors = await bucket_mgr.retrieve_strong_anchors(query, mask_tasks=mask_tasks)
        if strong_anchors:
            anchor_parts = []
            for b in strong_anchors:
                meta = b["metadata"]
                name = meta.get("name", b["id"])
                content = strip_wikilinks(b["content"])

                # Original content injection (no dehydration)
                # 原样注入（不脱水）
                tag = ""
                if meta.get("pinned"):
                    tag = " [钉选]"
                elif meta.get("protected"):
                    tag = " [保护]"

                entry = f"⚓ [{name}]{tag} [bucket_id:{b['id']}]\n{content[:500]}"
                anchor_parts.append(entry)
                pipeline_exclude_ids.add(b["id"])
                await bucket_mgr.touch(b["id"])

            if anchor_parts:
                pipeline_results.append("=== 强锚点 ===\n" + "\n---\n".join(anchor_parts))
    except Exception as e:
        logger.warning(f"Step 1 strong anchor retrieval failed: {e}")

    # --- Step 2: Experience extraction (TOP-3 semantically related) ---
    # --- 步骤2：年轮经验提取（TOP-3 语义相关）---
    try:
        top_experiences = await bucket_mgr.retrieve_top_experiences(query, top_n=3, mask_tasks=mask_tasks)
        if top_experiences:
            exp_parts = []
            for exp in top_experiences:
                meta = exp["metadata"]
                name = meta.get("name", exp["id"])
                content = strip_wikilinks(exp.get("content", ""))

                # Refined injection (提炼后注入)
                # Use dehydrated_summary if available, else first 200 chars
                refined = meta.get("dehydrated_summary", "")
                if not refined:
                    refined = content[:200] if content else name

                exp_type = meta.get("exp_type", "")
                apply_count = meta.get("apply_count", 0)
                entry = f"🌳 [{name}]"
                if exp_type:
                    entry += f" #{exp_type}"
                if apply_count > 0:
                    entry += f" (应用{apply_count}次)"
                entry += f"\n  {refined}"
                exp_parts.append(entry)
                pipeline_exclude_ids.add(exp["id"])
                bucket_mgr.record_injection(exp["id"])

            if exp_parts:
                pipeline_results.append("=== 年轮经验 ===\n" + "\n---\n".join(exp_parts))
    except Exception as e:
        logger.warning(f"Step 2 experience extraction failed: {e}")

    # --- Step 3: Hybrid memory bucket retrieval ---
    # --- 步骤3：记忆桶混合检索 ---
    # (handled below with existing search logic, using pipeline_exclude_ids)
    # (在下方现有检索逻辑中处理，使用 pipeline_exclude_ids 排除已注入的桶)

    try:
        matches = await bucket_mgr.search(
            query,
            limit=max(max_results * 2, 30),
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            mask_tasks=mask_tasks,
            force_keyword=force_keyword,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    matches = [b for b in matches if not (b["metadata"].get("pinned") or b["metadata"].get("protected"))]

    # --- Exclude buckets already injected in Steps 1 & 2 ---
    # --- 排除已在步骤1和步骤2中注入的桶 ---
    if pipeline_exclude_ids:
        matches = [b for b in matches if b["id"] not in pipeline_exclude_ids]
    
    if type_filter:
        matches = [b for b in matches if b["metadata"].get("type") == type_filter]

    matched_ids = {b["id"] for b in matches}
    # --- Build vector similarity map for threshold checking ---
    # --- 构建向量相似度映射，用于阈值检查 ---
    vector_sim_map = {}
    try:
        vector_results = await embedding_engine.search_similar(query, top_k=max(max_results * 2, 30))
        for bucket_id, sim_score in vector_results:
            vector_sim_map[bucket_id] = sim_score
            if bucket_id not in matched_ids and sim_score > 0.5:
                bucket = await bucket_mgr.get(bucket_id)
                if bucket and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
                    if type_filter and bucket["metadata"].get("type") != type_filter:
                        continue
                    bucket["score"] = round(sim_score * 100, 2)
                    bucket["vector_match"] = True
                    matches.append(bucket)
                    matched_ids.add(bucket_id)
    except Exception as e:
        logger.warning(f"Vector search failed, using keyword only / 向量搜索失败: {e}")

    results = []
    summarized_buckets = []
    token_used = 0
    shown_count = 0

    for bucket in matches:
        if shown_count >= max_results or token_used >= max_tokens:
            summarized_buckets.append(bucket)
            continue

        # --- Activation threshold for pattern/experience types ---
        # --- 年轮经验激活阈值：语义相似度低于阈值则跳过 ---
        bucket_type = bucket["metadata"].get("type", "")
        if bucket_type in ("pattern", "experience"):
            if not bucket_mgr.check_similarity_threshold(bucket["id"], query, vector_sim_map):
                continue

        # --- Cooldown check for pattern/experience types ---
        # --- 年轮经验冷却检查：频繁注入的降权或跳过 ---
        in_cooldown = False
        weight_factor = 1.0
        if bucket_type in ("pattern", "experience"):
            in_cooldown, weight_factor = bucket_mgr.check_cooldown(bucket["id"])
            if in_cooldown and weight_factor < 0.3:
                continue  # Too soon, skip entirely

        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            
            decay_stage = bucket["metadata"].get("decay_stage", 1)
            
            if decay_stage == 3:
                summary = f"[已消化] [bucket_id:{bucket['id']}] {bucket['metadata'].get('name', '')} - 知识已内化"
                summary_tokens = count_tokens_approx(summary)
                if token_used + summary_tokens > max_tokens:
                    summarized_buckets.append(bucket)
                    continue
                results.append(summary)
                token_used += summary_tokens
                shown_count += 1
                continue
            
            context_metadata = bucket["metadata"].get("context_metadata", {})
            event_context = context_metadata.get("event_context", "")
            
            score = bucket.get("score", 0)
            is_high_relevance = score >= 0.3
            
            if decay_stage == 2:
                dehydrated_summary = bucket["metadata"].get("dehydrated_summary", "")
                if dehydrated_summary:
                    summary = dehydrated_summary
                else:
                    summary = strip_wikilinks(bucket["content"])[:100]
                summary = f"[总结] [bucket_id:{bucket['id']}] {summary}"
                
                if event_context and is_high_relevance:
                    summary = f"{event_context}\n{summary}"
            else:
                content = strip_wikilinks(bucket["content"])
                if bucket.get("vector_match"):
                    summary = f"[语义关联] [bucket_id:{bucket['id']}] {content[:200]}"
                else:
                    summary = f"[bucket_id:{bucket['id']}] {content[:200]}"
                
                if event_context:
                    if is_high_relevance:
                        summary = f"{event_context}\n{summary}"
                    else:
                        context_summary = event_context[:80] + "..." if len(event_context) > 80 else event_context
                        summary = f"[背景: {context_summary}] {summary}"

            # Add cooldown tag for pattern/experience types
            # 为年轮经验添加冷却标记
            if in_cooldown:
                summary += " [冷却中]"

            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                summarized_buckets.append(bucket)
                continue
            await bucket_mgr.touch(bucket["id"])
            # Record injection for cooldown tracking
            # 记录注入，用于冷却跟踪
            if bucket_type in ("pattern", "experience"):
                bucket_mgr.record_injection(bucket["id"])
            results.append(summary)
            token_used += summary_tokens
            shown_count += 1
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    if len(matches) < 3 and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if type_filter:
                low_weight = [b for b in low_weight if b["metadata"].get("type") == type_filter]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
                    if dehydrated_summary:
                        summary = dehydrated_summary
                    else:
                        summary = strip_wikilinks(b["content"])[:100]
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    # --- Fallback mechanism: when all normalized Final_Scores are below threshold ---
    # --- 保底机制：当所有归一化后的最终得分都低于阈值时 ---
    # If ALL matches have normalized Final_Score < 0.4 OR no matches at all, trigger fallback
    # 如果所有匹配的记忆桶归一化最终得分都小于 0.4 或没有任何匹配，触发保底机制
    # 
    # Note: Uses normalized Final_Score = 0.3*Emotion + 0.2*Priority + 0.4*Vector + 0.5*Topic + 0.15*Time
    #       注意：使用归一化后的最终得分，综合考虑情绪、优先级、向量相似度、主题相关性和时间亲近度
    if matches:
        all_low_score = True
        for bucket in matches:
            # Use normalized Final_Score if available, otherwise use vector similarity as fallback
            # 使用归一化后的最终得分，如果没有则回退到向量相似度
            final_score = bucket.get("score", 0.0) / 100.0 if bucket.get("score", 0) > 1 else bucket.get("score", 0.0)
            if final_score >= 0.4:
                all_low_score = False
                break
    else:
        all_low_score = True  # No matches = definitely trigger fallback
    
    if all_low_score:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            # Filter: last_active within 7 days AND resolved=False
            # 过滤条件：最近7天活跃 且 未解决
            seven_days_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
            recent_unresolved = [
                b for b in all_buckets
                if not b["metadata"].get("resolved", False)
                and b["metadata"].get("last_active", "") >= seven_days_ago
                and b["id"] not in matched_ids
            ]
            
            # Apply mask_tasks filtering to fallback too
            # 保底机制也应用任务屏蔽
            if mask_tasks:
                recent_unresolved = bucket_mgr._mask_task_buckets(recent_unresolved)
            
            if type_filter:
                recent_unresolved = [
                    b for b in recent_unresolved 
                    if b["metadata"].get("type") == type_filter
                ]
            
            if recent_unresolved:
                fallback_buckets = random.sample(recent_unresolved, min(2, len(recent_unresolved)))
                fallback_results = []
                for b in fallback_buckets:
                    dehydrated_summary = b["metadata"].get("dehydrated_summary", "")
                    if dehydrated_summary:
                        summary = dehydrated_summary
                    else:
                        summary = strip_wikilinks(b["content"])[:100]
                    fallback_results.append(f"[fallback: random recent] [bucket_id:{b['id']}] {summary}")
                
                # Check token budget before appending
                # 在添加前检查 token 预算
                fallback_text = "--- 保底记忆 ---\n" + "\n---\n".join(fallback_results)
                fallback_tokens = count_tokens_approx(fallback_text)
                if token_used + fallback_tokens <= max_tokens:
                    results.append(fallback_text)
                    token_used += fallback_tokens
                    logger.info(
                        f"Breath: fallback triggered - all similarities < 0.4 or no matches, "
                        f"added {len(fallback_buckets)} recent unresolved buckets"
                    )
                else:
                    logger.info(
                        f"Breath: fallback triggered but skipped - token budget exceeded "
                        f"(used:{token_used}, max:{max_tokens})"
                    )
        except Exception as e:
            logger.warning(f"Fallback mechanism failed / 保底机制失败: {e}")

    if summary_report and summarized_buckets:
        # --- Step 3 continuation: non-top-N buckets return only one_line_summary ---
        # --- 步骤3续：非 TOP-N 桶仅返回 one_line_summary ---
        # Filter by normalized Final_Score threshold:
        # - score >= 0.7: already shown in full content above
        # - 0.4 <= score < 0.7: show one_line_summary
        # - score < 0.4: skip entirely (too irrelevant)
        # 按归一化最终得分阈值过滤：
        # - 得分 >= 0.7：已在上面显示完整内容
        # - 0.4 <= 得分 < 0.7：显示 one_line_summary
        # - 得分 < 0.4：完全跳过（太不相关）
        #
        # Final_Score formula: 0.3*Emotion + 0.2*Priority + 0.4*Vector + 0.5*Topic + 0.15*Time
        # 最终得分公式：综合考虑情绪、优先级、向量相似度、主题相关性和时间亲近度
        summary_lines = []
        for b in summarized_buckets:
            bucket_id = b["id"]
            # Use normalized Final_Score if available, otherwise use vector similarity as fallback
            # 使用归一化后的最终得分，如果没有则回退到向量相似度
            final_score = b.get("score", 0.0) / 100.0 if b.get("score", 0) > 1 else b.get("score", 0.0)
            
            # Skip buckets with very low score (< 0.4)
            # 跳过得分极低的桶（< 0.4）
            if final_score > 0.0 and final_score < 0.4:
                logger.debug(f"Skipping low-score bucket from summary: {bucket_id} (final_score={final_score:.3f})")
                continue
            
            one_line = b["metadata"].get("one_line_summary", "")
            dehydrated = b["metadata"].get("dehydrated_summary", "")
            name = b["metadata"].get("name", b["id"])
            
            # Add score indicator for moderate matches
            # 为中等得分的匹配添加得分指示
            score_tag = ""
            if final_score >= 0.4 and final_score < 0.7:
                score_tag = f" [score:{final_score:.2f}]"
            elif final_score >= 0.7:
                score_tag = f" [score:{final_score:.2f}]"
            
            if one_line:
                summary_lines.append(f"[摘要]{score_tag} {name}: {one_line}")
            elif dehydrated:
                summary_lines.append(f"[摘要]{score_tag} {name}: {dehydrated[:60]}")
            else:
                summary_lines.append(f"[摘要]{score_tag} {name}")
        
        if summary_lines:
            results.append(f"\n---\n📋 记忆速览（共{len(summary_lines)}条）:\n" + "\n".join(summary_lines))

    # --- Passive identity trigger: inject identities mentioned in query ---
    # --- 被动触发：只在 query 中提到名册实体时才注入相关名册 ---
    if not type_filter or type_filter == "identity":
        try:
            mentioned_identities = await identity_mgr.find_mentioned_identities(query)
            if mentioned_identities:
                ident_results = []
                for ident in mentioned_identities:
                    meta = ident.get("metadata", {})
                    name = meta.get("name", ident["id"])
                    aliases = ", ".join(meta.get("aliases", []))
                    traits = ", ".join(meta.get("core_traits", []))
                    parts_ident = [f"👤 [{name}]"]
                    if aliases:
                        parts_ident.append(f"别名: {aliases}")
                    if traits:
                        parts_ident.append(f"特征: {traits}")
                    content = strip_wikilinks(ident.get("content", ""))
                    if content:
                        parts_ident.append(content[:200])
                    ident_results.append("\n".join(parts_ident))
                if ident_results:
                    results.append("\n---\n=== 提及的人物 ===\n" + "\n---\n".join(ident_results))
        except Exception as e:
            logger.warning(f"Passive identity trigger failed: {e}")

    # --- Prepend pipeline results (Steps 1 & 2) before search results ---
    # --- 将管线结果（步骤1和步骤2）前置到检索结果之前 ---
    if pipeline_results:
        results = pipeline_results + results

    # --- Low-priority candlestick flavor injection for search mode ---
    # --- 搜索模式下的低优先级烛台调味料注入 ---
    # Only inject when:
    # 1. It's casual chat
    # 2. No strong anchors triggered (step 1 returned empty)
    # 仅在以下条件注入：
    # 1. 是闲聊
    # 2. 没有强锚点触发（步骤1返回空）
    if is_casual_chat(query) and not strong_anchors:
        try:
            flavor_candles = await bucket_mgr.retrieve_candlesticks_for_flavor(
                query=query,
                max_count=2,
                random_probability=0.3,
            )
            if flavor_candles:
                flavor_parts = []
                for candle in flavor_candles:
                    title = candle.get("title", "")
                    content = candle.get("content", "")
                    created = candle.get("created", "")[:10] if candle.get("created") else ""
                    entry = f"🕯️ [{title or '感想'}]"
                    if created:
                        entry += f" [{created}]"
                    if content:
                        entry += f"\n  {content[:80]}"
                    flavor_parts.append(entry)
                if flavor_parts:
                    results.append("\n---\n=== 语气调味（烛台）===\n" + "\n---\n".join(flavor_parts))
        except Exception as e:
            logger.warning(f"Candlestick flavor injection failed in search mode / 搜索模式下烛台调味料注入失败: {e}")

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    final_text = "\n".join(results)
    
    if housekeeper_prefetch:
        final_text += housekeeper_prefetch
    
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "shown": shown_count, "summarized": len(summarized_buckets), "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    task_flag: bool = False,
    source_bucket: str = "",
    valence: float = -1,
    arousal: float = -1,
    event_context: str = "",
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。task_flag=True标记为任务类记忆(当用户生病/疲惫/情绪化时自动屏蔽,防止催任务)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。event_context=事件背景(时间/地点/状态/当时发生的事件)。"""
    await decay_engine.ensure_started()
    await tag_normalizer.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=None,
            bucket_type="feel",
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        
        asyncio.create_task(_generate_one_line_summary_async(bucket_id, content))
        tag_normalizer.notify_new_record(1)
        
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
                
                source_bucket_data = await bucket_mgr.get(source_bucket.strip())
                if source_bucket_data:
                    source_content = source_bucket_data.get("content", "")
                    asyncio.create_task(_generate_one_line_summary_async(source_bucket.strip(), source_content))
                
                asyncio.create_task(_reinforce_related_experiences(source_bucket.strip()))
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 0: Extract Event Context / 提取事件背景 ---
    # 如果没有显式提供 event_context，则尝试从 content 中提取
    context_metadata = {}
    if event_context and event_context.strip():
        context_metadata["event_context"] = event_context.strip()
        context_metadata["context_provided"] = True
    else:
        context_metadata["event_context"] = _extract_event_context(content)
        context_metadata["context_provided"] = False
    
    # --- Step 0.5: Noise detection and TTL / 噪音检测和生存时间 ---
    ttl = None
    if _detect_noise_content(content):
        ttl = 7
    
    # --- Step 0.6: Status override / 状态覆盖 ---
    # 如果新内容表示"已恢复/已解决"，则标记旧的同类状态为已解决
    status_key = _extract_status_key(content)
    if status_key:
        if any(keyword in status_key for keyword in ["好了", "不痛了", "恢复了", "痊愈了"]):
            try:
                all_buckets = await bucket_mgr.list_all(include_archive=False)
                for bucket in all_buckets:
                    bucket_status_key = bucket["metadata"].get("status_key")
                    if bucket_status_key and bucket_status_key in ["肚子痛", "胃痛", "腹痛", "痛经", "头痛", "头晕", "发烧", "感冒", "咳嗽", "身体不适", "不舒服", "难受"]:
                        if not bucket["metadata"].get("resolved", False):
                            await bucket_mgr.update(bucket["id"], resolved=True)
                            logger.info(f"Status override: marked {bucket['id']} as resolved")
            except Exception as e:
                logger.warning(f"Status override failed: {e}")
    
    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "emotions": [], "dominant_emotion": "",
            "emotion_metrics": {"overall_intensity": 0.3, "emotional_range": 0.0, "emotional_valence": 0.0},
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    auto_emotions = analysis.get("emotions", [])
    auto_dominant = analysis.get("dominant_emotion", "")
    emotion_metrics = analysis.get("emotion_metrics", {})
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    final_emotions = auto_emotions
    final_dominant = auto_dominant
    final_emotion_metrics = emotion_metrics

    if 0 <= valence <= 1 or 0 <= arousal <= 1:
        v = valence if 0 <= valence <= 1 else 0.5
        a = arousal if 0 <= arousal <= 1 else 0.3
        final_emotions = bucket_mgr._valence_arousal_to_emotions(v, a)
        if final_emotions:
            final_dominant = max(final_emotions, key=lambda e: e["intensity"])["label"]
        final_emotion_metrics = {
            "overall_intensity": a,
            "emotional_range": abs(v - 0.5) * 2,
            "emotional_valence": v * 2 - 1,
        }

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            emotions=final_emotions,
            dominant_emotion=final_dominant,
            emotion_metrics=final_emotion_metrics,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
            task_flag=task_flag,
            context_metadata=context_metadata,
            ttl=ttl,
            status_key=status_key,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        
        # Generate one-line summary asynchronously for pinned buckets
        asyncio.create_task(_generate_one_line_summary_async(bucket_id, content))
        
        tag_normalizer.notify_new_record(1)
        return f"📌钉选→{bucket_id} {','.join(domain)}"

    result_name, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        emotions=final_emotions,
        dominant_emotion=final_dominant,
        emotion_metrics=final_emotion_metrics,
        name=suggested_name,
        task_flag=task_flag,
        dehydrator=dehydrator,
        context_metadata=context_metadata,
        ttl=ttl,
        status_key=status_key,
    )

    action = "合并→" if is_merged else "新建→"
    tag_normalizer.notify_new_record(1)
    return f"{action}{result_name} {','.join(domain)}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await decay_engine.ensure_started()
    await tag_normalizer.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "emotions": [], "dominant_emotion": "",
                "tags": [], "suggested_name": "",
            }
        emotions = analysis.get("emotions", [])
        dominant = analysis.get("dominant_emotion", "")
        result_name, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            emotions=emotions,
            dominant_emotion=dominant,
            name=analysis.get("suggested_name", ""),
            dehydrator=dehydrator,
        )
        action = "合并" if is_merged else "新建"
        emo_str = ",".join(f"{e['label']}({e['intensity']:.1f})" for e in emotions) if emotions else ""
        tag_normalizer.notify_new_record(1)
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} {emo_str}"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            emotions = item.get("emotions", [])
            dominant = item.get("dominant_emotion", "")
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                emotions=emotions,
                dominant_emotion=dominant,
                name=item.get("name", ""),
                dehydrator=dehydrator,
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    tag_normalizer.notify_new_record(len(items))
    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    force_resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    task_flag: int = -1,
    content: str = "",
    delete: bool = False,
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏(保留但不浮现)/0取消隐藏,task_flag=1标记为任务类/0取消任务标记(用户生病/疲惫/情绪化时自动屏蔽任务类桶,防止催任务),content=替换桶正文,delete=True删除。只传需改的,-1或空=不改。注意：task_flag=True的桶需要force_resolved=1才能设置resolved=1，防止dream()误解决待办事项。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete mode / 删除模式 ---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            embedding_engine.delete_embedding(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
        if resolved == 1:
            # Add force_resolved if explicitly requested
            if force_resolved == 1:
                updates["force_resolved"] = True
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if task_flag in (0, 1):
        updates["task_flag"] = bool(task_flag)
    if content:
        updates["content"] = content

    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    # Re-generate embedding if content changed
    if "content" in updates:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception as e:
            logger.warning(f"Failed to store embedding after update: {e}")

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
        # Task bucket protection hint
        if bucket.get("metadata", {}).get("task_flag") and updates["resolved"]:
            if updates.get("force_resolved"):
                changed += "（强制解决任务桶）"
            else:
                # This should have been blocked by bucket_manager, but just in case
                changed += "（注意：任务桶需要 force_resolved=1 才能解决）"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    if "task_flag" in updates:
        if updates["task_flag"]:
            changed += " → 已标记为任务类（脆弱状态下自动屏蔽）"
        else:
            changed += " → 已取消任务标记"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def pulse(include_archive: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"
    
    try:
        identities = await identity_mgr.list_all()
        patterns = await pattern_mgr.list_all()
    except Exception as e:
        identities = []
        patterns = []

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"身份档案: {len(identities)} 个\n"
        f"行为模式: {len(patterns)} 个\n"
        f"事件记忆: {stats['permanent_count'] + stats['dynamic_count']} 个\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
        f"标签归一化: {'运行中' if tag_normalizer.is_running else '已停止'}"
        f"（距上次运行: {tag_normalizer._records_since_last_run}/{tag_normalizer.batch_threshold} 条）\n"
    )

    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        bucket_type = meta.get("type", "")
        
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif bucket_type == "permanent":
            icon = "📦"
        elif bucket_type == "feel":
            icon = "🫧"
        elif bucket_type == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        elif bucket_type == "identity":
            icon = "👤"
        elif bucket_type == "pattern":
            icon = "📐"
        else:
            icon = "💭"
        
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        
        domains = ",".join(meta.get("domain", []))
        
        emotions = meta.get("emotions", [])
        if emotions:
            emo_str = ",".join(f"{e['label']}({e['intensity']:.1f})" for e in emotions)
        else:
            val = meta.get("valence", 0.5)
            aro = meta.get("arousal", 0.3)
            emo_str = f"V{val:.1f}/A{aro:.1f}"
        
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"类型:{bucket_type} "
            f"主题:{domains} "
            f"情感:{emo_str} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
@mcp.tool()
async def dream() -> str:
    """做梦——读取最近新增的记忆桶,供你自省。读完后可以trace(resolved=1)放下,或hold(feel=True)写感受。"""
    await decay_engine.ensure_started()

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # --- Step 0: Auto-merge similar memories from the past week ---
    # --- 步骤0：自动合并过去一周内的相似记忆 ---
    merge_summary = ""
    try:
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        recent_week_buckets = []
        
        for b in all_buckets:
            meta = b["metadata"]
            if meta.get("type") in ("permanent", "feel") or meta.get("pinned") or meta.get("protected"):
                continue
            
            created_str = meta.get("created", "")
            try:
                created = datetime.fromisoformat(str(created_str))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= one_week_ago:
                    recent_week_buckets.append(b)
            except (ValueError, TypeError):
                continue
        
        if len(recent_week_buckets) >= 3:
            similarity_groups = {}
            for i, b1 in enumerate(recent_week_buckets):
                for j, b2 in enumerate(recent_week_buckets[i+1:]):
                    try:
                        content1 = b1["content"]
                        content2 = b2["content"]
                        from rapidfuzz import fuzz
                        similarity = fuzz.ratio(content1, content2)
                        if similarity >= 60:
                            group_key = tuple(sorted([b1["id"], b2["id"]]))
                            if group_key not in similarity_groups:
                                similarity_groups[group_key] = set()
                            similarity_groups[group_key].add(b1["id"])
                            similarity_groups[group_key].add(b2["id"])
                    except Exception:
                        pass
            
            merged_count = 0
            merged_groups = []
            for group_ids in similarity_groups.values():
                if len(group_ids) >= 3:
                    group_buckets = [b for b in recent_week_buckets if b["id"] in group_ids]
                    if group_buckets:
                        merged_groups.append(group_buckets)
            
            for group in merged_groups:
                contents = [b["content"] for b in group]
                combined_content = "\n".join(contents)
                
                summary_prompt = f"请总结以下记忆片段，提炼成一条高密度的长效总结：\n{combined_content}\n\n总结要求：简洁、准确、保留关键信息（时间、状态、频率）"
                
                try:
                    global dehydrator
                    if dehydrator and dehydrator.api_available:
                        response = await dehydrator.client.chat.completions.create(
                            model=dehydrator.model,
                            messages=[
                                {"role": "system", "content": "你是一个记忆总结助手，擅长将多条相似记忆提炼成一条高密度总结。"},
                                {"role": "user", "content": summary_prompt},
                            ],
                            max_tokens=100,
                            temperature=0.3,
                        )
                        summary = response.choices[0].message.content.strip()
                    else:
                        summary = "相似记忆合并总结：" + combined_content[:50]
                except Exception as e:
                    logger.warning(f"Auto-merge LLM call failed: {e}")
                    summary = "相似记忆合并总结：" + combined_content[:50]
                
                first_bucket = group[0]
                meta = first_bucket["metadata"]
                new_bucket_id = await bucket_mgr.create(
                    content=summary,
                    tags=meta.get("tags", []),
                    importance=min(10, meta.get("importance", 5) + 1),
                    domain=meta.get("domain", ["未分类"]),
                    emotions=meta.get("emotions", []),
                    dominant_emotion=meta.get("dominant_emotion", ""),
                    emotion_metrics=meta.get("emotion_metrics", {}),
                    name=f"合并总结: {meta.get('name', '相似记忆')}",
                    bucket_type="dynamic",
                )
                
                for b in group:
                    try:
                        await bucket_mgr.update(b["id"], resolved=True)
                    except Exception:
                        pass
                
                merged_count += 1
                logger.info(f"Auto-merged {len(group)} similar memories into {new_bucket_id}")
            
            if merged_count > 0:
                merge_summary = f"🔄 自动合并了 {merged_count} 组相似记忆为高密度总结。\n\n"
    except Exception as e:
        logger.warning(f"Auto-merge step failed: {e}")

    # --- Filter: recent surface-level dynamic buckets (not permanent/pinned/feel) ---
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]

    # --- Sort by creation time desc, take top 10 ---
    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[:10]

    if not recent:
        return "没有需要消化的新记忆。"

    parts = []
    for b in recent:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        
        emotions = meta.get("emotions", [])
        if emotions:
            emo_str = ",".join(f"{e['label']}({e['intensity']:.1f})" for e in emotions)
        else:
            val = meta.get("valence", 0.5)
            aro = meta.get("arousal", 0.3)
            emo_str = f"V{val:.1f}/A{aro:.1f}"
        
        created = meta.get("created", "")
        parts.append(
            f"[{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} 情绪:{emo_str} "
            f"创建:{created}\n"
            f"ID: {b['id']}\n"
            f"{strip_wikilinks(b['content'][:500])}"
        )

    header = (
        "=== Dreaming ===\n"
        + merge_summary
        + "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", emotions=[{\"label\":\"情绪词\",\"intensity\":0~1}]) 写下来。\n"
        "emotions 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
        "当同类事件积累3条以上时，可以考虑用 pattern_create 创建行为模式。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 7+: analytics — Get memory analytics
# 工具 7+：analytics — 获取记忆分析数据
# =============================================================
@mcp.tool()
async def analytics() -> str:
    """获取记忆库的统计分析数据，包括情绪分布、类型统计、活跃度趋势等。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        emotion_counts = {}
        type_counts = {}
        domain_counts = {}
        date_counts = {}
        
        for b in all_buckets:
            meta = b.get("metadata", {})
            emotions = meta.get("emotions", [])
            b_type = meta.get("type", "dynamic")
            domains = meta.get("domain", [])
            created = meta.get("created", "")
            
            for emo in emotions:
                if isinstance(emo, dict):
                    emo_label = emo.get("label", str(emo))
                else:
                    emo_label = str(emo)
                emotion_counts[emo_label] = emotion_counts.get(emo_label, 0) + 1
            
            type_counts[b_type] = type_counts.get(b_type, 0) + 1
            
            for d in domains:
                domain_counts[d] = domain_counts.get(d, 0) + 1
            
            if created:
                date_key = created[:10]
                date_counts[date_key] = date_counts.get(date_key, 0) + 1
        
        top_emotions = sorted(emotion_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        recent_dates = sorted(date_counts.items(), key=lambda x: x[0])[-7:]
        
        lines = []
        lines.append("=== 记忆库分析 ===")
        lines.append(f"总记忆桶: {len(all_buckets)}")
        
        if type_counts:
            lines.append("\n类型分布:")
            for t, c in type_counts.items():
                lines.append(f"  - {t}: {c}")
        
        if top_emotions:
            lines.append("\n情绪分布(前5):")
            for e, c in top_emotions:
                lines.append(f"  - {e}: {c}")
        
        if top_domains:
            lines.append("\n热门主题(前5):")
            for d, c in top_domains:
                lines.append(f"  - {d}: {c}")
        
        if recent_dates:
            lines.append("\n近7天活跃度:")
            for d, c in recent_dates:
                lines.append(f"  - {d}: {c}条")
        
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"analytics failed: {e}")
        return f"分析失败: {e}"


# =============================================================
# Tool 23.5: tag_normalize — Tag normalization batch job
# 工具 23.5：tag_normalize — 标签归一化批量任务
# =============================================================
@mcp.tool()
async def tag_normalize(action: str = "run") -> str:
    """标签归一化任务。action可选: run(立即执行), status(查看状态)。
    后台自动每周或每50条记录运行一次，将非标准标签映射到泛化标签树。"""
    if action == "status":
        return (
            f"标签归一化引擎: {'运行中' if tag_normalizer.is_running else '已停止'}\n"
            f"距上次运行: {tag_normalizer._records_since_last_run}/{tag_normalizer.batch_threshold} 条\n"
            f"触发间隔: {tag_normalizer.interval_hours} 小时\n"
            f"最小标签频率: {tag_normalizer.min_tag_frequency}\n"
            f"是否需要运行: {'是' if tag_normalizer.needs_run else '否'}"
        )

    result = await tag_normalizer.run_normalization()
    if result.get("skipped"):
        return f"标签归一化已跳过: {result.get('reason', '未知原因')}"

    if result.get("error"):
        return f"标签归一化失败: {result['error']}"

    mapping_str = ""
    if "mapping" in result and result["mapping"]:
        mapping_lines = [f"  {k} → {v}" for k, v in result["mapping"].items()]
        mapping_str = "\n映射关系:\n" + "\n".join(mapping_lines)

    return (
        f"标签归一化完成\n"
        f"总标签数: {result.get('total_tags', 0)}\n"
        f"非标准标签: {result.get('non_standard', 0)}\n"
        f"已归一化: {result.get('normalized', 0)}\n"
        f"更新桶数: {result.get('buckets_updated', 0)}"
        f"{mapping_str}"
    )


# =============================================================
# Tool 24: memory_export — Export memories
# 工具 24：memory_export — 导出记忆
# =============================================================
@mcp.tool()
async def memory_export(export_type: str = "all") -> str:
    """导出记忆数据。export_type可选: all(全部), dynamic(动态), permanent(永久), identity(身份), pattern(模式), feel(感受)。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        
        if export_type != "all":
            buckets = [b for b in all_buckets if b.get("metadata", {}).get("type") == export_type]
        else:
            buckets = all_buckets
        
        if not buckets:
            return f"没有找到{export_type}类型的记忆。"
        
        import json
        export_data = {
            "export_time": datetime.datetime.now().isoformat(),
            "total_count": len(buckets),
            "type": export_type,
            "buckets": buckets,
        }
        
        return json.dumps(export_data, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"memory_export failed: {e}")
        return f"导出失败: {e}"


# =============================================================
# Tool 25: memory_batch_delete — Batch delete memories
# 工具 25：memory_batch_delete — 批量删除记忆
# =============================================================
@mcp.tool()
async def memory_batch_delete(bucket_ids: str) -> str:
    """批量删除记忆桶。bucket_ids=多个记忆桶ID逗号分隔。"""
    if not bucket_ids or not bucket_ids.strip():
        return "请提供记忆桶ID。"
    
    ids_list = [id.strip() for id in bucket_ids.split(",") if id.strip()]
    if not ids_list:
        return "没有有效的记忆桶ID。"
    
    deleted = 0
    for bucket_id in ids_list:
        try:
            success = await bucket_mgr.delete(bucket_id)
            if success:
                deleted += 1
                embedding_engine.delete_embedding(bucket_id)
        except Exception as e:
            logger.warning(f"Failed to delete bucket {bucket_id}: {e}")
    
    return f"批量删除完成: 成功删除 {deleted}/{len(ids_list)} 个记忆桶"


# =============================================================
# Tool 26: memory_directory — Generate memory directory summary
# 工具 26：memory_directory — 生成记忆目录摘要
# =============================================================
@mcp.tool()
async def memory_directory(detail_level: str = "medium") -> str:
    """生成记忆库的简洁目录摘要，帮助快速了解记忆结构。detail_level可选: brief(仅统计和关键条目), medium(详细分类), full(完整目录)。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        by_type = {}
        for b in all_buckets:
            bucket_type = b.get("metadata", {}).get("type", "event")
            if bucket_type not in by_type:
                by_type[bucket_type] = []
            by_type[bucket_type].append(b)
        
        type_names = {
            "identity": "👤 身份档案",
            "pattern": "📐 行为模式",
            "event": "📝 事件记忆",
            "feel": "🫧 感受记忆",
            "permanent": "📌 永久记忆",
            "archived": "📦 归档记忆",
        }
        
        result = "📚 记忆库目录\n\n"
        result += "=" * 50 + "\n\n"
        
        total_count = len(all_buckets)
        result += f"📊 总览：共 {total_count} 条记忆\n\n"
        
        for bucket_type, buckets in by_type.items():
            type_label = type_names.get(bucket_type, bucket_type)
            count = len(buckets)
            result += f"--- {type_label} ({count}条) ---\n"
            
            if detail_level == "full":
                for b in buckets[:50]:
                    meta = b.get("metadata", {})
                    name = meta.get("name", b["id"])
                    importance = meta.get("importance", 0)
                    tags = meta.get("tags", [])[:3]
                    tags_str = " ".join([f"#{t}" for t in tags])
                    result += f"  [{importance}] {name} {tags_str}\n"
            elif detail_level == "medium":
                key_entries = []
                for b in buckets:
                    meta = b.get("metadata", {})
                    name = meta.get("name", b["id"])
                    importance = meta.get("importance", 0)
                    emotions = meta.get("emotions", [])
                    dominant = emotions[0]["label"] if emotions else ""
                    key_entries.append((importance, name, dominant))
                
                key_entries.sort(key=lambda x: x[0], reverse=True)
                for imp, name, emo in key_entries[:10]:
                    emo_str = f" [{emo}]" if emo else ""
                    result += f"  [{imp}] {name}{emo_str}\n"
                
                if count > 10:
                    result += f"  ... 还有 {count - 10} 条\n"
            else:
                result += f"  ({count} 条记忆)\n"
            
            result += "\n"
        
        emotion_counts = {}
        for b in all_buckets:
            emotions = b.get("metadata", {}).get("emotions", [])
            for e in emotions:
                label = e.get("label", "")
                if label:
                    emotion_counts[label] = emotion_counts.get(label, 0) + 1
        
        if emotion_counts:
            sorted_emotions = sorted(emotion_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            result += "--- 情绪分布 ---\n"
            for emo, cnt in sorted_emotions:
                result += f"  {emo}: {cnt}次\n"
            result += "\n"
        
        top_tags = {}
        for b in all_buckets:
            tags = b.get("metadata", {}).get("tags", [])
            for t in tags:
                if t:
                    top_tags[t] = top_tags.get(t, 0) + 1
        
        if top_tags:
            sorted_tags = sorted(top_tags.items(), key=lambda x: x[1], reverse=True)[:8]
            result += "--- 热门标签 ---\n"
            for tag, cnt in sorted_tags:
                result += f"  #{tag}: {cnt}次\n"
        
        result += "\n" + "=" * 50 + "\n"
        result += "提示：使用 breath(query) 检索具体内容，hold(content) 添加新记忆"
        
        return result
    
    except Exception as e:
        logger.error(f"memory_directory failed: {e}")
        return f"生成目录失败: {e}"


# =============================================================
# Tool 27: get_anchors — 获取情绪锚点
# 工具 27：get_anchors — 获取情绪锚点
# =============================================================
@mcp.tool()
async def get_anchors(active_only: bool = False) -> str:
    """获取所有行为与情绪锚点。锚点只存规则和指导（触发词+情绪基调+行为禁忌），事件细节仅保留 bucket_id 指针。设置 active_only=true 只返回正在生效的锚点。"""
    from utils import format_relative_time
    try:
        anchors = await bucket_mgr.get_anchors(active_only=active_only)
        if not anchors:
            return "暂无锚点记录。"

        results = []
        for anchor in anchors:
            name = anchor.get("name", anchor.get("id", ""))
            atype = anchor.get("anchor_type", "dynamic")
            is_active = anchor.get("is_active", True)
            triggers = anchor.get("triggers", [])
            baseline = anchor.get("emotional_baseline", [])
            boundaries = anchor.get("boundaries", [])
            related_ids = anchor.get("related_bucket_ids", [])
            created = anchor.get("created", "")

            # --- Status tag ---
            status_tag = "✅[生效]" if is_active else "💤[失效]"
            type_tag = "🔒静态" if atype == "static" else "⚡动态"

            # --- TTL info for dynamic anchors ---
            ttl_info = ""
            if atype == "dynamic" and is_active:
                expires_at = anchor.get("expires_at", "")
                if expires_at:
                    try:
                        expiry = datetime.fromisoformat(expires_at)
                        remaining = expiry - datetime.now()
                        hours_left = remaining.total_seconds() / 3600
                        if hours_left > 0:
                            ttl_info = f" [剩余{hours_left:.1f}h]"
                        else:
                            ttl_info = " [已过期]"
                    except (ValueError, TypeError):
                        pass

            # --- Relative created time ---
            rel_created = format_relative_time(created) if created else ""

            entry = f"⚓ {status_tag} {type_tag} [{name}]"
            if rel_created:
                entry += f" [{rel_created}]"
            entry += ttl_info

            # --- Triggers ---
            if triggers:
                entry += f"\n  触发条件: {', '.join(triggers[:5])}"

            # --- Emotional baseline ---
            if baseline:
                entry += f"\n  情绪基调: {', '.join(baseline[:5])}"

            # --- Boundaries ---
            if boundaries:
                entry += f"\n  行为禁忌: {', '.join(boundaries[:5])}"

            # --- Related bucket pointers ---
            if related_ids:
                entry += f"\n  关联桶: {', '.join(related_ids[:3])}"

            results.append(entry)

        # --- Summary line ---
        active_count = sum(1 for a in anchors if a.get("is_active", True))
        summary_line = f"=== 锚点 (共{len(anchors)}个, 生效{active_count}个) ==="

        return summary_line + "\n" + "\n---\n".join(results)

    except Exception as e:
        logger.error(f"get_anchors failed: {e}")
        return f"获取锚点失败: {e}"


# =============================================================
# Tool 28: get_timelines — 获取时间链
# 工具 28：get_timelines — 获取时间链
# =============================================================
@mcp.tool()
async def get_timelines() -> str:
    """获取所有时间链列表，用于AI检索时查阅事件发展脉络。时间链的时间戳为ISO-8601格式，返回时动态生成相对时间描述。"""
    from utils import format_relative_time
    try:
        timelines = await bucket_mgr.get_timelines()
        if not timelines:
            return "暂无时间链记录。"

        results = []
        for timeline in timelines:
            metadata = timeline.get("metadata", {})
            title = metadata.get("title", "")
            summary = metadata.get("summary", "")
            phases = metadata.get("phases", [])
            created = timeline.get("created", "")

            # --- Dynamically generate relative time for created ---
            # --- 动态生成创建时间的相对描述 ---
            rel_created = format_relative_time(created) if created else ""

            entry = f"📅 [{title}]"
            if rel_created:
                entry += f" [{rel_created}]"
            if summary:
                entry += f"\n  摘要: {summary[:150]}"
            if phases:
                entry += f"\n  阶段数: {len(phases)}"
                for i, phase in enumerate(phases[:3], 1):
                    phase_time = phase.get("time", "")
                    # --- Dynamically generate relative time for each phase ---
                    # --- 动态生成每个阶段的相对时间 ---
                    rel_phase_time = format_relative_time(phase_time) if phase_time else ""
                    phase_desc = phase.get("description", phase.get("title", ""))[:30]
                    entry += f"\n    {i}. [{rel_phase_time}] {phase_desc}"

            results.append(entry)

        return "=== 时间链 ===\n" + "\n---\n".join(results)

    except Exception as e:
        logger.error(f"get_timelines failed: {e}")
        return f"获取时间链失败: {e}"


# =============================================================
# Tool 29: get_memos — 获取烛台(备忘录)
# 工具 29：get_memos — 获取烛台(备忘录)
# =============================================================
@mcp.tool()
async def get_memos() -> str:
    """获取所有烛台(备忘录)记录，用于AI检索时查阅重要备忘事项。"""
    try:
        candlesticks = await bucket_mgr.get_candlesticks()
        if not candlesticks:
            return "暂无烛台(备忘录)记录。"
        
        recent_candlesticks = sorted(candlesticks, key=lambda c: c.get("created", ""), reverse=True)
        
        results = []
        for c in recent_candlesticks:
            metadata = c.get("metadata", {})
            title = metadata.get("title", "")
            content = c.get("content", "")
            created = c.get("created", "")
            
            entry = f"🕯️ [{title}]"
            if created:
                entry += f" [{created[:10]}]"
            if content:
                entry += f"\n  {content[:200]}"
            
            results.append(entry)
        
        return "=== 烛台备忘录 ===\n" + "\n---\n".join(results)
    
    except Exception as e:
        logger.error(f"get_memos failed: {e}")
        return f"获取烛台备忘录失败: {e}"


# =============================================================
# Tool 30: trace_chain — 追溯因果链
# 工具 30：trace_chain — 追溯因果链
# =============================================================
@mcp.tool()
async def trace_chain(
    bucket_id: str,
    direction: str = "both",
    max_depth: int = 3,
) -> str:
    """
    追溯某条记忆的因果链（前因后果）。通过指针直接调出关联事件，无需重新扫描全库。
    
    Trace the causal chain of a memory (cause and effect). Uses bidirectional
    pointers (previous_event_id / next_event_id) to retrieve related events
    directly, without scanning the entire database.
    
    Args:
        bucket_id: 记忆桶 ID
        direction: 遍历方向，"previous"=前因(更早事件), "next"=后果(更晚事件), "both"=双向(默认)
        max_depth: 最大遍历深度，默认3层，防止无限遍历
    
    Returns:
        因果链文本，包含当前事件、前因列表、后果列表
    """
    try:
        chain = await bucket_mgr.get_event_chain(bucket_id, direction, max_depth)
        
        if not chain["current"]:
            return f"记忆桶 {bucket_id} 不存在。"
        
        parts = []
        current = chain["current"]
        
        # --- Current event ---
        # --- 当前事件 ---
        parts.append(f"=== 当前事件 ===")
        parts.append(f"📌 [{current['name']}]")
        parts.append(f"ID: {current['id']}")
        if current["created"]:
            parts.append(f"时间: {current['created']}")
        if current["one_line_summary"]:
            parts.append(f"摘要: {current['one_line_summary']}")
        
        # --- Previous events (causes) ---
        # --- 前因事件 ---
        if chain["previous"]:
            parts.append("\n=== 前因 (按时间倒序) ===")
            for i, event in enumerate(chain["previous"], 1):
                prefix = "←" * i
                parts.append(f"{prefix} [{event['name']}]")
                parts.append(f"   ID: {event['id']}")
                if event["created"]:
                    parts.append(f"   时间: {event['created']}")
                if event["one_line_summary"]:
                    parts.append(f"   摘要: {event['one_line_summary']}")
        
        # --- Next events (effects) ---
        # --- 后果事件 ---
        if chain["next"]:
            parts.append("\n=== 后果 (按时间正序) ===")
            for i, event in enumerate(chain["next"], 1):
                prefix = "→" * i
                parts.append(f"{prefix} [{event['name']}]")
                parts.append(f"   ID: {event['id']}")
                if event["created"]:
                    parts.append(f"   时间: {event['created']}")
                if event["one_line_summary"]:
                    parts.append(f"   摘要: {event['one_line_summary']}")
        
        if not chain["previous"] and not chain["next"]:
            parts.append("\n（无因果链关联）")
        
        return "\n".join(parts)
    
    except Exception as e:
        logger.error(f"trace_chain failed: {e}")
        return f"追溯因果链失败: {e}"


# =============================================================
# Tool 31: link_events — 建立因果链
# 工具 31：link_events — 建立因果链
# =============================================================
@mcp.tool()
async def link_events(
    prev_id: str,
    next_id: str,
) -> str:
    """
    建立两个事件之间的因果关系。
    
    Create a causal relationship between two events.
    
    Args:
        prev_id: 前因事件 ID（更早发生的事件）
        next_id: 后果事件 ID（更晚发生的事件）
    
    Returns:
        操作结果
    """
    try:
        success = await bucket_mgr.link_events(prev_id, next_id)
        if success:
            return f"✅ 已建立因果链：[{prev_id}] → [{next_id}]"
        else:
            return f"❌ 建立因果链失败：请检查两个桶 ID 是否存在"
    
    except Exception as e:
        logger.error(f"link_events failed: {e}")
        return f"建立因果链失败: {e}"


# =============================================================
# Tool 32: manage_identity_relation — 管理身份关系
# 工具 32：manage_identity_relation — 管理身份关系
# =============================================================
@mcp.tool()
async def manage_identity_relation(
    action: str,
    from_id: str = None,
    to_id: str = None,
    relation_type: str = "朋友",
    base_weight: float = 5.0,
) -> str:
    """
    管理身份之间的关系（建立、查询、更新权重）。
    
    Manage relationships between identities (create, query, update weight).
    
    Args:
        action: 操作类型，"add"=建立关系, "query"=查询关系, "update_weight"=更新权重
        from_id: 源身份 ID
        to_id: 目标身份 ID（query 时可省略，查询所有关系）
        relation_type: 关系类型（如"朋友", "同事", "家人"）
        base_weight: 基础权重（1.0~10.0）
    
    Returns:
        操作结果
    """
    try:
        if action == "add":
            if not from_id or not to_id:
                return "❌ 参数错误：add 需要 from_id 和 to_id"
            success = await identity_mgr.add_relation(from_id, to_id, relation_type, base_weight)
            if success:
                return f"✅ 已建立关系：[{from_id}] -> [{to_id}] ({relation_type}, 权重={base_weight})"
            else:
                return "❌ 建立关系失败：请检查身份 ID 是否存在"
        
        elif action == "query":
            if not from_id:
                return "❌ 参数错误：query 需要 from_id"
            relations = await identity_mgr.get_relations(from_id)
            if not relations:
                return f"[{from_id}] 没有活跃的关系"
            
            parts = [f"=== [{from_id}] 的关系网 ==="]
            for rel in relations:
                weight = rel["effective_weight"]
                tier = "高" if weight >= 3.0 else "中" if weight >= 1.0 else "低"
                parts.append(
                    f"→ [{rel['target_name']}] ({rel['relation_type']}) "
                    f"| 权重: {weight:.2f} | 热度: {tier} | "
                    f"上次提及: {rel['last_mentioned'][:10] if rel['last_mentioned'] else '从未'}"
                )
            return "\n".join(parts)
        
        elif action == "update_weight":
            if not from_id or not to_id:
                return "❌ 参数错误：update_weight 需要 from_id 和 to_id"
            success = await identity_mgr.update_relation_weight(from_id, to_id, base_weight)
            if success:
                return f"✅ 已更新关系权重：[{from_id}] -> [{to_id}] = {base_weight}"
            else:
                return "❌ 更新权重失败：关系不存在"
        
        else:
            return f"❌ 未知操作：{action}，支持的操作：add, query, update_weight"
    
    except Exception as e:
        logger.error(f"manage_relation failed: {e}")
        return f"管理关系失败: {e}"


# =============================================================
# Tool 33: get_roster — 查询名册(人物)
# 工具 33：get_roster — 查询名册(人物)
# =============================================================
@mcp.tool()
async def get_roster(name: str = None) -> str:
    """
    查询名册(人物)记录。
    
    Args:
        name: 可选，人物姓名或别名，用于精确查找。若不提供则返回所有人。
    
    Returns:
        名册人物信息，包括姓名、别名、特征、基础信息和关联记忆。
    """
    try:
        identities = await identity_mgr.list_all()
        if not identities:
            return "暂无名册(人物)记录。"
        
        if name:
            matched = []
            for ident in identities:
                meta = ident.get("metadata", {})
                if name == meta.get("name") or name in meta.get("aliases", []):
                    matched.append(ident)
            if not matched:
                return f"未找到名为'{name}'的人物。"
            identities = matched
        
        results = []
        for ident in identities:
            meta = ident.get("metadata", {})
            name = meta.get("name", "")
            aliases = ", ".join(meta.get("aliases", []))
            traits = ", ".join(meta.get("core_traits", []))
            basic_info = meta.get("basic_info", {})
            related_memories = meta.get("related_memories", [])
            pinned = meta.get("pinned", False)
            activation_count = meta.get("activation_count", 0)
            
            entry = f"[{name}]"
            if pinned:
                entry += " [钉选]"
            if aliases:
                entry += f"\n  别名: {aliases}"
            if traits:
                entry += f"\n  特征: {traits}"
            if basic_info:
                for key, value in basic_info.items():
                    entry += f"\n  {key}: {value}"
            if activation_count > 0:
                entry += f"\n  激活次数: {activation_count}"
            if related_memories:
                entry += f"\n  关联记忆: {len(related_memories)}条"
            content = ident.get("content", "")
            if content:
                entry += f"\n  描述: {content[:100]}"
            
            results.append(entry)
        
        return "=== 名册查询结果 ===\n" + "\n---\n".join(results)
    
    except Exception as e:
        logger.error(f"get_roster failed: {e}")
        return f"查询名册失败: {e}"


# =============================================================
# Tool 33: get_experiences — 获取年轮(经验)
# 工具 31：get_experiences — 获取年轮(经验)
# =============================================================
@mcp.tool()
async def get_experiences() -> str:
    """获取所有年轮(经验)记录，用于AI检索时查阅从事件中获得的经验。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        experiences = [b for b in all_buckets 
                       if b.get("metadata", {}).get("domain") and "经验" in b.get("metadata", {}).get("domain")]
        
        if not experiences:
            return "暂无年轮(经验)记录。"
        
        recent_experiences = sorted(experiences, key=lambda e: e["metadata"].get("created", ""), reverse=True)
        
        results = []
        for exp in recent_experiences:
            meta = exp.get("metadata", {})
            name = meta.get("name", exp["id"])
            content = exp.get("content", "")
            exp_type = meta.get("exp_type", "")
            apply_count = meta.get("apply_count", 0)
            last_applied = meta.get("last_applied", "")
            source = meta.get("source", "")
            
            entry = f"🌳 [{name}]"
            if exp_type:
                entry += f" #{exp_type}"
            if apply_count > 0:
                entry += f" (应用{apply_count}次)"
            if last_applied:
                entry += f" [{last_applied[:10]}]"
            if source:
                entry += f"\n  来源事件: {source}"
            entry += f"\n  {content}"
            
            results.append(entry)
        
        return "=== 年轮经验 ===\n" + "\n---\n".join(results)
    
    except Exception as e:
        logger.error(f"get_experiences failed: {e}")
        return f"获取年轮经验失败: {e}"


# =============================================================
# Tool 34: weekly_organize — 每周内容整理
# 工具 31：weekly_organize — 每周内容整理
# =============================================================
@mcp.tool()
async def smart_organize(days: int = 30, importance_drop: int = 2) -> str:
    """
    智能整理：自动识别过期记忆并批量调整权重。
    
    参数:
    - days: 超过多少天未激活视为过期（默认30天）
    - importance_drop: 权重降低幅度（默认2级，1-5之间）
    
    规则：
    - 跳过钉选(pinned)、已解决(resolved)、永久型(permanent)记忆
    - 跳过重要度已经很低(≤2)的记忆
    - 跳过最近激活(active)的记忆
    """
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        threshold_time = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        
        candidates = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            
            if meta.get("pinned"):
                continue
            if meta.get("resolved"):
                continue
            if meta.get("type") == "permanent":
                continue
            if meta.get("type") == "identity":
                continue
            if meta.get("importance", 5) <= 2:
                continue
            
            last_active = meta.get("last_active", meta.get("created", ""))
            if last_active and last_active > threshold_time:
                continue
            
            candidates.append(b)
        
        if not candidates:
            return f"✅ 没有需要调整的记忆（超过{days}天未激活的非重要记忆）"
        
        adjusted = 0
        skipped = 0
        results = []
        
        importance_drop = max(1, min(5, importance_drop))
        
        for b in candidates:
            meta = b.get("metadata", {})
            current_importance = meta.get("importance", 5)
            new_importance = max(1, current_importance - importance_drop)
            
            try:
                success = await bucket_mgr.update(
                    b["id"],
                    importance=new_importance
                )
                if success:
                    adjusted += 1
                    name = meta.get("name", b["id"])[:25]
                    results.append(f"  ↓ [{current_importance}→{new_importance}] {name}")
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        
        result = f"📋 智能整理完成\n\n"
        result += f"⏰ 时间阈值: 超过{days}天未激活\n"
        result += f"📉 权重降低: {importance_drop}级\n"
        result += f"\n✅ 已调整: {adjusted}条\n"
        if results:
            result += "\n".join(results)
        if skipped > 0:
            result += f"\n\n⚠️ 跳过: {skipped}条（更新失败）"
        
        return result
    
    except Exception as e:
        logger.error(f"smart_organize failed: {e}")
        return f"智能整理失败: {e}"


# =============================================================
# Housekeeper tools - Event Chain and Cleanup Proposal management
# 管家工具 - 事件链和清理提案管理
# =============================================================
@mcp.tool()
async def get_event_chains() -> str:
    """获取所有事件链草案，供主AI终审裁决。"""
    await housekeeper.ensure_started()
    
    try:
        chains = await housekeeper.get_event_chains()
        if not chains:
            return "暂无事件链草案。"
        
        parts = ["=== 事件链草案 ===\n"]
        for chain in chains:
            status_icon = "🔄" if chain.status == "in_progress" else "✅"
            parts.append(f"{status_icon} [{chain.chain_id}] {chain.topic}")
            parts.append(f"   状态: {chain.status}")
            parts.append(f"   摘要: {chain.summary}")
            parts.append(f"   时间线节点数: {len(chain.timeline)}")
            parts.append(f"   更新时间: {chain.updated}\n")
        
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"get_event_chains failed: {e}")
        return f"获取事件链失败: {e}"


@mcp.tool()
async def approve_event_chain(chain_id: str) -> str:
    """批准事件链，标记为已结案。chain_id=事件链ID。"""
    await housekeeper.ensure_started()
    
    try:
        success = await housekeeper.approve_chain(chain_id)
        if success:
            return f"✅ 已批准事件链: {chain_id}"
        else:
            return f"❌ 未找到事件链: {chain_id}"
    except Exception as e:
        logger.error(f"approve_event_chain failed: {e}")
        return f"批准事件链失败: {e}"


@mcp.tool()
async def get_cleanup_proposals() -> str:
    """获取待清理提案，供主AI终审裁决。"""
    await housekeeper.ensure_started()
    
    try:
        proposals = await housekeeper.get_cleanup_proposals(status="pending")
        if not proposals:
            return "暂无待清理提案。"
        
        parts = ["=== 待清理提案 ===\n"]
        for proposal in proposals:
            info = proposal.bucket_info
            parts.append(f"📋 [{proposal.proposal_id}] {info.get('name', proposal.bucket_id)}")
            parts.append(f"   原因: {proposal.reason}")
            parts.append(f"   重要度: {info.get('importance', '?')}")
            parts.append(f"   创建时间: {info.get('created', '?')}")
            parts.append(f"   最后访问: {info.get('last_accessed', '?')}\n")
        
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"get_cleanup_proposals failed: {e}")
        return f"获取清理提案失败: {e}"


@mcp.tool()
async def approve_cleanup_proposal(proposal_id: str) -> str:
    """批准清理提案，标记为待执行。proposal_id=提案ID。"""
    await housekeeper.ensure_started()
    
    try:
        success = await housekeeper.approve_proposal(proposal_id)
        if success:
            return f"✅ 已批准清理提案: {proposal_id}（将在下次衰减周期执行）"
        else:
            return f"❌ 未找到提案: {proposal_id}"
    except Exception as e:
        logger.error(f"approve_cleanup_proposal failed: {e}")
        return f"批准清理提案失败: {e}"


@mcp.tool()
async def reject_cleanup_proposal(proposal_id: str) -> str:
    """驳回清理提案，保留该记忆。proposal_id=提案ID。"""
    await housekeeper.ensure_started()
    
    try:
        success = await housekeeper.reject_proposal(proposal_id)
        if success:
            return f"✅ 已驳回清理提案: {proposal_id}（记忆已保留）"
        else:
            return f"❌ 未找到提案: {proposal_id}"
    except Exception as e:
        logger.error(f"reject_cleanup_proposal failed: {e}")
        return f"驳回清理提案失败: {e}"


@mcp.tool()
async def run_housekeeper() -> str:
    """手动触发管家管线执行（每日总结 + 时间链更新 + 冲突检测）。"""
    await housekeeper.ensure_started()
    
    logger.info("[Housekeeper] 手动触发 Daily Job...")
    
    try:
        results = await housekeeper.run_daily_job()
        parts = ["=== 管家管线执行结果 ===\n"]
        
        daily_summary = results.get("daily_summary", {})
        if "error" in daily_summary:
            parts.append(f"❌ 每日总结失败: {daily_summary['error']}")
        else:
            parts.append(f"✅ 每日总结: 处理{daily_summary.get('buckets_processed', 0)}条记忆")
            mood_tags = daily_summary.get("mood_tags", [])
            mood_level = daily_summary.get("mood_level", "")
            if mood_tags or mood_level != "neutral":
                parts.append(f"   情绪标签: {', '.join(mood_tags) if mood_tags else '无'}")
                parts.append(f"   情绪等级: {mood_level}")
        
        chain_updates = results.get("chain_updates", {})
        if "error" in chain_updates:
            parts.append(f"❌ 时间链更新失败: {chain_updates['error']}")
        else:
            parts.append(f"✅ 时间链更新: 更新{chain_updates.get('chains_updated', 0)}条链")
        
        conflicts = results.get("conflicts", {})
        if "error" in conflicts:
            parts.append(f"❌ 冲突检测失败: {conflicts['error']}")
        else:
            parts.append(f"✅ 冲突检测: 发现{conflicts.get('conflicts_found', 0)}条冲突")
        
        logger.info(f"[Housekeeper] Daily Job 完成: {results}")
        
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_housekeeper failed: {e}")
        return f"管家执行失败: {e}"


@mcp.tool()
async def check_echo_chamber() -> str:
    """检查回音壁数据库内容，查看已生成的每日/每周摘要和待审批提案。"""
    await housekeeper.ensure_started()
    
    try:
        summary = await housekeeper.review_digest()
        
        parts = ["=== 回音壁数据库检查 ===\n"]
        
        parts.append(f"\n📂 存储目录: {housekeeper.echo_chamber_dir}")
        
        parts.append(f"\n📝 待审阅摘要 ({summary['pending_digests']}条):")
        for digest in summary["digests"]:
            parts.append(f"  • [{digest['digest_id']}] {digest['digest_type']}")
            parts.append(f"    创建时间: {digest.get('created', '')}")
            parts.append(f"    已审阅: {digest.get('reviewed', False)}")
            metadata = digest.get("metadata", {})
            if metadata:
                parts.append(f"    元数据: {json.dumps(metadata, ensure_ascii=False)}")
            parts.append(f"    内容预览: {digest.get('content', '')[:150]}...")
        
        parts.append(f"\n📋 待审批提案 ({summary['pending_actions']}条):")
        for action in summary["actions"]:
            parts.append(f"  • [{action['action_id']}] {action['action_type']}")
            parts.append(f"    状态: {action.get('status', '')}")
            parts.append(f"    创建时间: {action.get('created', '')}")
            data = action.get("data", {})
            if data:
                parts.append(f"    数据: {json.dumps(data, ensure_ascii=False)[:200]}...")
        
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"check_echo_chamber failed: {e}")
        return f"检查回音壁失败: {e}"


@mcp.tool()
async def run_weekly_housekeeper() -> str:
    """手动触发每周管家管线（事件链合并 + 清理提案生成）。"""
    await housekeeper.ensure_started()
    
    try:
        results = await housekeeper.run_weekly_job()
        parts = ["=== 每周管家管线执行结果 ===\n"]
        
        chain_merge = results.get("chain_merge", {})
        if "error" in chain_merge:
            parts.append(f"❌ 事件链合并失败: {chain_merge['error']}")
        else:
            parts.append(f"✅ 事件链合并: 合并{chain_merge.get('chains_merged', 0)}条链")
        
        cleanup_scan = results.get("cleanup_scan", {})
        if "error" in cleanup_scan:
            parts.append(f"❌ 清理扫描失败: {cleanup_scan['error']}")
        else:
            parts.append(f"✅ 清理扫描: 生成{cleanup_scan.get('proposals_created', 0)}条清理提案")
        
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_weekly_housekeeper failed: {e}")
        return f"每周管家执行失败: {e}"


@mcp.tool()
async def review_digest() -> str:
    """审阅回音壁中的待办提案（每日/每周摘要 + 清理提案），行使主AI最高裁决权。"""
    await housekeeper.ensure_started()
    
    try:
        summary = await housekeeper.review_digest()
        
        if summary["pending_digests"] == 0 and summary["pending_actions"] == 0:
            return "回音壁中暂无待办提案。"
        
        parts = ["=== 回音壁审阅报告 ===\n"]
        
        if summary["pending_digests"] > 0:
            parts.append(f"\n📝 待审阅摘要 ({summary['pending_digests']}条):")
            for digest in summary["digests"]:
                parts.append(f"  • [{digest['digest_id']}] {digest['digest_type']}")
                
                metadata = digest.get("metadata", {})
                mood_tags = metadata.get("mood_tags", [])
                mood_level = metadata.get("mood_level", "")
                
                if mood_tags or mood_level != "neutral":
                    mood_display = {
                        "low": "🔴 情绪低落",
                        "slightly_low": "🟡 情绪偏低",
                        "high": "🟢 情绪高涨",
                        "slightly_high": "🟢 情绪较好",
                        "neutral": "",
                    }
                    mood_text = mood_display.get(mood_level, "")
                    tag_text = ", ".join(mood_tags) if mood_tags else ""
                    
                    if mood_text:
                        parts.append(f"    {mood_text}")
                    if tag_text:
                        parts.append(f"    情绪标签: {tag_text}")
                
                parts.append(f"    {digest['content'][:200]}...")
        
        if summary["pending_actions"] > 0:
            parts.append(f"\n📋 待审批提案 ({summary['pending_actions']}条):")
            for action in summary["actions"]:
                data = action.get("data", {})
                action_type = action["action_type"]
                
                if action_type == "conflict":
                    parts.append(f"  ⚠️ [{action['action_id']}] 记忆冲突 ({data.get('conflict_type', 'unknown')})")
                    parts.append(f"    {data.get('conflict_reason', '')}")
                    parts.append(f"    旧记录: [{data.get('old_metadata', {}).get('created', '')[:10]}] {data.get('old_content', '')[:100]}")
                    parts.append(f"    新记录: [{data.get('new_metadata', {}).get('created', '')[:10]}] {data.get('new_content', '')[:100]}")
                elif action_type == "cleanup":
                    parts.append(f"  • [{action['action_id']}] 清理提案")
                    parts.append(f"    Bucket: {data.get('bucket_id', '')}")
                    parts.append(f"    Reason: {data.get('reason', '')}")
                else:
                    parts.append(f"  • [{action['action_id']}] {action_type}")
        
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"review_digest failed: {e}")
        return f"审阅失败: {e}"


@mcp.tool()
async def approve_action(action_id: str) -> str:
    """批准回音壁中的待办提案。action_id=提案ID。"""
    await housekeeper.ensure_started()
    
    try:
        success = await housekeeper.approve_action(action_id)
        if success:
            return f"✅ 已批准提案: {action_id}"
        else:
            return f"❌ 未找到提案: {action_id}"
    except Exception as e:
        logger.error(f"approve_action failed: {e}")
        return f"批准失败: {e}"


@mcp.tool()
async def reject_action(action_id: str) -> str:
    """驳回回音壁中的待办提案。action_id=提案ID。"""
    await housekeeper.ensure_started()
    
    try:
        success = await housekeeper.reject_action(action_id)
        if success:
            return f"✅ 已驳回提案: {action_id}"
        else:
            return f"❌ 未找到提案: {action_id}"
    except Exception as e:
        logger.error(f"reject_action failed: {e}")
        return f"驳回失败: {e}"


@mcp.tool()
async def inject_context(user_input: str = "") -> str:
    """
    静默预处理中间件：自动检索相关记忆并注入上下文。
    在用户发送消息前调用，将检索到的背景记忆以<context>结构注入到Prompt头部。
    user_input=用户输入的消息内容。
    """
    await decay_engine.ensure_started()
    await housekeeper.ensure_started()
    
    logger.info(f"[Middleware] 拦截用户输入: {user_input[:50]}...")
    
    context_parts = []
    
    try:
        chains = await housekeeper.get_event_chains()
        logger.info(f"[Middleware] 检索到 {len(chains)} 条事件链")
        
        relevant_chains = []
        
        for chain in chains:
            if chain.status == "resolved":
                continue
            
            chain_text = f"{chain.topic} {chain.summary}"
            chain_text += " ".join(node.get("content_preview", "") for node in chain.timeline)
            
            if HAS_RAPIDFUZZ:
                try:
                    from rapidfuzz import fuzz
                    similarity = fuzz.ratio(user_input, chain_text)
                    logger.debug(f"[Middleware] 事件链 '{chain.topic}' 相似度: {similarity}")
                    if similarity >= 40:
                        relevant_chains.append((chain, similarity))
                except ImportError:
                    if user_input and any(keyword in chain.topic for keyword in user_input[:20]):
                        relevant_chains.append((chain, 100))
            else:
                if user_input and any(keyword in chain.topic for keyword in user_input[:20]):
                    relevant_chains.append((chain, 100))
        
        relevant_chains.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"[Middleware] 匹配到 {len(relevant_chains)} 条相关事件链")
        
        for chain, score in relevant_chains[:3]:
            logger.info(f"[Middleware] 注入事件链: {chain.topic} (相似度: {score})")
            context_parts.append(f"【事件链】{chain.topic}")
            context_parts.append(f"   状态: {chain.status}")
            context_parts.append(f"   摘要: {chain.summary}")
            for node in chain.timeline[-3:]:
                context_parts.append(f"   [{node.get('timestamp', '')[:10]}] {node.get('content_preview', '')}")
            context_parts.append("")
    except Exception as e:
        logger.warning(f"Event chain context injection failed: {e}")
    
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        recent_feels = [
            b for b in all_buckets
            if b["metadata"].get("type") == "feel"
        ]
        recent_feels.sort(key=lambda x: x["metadata"].get("created", ""), reverse=True)
        
        logger.info(f"[Middleware] 检索到 {len(recent_feels)} 条感觉状态记忆")
        
        for feel in recent_feels[:3]:
            meta = feel["metadata"]
            context_metadata = meta.get("context_metadata", {})
            event_context = context_metadata.get("event_context", "")
            
            feel_entry = f"【感觉状态】[{meta.get('created', '')[:10]}]"
            if event_context:
                feel_entry += f" {event_context}"
            feel_entry += f"\n   {feel['content'][:100]}"
            context_parts.append(feel_entry)
            logger.info(f"[Middleware] 注入感觉状态: {feel['content'][:50]}")
    except Exception as e:
        logger.warning(f"Feel context injection failed: {e}")
    
    try:
        mood_warning = await _check_ongoing_mood_trend()
        if mood_warning:
            context_parts.insert(0, mood_warning)
    except Exception as e:
        logger.warning(f"Mood trend check failed: {e}")
    
    if context_parts:
        return "<context>\n" + "\n".join(context_parts) + "</context>"
    else:
        return "<context></context>"


async def _check_ongoing_mood_trend() -> str | None:
    """
    Check if there's an ongoing low mood trend over consecutive days.
    If detected, return a warning message for context injection.
    """
    try:
        digests = await housekeeper.echo_chamber.get_pending_digests(digest_type="daily")
    except Exception:
        try:
            digests = await housekeeper.echo_chamber.get_pending_digests(digest_type="all")
        except Exception as e:
            logger.warning(f"Failed to get digests for mood check: {e}")
            return None
    
    low_mood_days = []
    for digest in digests:
        metadata = digest.get("metadata", {})
        mood_level = metadata.get("mood_level", "")
        
        if mood_level in ("low", "slightly_low"):
            low_mood_days.append({
                "date": digest.get("digest_id", "")[-8:],
                "tags": metadata.get("mood_tags", []),
                "level": mood_level,
            })
    
    if len(low_mood_days) >= 2:
        tag_counts = {}
        for day in low_mood_days:
            for tag in day["tags"]:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        
        most_common_tag = max(tag_counts, key=tag_counts.get) if tag_counts else "情绪低落"
        
        tag_display = {
            "anxious": "焦虑",
            "unwell": "身体不适",
            "sad": "难过",
            "angry": "生气",
            "happy": "开心",
        }
        
        display_tag = tag_display.get(most_common_tag, most_common_tag)
        
        return f"【情绪提示】检测到连续{len(low_mood_days)}天{display_tag}，请提高耐心和陪伴感"
    
    return None


async def weekly_organize() -> str:
    """每周内容整理：对本周新增的记忆进行整理和总结（仅生成报告，不调整权重）。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        one_week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        recent_buckets = [
            b for b in all_buckets
            if b["metadata"].get("created", "") > one_week_ago
            and b["metadata"].get("type") not in ("permanent", "identity")
        ]
        
        if not recent_buckets:
            return "本周没有新增记忆，无需整理。"
        
        by_domain = {}
        for b in recent_buckets:
            domains = b["metadata"].get("domain", ["未分类"])
            for d in domains:
                if d not in by_domain:
                    by_domain[d] = []
                by_domain[d].append(b)
        
        result = "📋 每周内容整理报告\n\n"
        result += "=" * 50 + "\n\n"
        result += f"📅 统计周期: 最近7天\n"
        result += f"📊 新增记忆: {len(recent_buckets)}条\n\n"
        
        for domain, buckets in sorted(by_domain.items(), key=lambda x: len(x[1]), reverse=True):
            result += f"--- {domain} ({len(buckets)}条) ---\n"
            for b in buckets:
                name = b["metadata"].get("name", b["id"])[:30]
                importance = b["metadata"].get("importance", 5)
                created = b["metadata"].get("created", "")[:10]
                result += f"  [{importance}] {name} [{created}]\n"
            result += "\n"
        
        anchors = await bucket_mgr.get_anchors()
        recent_anchors = [a for a in anchors if a.get("created", "") > one_week_ago]
        if recent_anchors:
            active_count = sum(1 for a in recent_anchors if a.get("is_active", True))
            result += f"--- 本周锚点 ({len(recent_anchors)}个, 生效{active_count}个) ---\n"
            for a in recent_anchors:
                name = a.get("name", a.get("id", ""))
                is_active = a.get("is_active", True)
                atype = a.get("anchor_type", "dynamic")
                triggers = a.get("triggers", [])
                status = "✅" if is_active else "💤"
                type_tag = "🔒" if atype == "static" else "⚡"
                trigger_str = ", ".join(triggers[:2]) if triggers else "无"
                result += f"  {status}{type_tag} [{name}] 触发: {trigger_str}\n"
            result += "\n"
        
        candlesticks = await bucket_mgr.get_candlesticks()
        recent_candlesticks = [c for c in candlesticks if c.get("created", "") > one_week_ago]
        if recent_candlesticks:
            result += f"--- 本周烛台备忘录 ({len(recent_candlesticks)}条) ---\n"
            for c in recent_candlesticks:
                title = c.get("metadata", {}).get("title", "")
                result += f"  🕯️ {title}\n"
            result += "\n"
        
        recent_experiences = [
            b for b in all_buckets
            if b["metadata"].get("type") == "experience"
            and b["metadata"].get("created", "") > one_week_ago
        ]
        if recent_experiences:
            result += f"--- 本周年轮经验 ({len(recent_experiences)}条) ---\n"
            for exp in recent_experiences:
                name = exp["metadata"].get("name", exp["id"])[:30]
                exp_type = exp["metadata"].get("exp_type", "")
                result += f"  🌳 {name}"
                if exp_type:
                    result += f" #{exp_type}"
                result += "\n"
            result += "\n"
        
        result += "=" * 50 + "\n"
        result += "💡 建议：使用 ai_analyze(task=summarize) 对重要记忆进行深度总结"
        
        return result
    
    except Exception as e:
        logger.error(f"weekly_organize failed: {e}")
        return f"每周整理失败: {e}"


@mcp.tool()
async def manage_record(action: str, record_type: str = "", record_id: str = "", name: str = "", description: str = "", content: str = "", detail: str = "", text: str = "", exp_type: str = "", title: str = "", tags: str = "", importance: int = -1, source: str = "", bucket_id: str = "", relationships: str = "", triggers: str = "") -> str:
    """通用记录管理工具，替代identity_*/pattern_*/candlestick_*/experience_*等CRUD工具。
    action: create/update/get/list/delete/apply
    record_type: identity/roster/pattern/candlestick/experience/annual_ring
    record_id: 记录ID(仅get/update/delete/apply需要)
    create参数: name/description/relationships(identity), content/detail/text/title/exp_type(experience), name/description/triggers(pattern), content/bucket_id/title(candlestick), content/detail/text/title(annual_ring)
    update参数: content/tags/importance"""
    try:
        if action not in ["create", "update", "get", "list", "delete", "apply"]:
            return f"未知操作: {action}"
        
        if action == "create":
            if record_type == "identity":
                rel_list = [r.strip() for r in relationships.split(",")] if relationships else []
                if not name:
                    return "请提供姓名"
                identity_id = await identity_mgr.create(
                    name=name,
                    content=description,
                    relationships=rel_list,
                )
                return f"👤 身份档案已创建 → {identity_id}"
            elif record_type == "pattern":
                success = await bucket_mgr.save_pattern(name, description, triggers)
                return f"📐 行为模式已创建 → {success['id']}" if success else "创建失败"
            elif record_type == "candlestick":
                success = await bucket_mgr.save_candlestick(content, bucket_id, title)
                return f"🕯️ 烛台已记录 → {success['id']}" if success else "记录失败"
            elif record_type == "experience":
                content_val = content or detail or text
                title_val = title or name
                if not content_val:
                    return "请提供经验内容"
                bucket_id = await bucket_mgr.create(
                    content=content_val,
                    tags=[],
                    importance=8,
                    domain=["经验"],
                    name=title_val or "未命名经验",
                    bucket_type="permanent",
                )
                await bucket_mgr.update(
                    bucket_id,
                    exp_type=exp_type,
                    source=source,
                    source_bucket_ids=[],
                    apply_count=0,
                    last_applied="",
                    hit_count=0,
                    last_hit="",
                )
                return f"📚 经验已创建 → {bucket_id}"
            elif record_type == "annual_ring":
                content_val = content or detail or text
                title_val = title or name
                if not content_val:
                    return "请提供年轮内容"
                bucket_id = await bucket_mgr.create(
                    content=content_val,
                    tags=[],
                    importance=8,
                    domain=["年轮"],
                    name=title_val or "未命名年轮",
                    bucket_type="permanent",
                )
                return f"🌳 年轮已记录 → {bucket_id}"
            return f"不支持创建 {record_type} 类型"
        
        elif action == "list":
            if record_type == "identity" or record_type == "roster":
                identities = await identity_mgr.list_all()
                return "\n".join([f"[{i['id']}] {i['metadata'].get('name', '')}" for i in identities]) if identities else "暂无身份档案"
            elif record_type == "pattern":
                patterns = await bucket_mgr.get_patterns()
                return "\n".join([f"[{p['id']}] {p['metadata'].get('name', '')}" for p in patterns]) if patterns else "暂无行为模式"
            elif record_type == "candlestick":
                candlesticks = await bucket_mgr.get_candlesticks()
                return "\n".join([f"[{c['id']}] {c['metadata'].get('title', '')[:30]}" for c in candlesticks]) if candlesticks else "暂无烛台记录"
            elif record_type == "experience":
                all_buckets = await bucket_mgr.list_all(include_archive=False)
                experiences = [b for b in all_buckets 
                               if b.get("metadata", {}).get("domain") and "经验" in b.get("metadata", {}).get("domain")]
                
                exp_type_filter = kwargs.get("exp_type", "")
                if exp_type_filter:
                    experiences = [e for e in experiences if e.get("metadata", {}).get("exp_type") == exp_type_filter]
                
                if not experiences:
                    return "暂无经验"
                
                results = []
                for e in experiences:
                    meta = e.get("metadata", {})
                    name = meta.get("name", e["id"])
                    exp_type = meta.get("exp_type", "")
                    apply_count = meta.get("apply_count", 0)
                    entry = f"[{e['id']}] {name}"
                    if exp_type:
                        entry += f" #{exp_type}"
                    if apply_count > 0:
                        entry += f" (应用{apply_count}次)"
                    results.append(entry)
                
                return "\n".join(results)
            elif record_type == "annual_ring":
                all_buckets = await bucket_mgr.list_all(include_archive=False)
                rings = [b for b in all_buckets 
                         if b.get("metadata", {}).get("domain") and "年轮" in b.get("metadata", {}).get("domain")]
                
                if not rings:
                    return "暂无年轮记录"
                
                return "\n".join([f"[{r['id']}] {r['metadata'].get('name', r['id'])}" for r in rings])
            return f"不支持列出 {record_type} 类型"
        
        elif action == "get":
            if not record_id:
                return "请提供record_id"
            if record_type in ["identity", "roster", "pattern", "candlestick", "experience", "annual_ring"]:
                record = await bucket_mgr.get(record_id)
                if record:
                    meta = record.get("metadata", {})
                    return f"ID: {record_id}\n名称: {meta.get('name', '')}\n内容: {record.get('content', '')[:200]}"
                return f"未找到记录: {record_id}"
            return f"不支持获取 {record_type} 类型"
        
        elif action == "update":
            if not record_id:
                return "请提供record_id"
            if record_type in ["identity", "roster", "pattern", "candlestick", "experience", "annual_ring"]:
                record = await bucket_mgr.get(record_id)
                if not record:
                    return f"未找到记录: {record_id}"
                content = kwargs.get("content", record.get("content", ""))
                meta = {**record.get("metadata", {}), **kwargs.get("meta", {})}
                success = await bucket_mgr.update(record_id, content, meta)
                return f"已更新 → {record_id}" if success else "更新失败"
            return f"不支持更新 {record_type} 类型"
        
        elif action == "delete":
            if not record_id:
                return "请提供record_id"
            if record_type in ["identity", "roster", "pattern", "candlestick", "experience", "annual_ring"]:
                success = await bucket_mgr.delete(record_id)
                return f"已删除 → {record_id}" if success else "删除失败"
            return f"不支持删除 {record_type} 类型"
        
        elif action == "apply":
            if not record_id:
                return "请提供record_id"
            if record_type == "experience":
                exp = await bucket_mgr.get(record_id)
                if not exp:
                    return f"未找到经验: {record_id}"
                meta = exp.get("metadata", {})
                meta["apply_count"] = meta.get("apply_count", 0) + 1
                meta["last_applied"] = datetime.now().isoformat()
                success = await bucket_mgr.update(record_id, exp.get("content", ""), meta)
                return f"经验已应用 → {record_id} | 次数: {meta['apply_count']}" if success else "应用失败"
            elif record_type == "annual_ring":
                ring = await bucket_mgr.get(record_id)
                if not ring:
                    return f"未找到年轮: {record_id}"
                meta = ring.get("metadata", {})
                meta["apply_count"] = meta.get("apply_count", 0) + 1
                meta["last_applied"] = datetime.now().isoformat()
                success = await bucket_mgr.update(record_id, ring.get("content", ""), meta)
                return f"年轮已应用 → {record_id} | 次数: {meta['apply_count']}" if success else "应用失败"
            return f"不支持应用 {record_type} 类型"
        
        return f"操作失败: {action} {record_type}"
    except Exception as e:
        logger.error(f"manage_record failed: {e}")
        return f"操作失败: {e}"


@mcp.tool()
async def manage_relation(action: str, bucket_id: str = "", target_id: str = "", position: int = 0, impact: int = 0, duration: int = 0, emotional_intensity: int = 0, recurrence: int = 0, interconnectedness: int = 0) -> str:
    """通用关联管理工具，替代link_buckets/set_bucket_parent/chain_events/rate_importance。
    action: link(关联)/parent(父子)/chain(事件链)/importance(重要度)
    bucket_id: 源桶ID
    target_id: 目标桶ID(link/parent/chain需要)
    position: 事件链位置(chain模式)
    impact/duration/emotional_intensity/recurrence/interconnectedness: 重要度维度0~10(importance模式)"""
    try:
        if action == "link":
            if not bucket_id or not target_id:
                return "请提供bucket_id和target_id"
            success = await bucket_mgr.add_related_bucket(bucket_id, target_id)
            return f"已建立关联 → {bucket_id} ↔ {target_id}" if success else "建立关联失败"
        
        elif action == "parent":
            if not bucket_id or not target_id:
                return "请提供bucket_id(子桶)和target_id(父桶)"
            success = await bucket_mgr.set_parent_bucket(bucket_id, target_id)
            return f"已建立层级 → {target_id} ⊃ {bucket_id}" if success else "建立层级失败"
        
        elif action == "chain":
            if not bucket_id or not target_id:
                return "请提供bucket_id和target_id"
            success = await bucket_mgr.add_event_sequence(bucket_id, target_id, position)
            return f"已添加到事件链 → {bucket_id} → {target_id}" if success else "添加事件链失败"
        
        elif action == "importance":
            if not bucket_id:
                return "请提供bucket_id"
            details = {
                "impact": max(0, min(10, impact)),
                "duration": max(0, min(10, duration)),
                "emotional_intensity": max(0, min(10, emotional_intensity)),
                "recurrence": max(0, min(10, recurrence)),
                "interconnectedness": max(0, min(10, interconnectedness)),
            }
            success = await bucket_mgr.update_importance_details(bucket_id, details)
            if success:
                bucket = await bucket_mgr.get(bucket_id)
                importance = bucket.get("metadata", {}).get("importance", 5)
                return f"重要度评估完成 → {bucket_id} | 综合: {importance}/10"
            return f"重要度评估失败: {bucket_id}"
        
        return f"未知操作: {action}"
    except Exception as e:
        logger.error(f"manage_relation failed: {e}")
        return f"操作失败: {e}"


@mcp.tool()
async def query_memory(query: str = "", mode: str = "search", domain: str = "", valence: float = -1, arousal: float = -1, importance_min: int = -1, max_results: int = 10, days: int = 7, detail_level: str = "medium") -> str:
    """通用记忆查询工具，替代breath/pulse/memory_directory/summarize_recent_events。
    mode: search(关键词搜索)/float(自动浮现)/status(系统状态)/directory(目录摘要)/recent(最近事件)
    query: 搜索关键词(search模式需要)
    domain/valence/arousal/importance_min/max_results: breath参数
    days: 最近事件天数(recent模式)
    detail_level: 目录详细程度(brief/medium/full, directory模式)"""
    try:
        if mode == "search":
            return await breath(query=query, domain=domain, valence=valence, arousal=arousal, importance_min=importance_min, max_results=max_results)
        
        elif mode == "float":
            return await breath(query="", brief=True)
        
        elif mode == "status":
            return await pulse()
        
        elif mode == "directory":
            return await memory_directory(detail_level=detail_level)
        
        elif mode == "recent":
            return await summarize_recent_events(days=days)
        
        return f"未知模式: {mode}"
    except Exception as e:
        logger.error(f"query_memory failed: {e}")
        return f"查询失败: {e}"


@mcp.tool()
async def ai_analyze(task: str, bucket_id: str = "", query: str = "") -> str:
    """AI分析工具，替代ai_link_memory/ai_find_related/ai_build_event_chain/ai_summarize_memory/ai_classify_memory。
    task: link(建立关联)/find(查找相关)/chain(构建事件链)/summarize(总结记忆)/classify(分类记忆)
    bucket_id: 记忆桶ID(link/summarize/classify需要)
    query: 搜索关键词(find模式需要)"""
    try:
        if not dehydrator or not dehydrator.api_available:
            return "需要配置脱水API才能使用AI功能。"
        
        if task == "link":
            return await ai_link_memory(bucket_id)
        
        elif task == "find":
            return await ai_find_related(query=query, bucket_id=bucket_id)
        
        elif task == "chain":
            return await ai_build_event_chain(bucket_id=bucket_id)
        
        elif task == "summarize":
            return await ai_summarize_memory(bucket_id)
        
        elif task == "classify":
            return await ai_classify_memory(bucket_id)
        
        return f"未知任务: {task}"
    except Exception as e:
        logger.error(f"ai_analyze failed: {e}")
        return f"AI分析失败: {e}"


async def ai_link_memory(bucket_id: str) -> str:
    """AI自动分析并建立记忆关联。bucket_id为记忆桶ID，AI会分析其内容并找到语义相关的记忆桶建立关联。"""
    try:
        if not dehydrator or not dehydrator.api_available:
            return "需要配置脱水API才能使用AI关联功能。"
        
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket:
            return f"未找到记忆桶: {bucket_id}"
        
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        if len(all_buckets) < 2:
            return "记忆库中记忆不足，无法建立关联。"
        
        other_buckets = [b for b in all_buckets if b["id"] != bucket_id]
        if not other_buckets:
            return "没有其他记忆桶可关联。"
        
        bucket_info = bucket.get("metadata", {})
        content = bucket.get("content", "")[:500]
        name = bucket_info.get("name", "")
        tags = bucket_info.get("tags", [])
        
        candidates = []
        for b in other_buckets[:20]:
            meta = b.get("metadata", {})
            candidates.append({
                "id": b["id"],
                "name": meta.get("name", ""),
                "tags": meta.get("tags", []),
                "type": meta.get("type", ""),
            })
        
        prompt = f"""分析以下记忆桶，找出语义上最相关的其他记忆桶。

当前记忆桶:
ID: {bucket_id}
名称: {name}
标签: {', '.join(tags)}
内容摘要: {content}

候选记忆桶列表:
{_json_lib.dumps(candidates, ensure_ascii=False)}

请返回与当前记忆桶最相关的记忆桶ID列表，按相关性从高到低排序。
只返回JSON格式：{{"related_ids": ["id1", "id2", ...], "reason": "关联原因"}}
最多返回5个相关记忆桶。"""
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个记忆关联分析助手，擅长分析记忆内容并建立语义关联。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.2,
        )
        
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        
        result = _json_lib.loads(raw)
        related_ids = result.get("related_ids", [])
        reason = result.get("reason", "")
        
        if not related_ids:
            return f"未找到相关记忆桶。原因: {reason}"
        
        links_created = 0
        for rel_id in related_ids[:3]:
            success = await bucket_mgr.add_related_bucket(bucket_id, rel_id)
            if success:
                links_created += 1
        
        return f"已建立 {links_created} 个关联 → {bucket_id} ↔ {related_ids[:3]} | 原因: {reason}"
    except Exception as e:
        logger.error(f"ai_link_memory failed: {e}")
        return f"AI关联失败: {e}"


async def ai_find_related(query: str = "", bucket_id: str = "") -> str:
    """AI查找相关记忆。可传入query关键词或bucket_id，AI会找到语义相关的记忆桶。"""
    try:
        if not dehydrator or not dehydrator.api_available:
            return "需要配置脱水API才能使用AI查找功能。"
        
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        if not all_buckets:
            return "记忆库为空。"
        
        if bucket_id:
            bucket = await bucket_mgr.get(bucket_id)
            if not bucket:
                return f"未找到记忆桶: {bucket_id}"
            meta = bucket.get("metadata", {})
            query_text = f"{meta.get('name', '')} {meta.get('tags', [])} {bucket.get('content', '')[:300]}"
        else:
            query_text = query
        
        if not query_text:
            return "请提供查询关键词或记忆桶ID。"
        
        candidates = []
        for b in all_buckets[:30]:
            if bucket_id and b["id"] == bucket_id:
                continue
            meta = b.get("metadata", {})
            candidates.append({
                "id": b["id"],
                "name": meta.get("name", ""),
                "tags": meta.get("tags", []),
                "type": meta.get("type", ""),
                "content": b.get("content", "")[:200],
            })
        
        prompt = f"""根据查询内容，找出语义相关的记忆桶。

查询内容: {query_text}

候选记忆桶列表:
{_json_lib.dumps(candidates, ensure_ascii=False)}

请返回最相关的记忆桶ID列表，按相关性从高到低排序。
只返回JSON格式：{{"related_ids": ["id1", "id2", ...], "descriptions": ["描述1", "描述2", ...]}}
最多返回8个相关记忆桶，并为每个桶提供简短描述。"""
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个记忆检索助手，擅长根据语义查找相关记忆。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
            temperature=0.2,
        )
        
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        
        result = _json_lib.loads(raw)
        related_ids = result.get("related_ids", [])
        descriptions = result.get("descriptions", [])
        
        if not related_ids:
            return "未找到相关记忆。"
        
        result_text = "找到相关记忆:\n"
        for i, (rid, desc) in enumerate(zip(related_ids, descriptions)):
            result_text += f"  {i+1}. [{rid}] {desc}\n"
        
        return result_text
    except Exception as e:
        logger.error(f"ai_find_related failed: {e}")
        return f"AI查找失败: {e}"


async def ai_build_event_chain(bucket_id: str = "") -> str:
    """AI构建事件链。bucket_id为起始记忆桶，AI会分析时间顺序并构建事件序列。"""
    try:
        if not dehydrator or not dehydrator.api_available:
            return "需要配置脱水API才能使用AI构建事件链功能。"
        
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        event_buckets = [b for b in all_buckets if b.get("metadata", {}).get("type") == "event"]
        
        if len(event_buckets) < 2:
            return "事件记忆不足，无法构建事件链。"
        
        events_info = []
        for b in event_buckets[:20]:
            meta = b.get("metadata", {})
            events_info.append({
                "id": b["id"],
                "name": meta.get("name", ""),
                "created": meta.get("created", ""),
                "tags": meta.get("tags", []),
                "content": b.get("content", "")[:200],
            })
        
        prompt = f"""分析以下事件记忆，按时间顺序和逻辑关系构建事件链。

事件列表:
{_json_lib.dumps(events_info, ensure_ascii=False)}

{'' if not bucket_id else f'起始事件ID: {bucket_id}'}

请返回事件链序列，按时间顺序排列。
只返回JSON格式：{{"event_chain": ["id1", "id2", ...], "description": "事件链描述"}}
最多返回10个事件。"""
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个事件序列分析助手，擅长分析事件的时间顺序和逻辑关系。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.2,
        )
        
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        
        result = _json_lib.loads(raw)
        event_chain = result.get("event_chain", [])
        description = result.get("description", "")
        
        if not event_chain:
            return f"无法构建事件链。描述: {description}"
        
        if bucket_id:
            for i, event_id in enumerate(event_chain):
                if i > 0:
                    prev_id = event_chain[i-1]
                    await bucket_mgr.add_event_sequence(prev_id, event_id)
        else:
            for i, event_id in enumerate(event_chain):
                if i > 0:
                    prev_id = event_chain[i-1]
                    await bucket_mgr.add_event_sequence(prev_id, event_id)
        
        chain_text = " → ".join(event_chain)
        return f"事件链已构建: {chain_text}\n描述: {description}"
    except Exception as e:
        logger.error(f"ai_build_event_chain failed: {e}")
        return f"AI构建事件链失败: {e}"


async def ai_summarize_memory(bucket_id: str) -> str:
    """AI总结记忆内容。bucket_id为记忆桶ID，AI会生成简洁的摘要和关键词。"""
    try:
        if not dehydrator or not dehydrator.api_available:
            return "需要配置脱水API才能使用AI总结功能。"
        
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket:
            return f"未找到记忆桶: {bucket_id}"
        
        meta = bucket.get("metadata", {})
        content = bucket.get("content", "")
        
        prompt = f"""请总结以下记忆内容。

记忆ID: {bucket_id}
名称: {meta.get('name', '')}
标签: {', '.join(meta.get('tags', []))}
创建时间: {meta.get('created', '')}
类型: {meta.get('type', '')}

内容:
{content}

请返回:
1. 一句话摘要(20-50字)
2. 5个关键词
3. 情感倾向(正面/中性/负面)

只返回JSON格式：{{"summary": "摘要", "keywords": ["关键词1", ...], "sentiment": "情感倾向"}}"""
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个记忆总结助手，擅长提炼记忆的核心内容。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        
        result = _json_lib.loads(raw)
        summary = result.get("summary", "")
        keywords = result.get("keywords", [])
        sentiment = result.get("sentiment", "")
        
        return f"记忆摘要:\n\n📝 {summary}\n\n🔑 关键词: {', '.join(keywords)}\n\n💭 情感倾向: {sentiment}"
    except Exception as e:
        logger.error(f"ai_summarize_memory failed: {e}")
        return f"AI总结失败: {e}"


async def ai_classify_memory(bucket_id: str) -> str:
    """AI分类记忆。bucket_id为记忆桶ID，AI会分析内容并给出分类建议。"""
    try:
        if not dehydrator or not dehydrator.api_available:
            return "需要配置脱水API才能使用AI分类功能。"
        
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket:
            return f"未找到记忆桶: {bucket_id}"
        
        meta = bucket.get("metadata", {})
        content = bucket.get("content", "")
        
        prompt = f"""请对以下记忆进行分类分析。

记忆ID: {bucket_id}
名称: {meta.get('name', '')}
当前标签: {', '.join(meta.get('tags', []))}
当前类型: {meta.get('type', '')}
当前主题域: {', '.join(meta.get('domain', []))}

内容:
{content}

请分析并返回:
1. 推荐类型(event/feel/identity/pattern/permanent)
2. 推荐主题域(工作/生活/学习/健康/娱乐/人际关系等)
3. 推荐标签(3-5个)
4. 推荐重要度(1-10)

只返回JSON格式：{{"type": "类型", "domain": ["主题1", ...], "tags": ["标签1", ...], "importance": 5}}"""
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个记忆分类助手，擅长对记忆进行分类和打标。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.2,
        )
        
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        
        result = _json_lib.loads(raw)
        new_type = result.get("type", "")
        new_domain = result.get("domain", [])
        new_tags = result.get("tags", [])
        new_importance = result.get("importance", 5)
        
        if new_type or new_domain or new_tags:
            meta_update = {}
            if new_type:
                meta_update["type"] = new_type
            if new_domain:
                meta_update["domain"] = new_domain
            if new_tags:
                meta_update["tags"] = new_tags
            if new_importance:
                meta_update["importance"] = new_importance
            
            await bucket_mgr.update(bucket_id, content, {**meta, **meta_update})
        
        return f"分类结果已更新:\n\n📁 类型: {new_type}\n\n🏷️ 主题域: {', '.join(new_domain)}\n\n🔖 标签: {', '.join(new_tags)}\n\n⭐ 重要度: {new_importance}/10"
    except Exception as e:
        logger.error(f"ai_classify_memory failed: {e}")
        return f"AI分类失败: {e}"


# =============================================================
# Tool 24: summarize_recent_events — Summarize recent memory events
# 工具 24：summarize_recent_events — 概括最近的记忆事件
# =============================================================
@mcp.tool()
async def ai_manage(request: str) -> str:
    """AI管家——智能分析用户需求并自动调用合适的工具。传入自然语言请求,AI管家会理解意图并执行相应操作。支持多轮工具调用、任务总结和全局操控。"""
    try:
        if not request or not request.strip():
            return "请输入您的需求。"
        
        if not dehydrator or not dehydrator.api_available:
            return "AI管家需要配置脱水API才能工作，请先在设置中配置API。"
        
        tools_info = [
            {"name": "breath", "description": "检索/浮现记忆，支持关键词搜索、按主题域/情感坐标/重要度筛选"},
            {"name": "hold", "description": "存储单条记忆，自动打标+合并，支持设置重要度、钉选、情感坐标"},
            {"name": "grow", "description": "日记归档，自动拆分为多个记忆桶"},
            {"name": "trace", "description": "修改记忆元数据或内容，支持沉底/激活/钉选/删除等操作"},
            {"name": "dream", "description": "读取最近新增的记忆桶，供自省"},
            {"name": "smart_organize", "description": "智能整理：自动识别过期记忆并批量降低权重（days=30, importance_drop=2）"},
            {"name": "weekly_organize", "description": "每周内容整理：生成本周新增记忆报告（仅报告，不调整权重）"},
            {"name": "manage_record", "description": "通用记录管理：action(create/update/get/list/delete/apply), record_type(identity/roster/pattern/candlestick/experience/annual_ring), record_id"},
            {"name": "manage_relation", "description": "通用关联管理：action(link/parent/chain/importance), bucket_id, target_id"},
            {"name": "manage_identity_relation", "description": "管理身份关系：action(add/query/update_weight), from_id, to_id, relation_type, base_weight"},
            {"name": "query_memory", "description": "通用记忆查询：mode(search/float/status/directory/recent), query"},
            {"name": "ai_analyze", "description": "AI分析工具：task(link/find/chain/summarize/classify), bucket_id, query"},
            {"name": "get_anchors", "description": "获取行为与情绪锚点（触发词+情绪基调+行为禁忌）"},
            {"name": "get_timelines", "description": "获取时间链（事件发展脉络）"},
            {"name": "get_memos", "description": "获取烛台备忘录"},
            {"name": "get_experiences", "description": "获取年轮经验"},
            {"name": "get_roster", "description": "查询名册(人物)记录，支持姓名精确查找"},
            {"name": "analytics", "description": "获取记忆库统计分析数据（情绪分布、类型统计、活跃度趋势）"},
            {"name": "trace_chain", "description": "追溯记忆因果链（前因后果），direction=both/previous/next, max_depth=3"},
            {"name": "link_events", "description": "建立两个事件之间的因果关系，prev_id=前因, next_id=后果"},
            {"name": "memory_export", "description": "导出记忆数据，export_type=all/dynamic/permanent/identity/pattern/feel"},
        ]
        
        tools_json = _json_lib.dumps(tools_info, ensure_ascii=False)
        
        system_prompt = f"""你是记忆系统AI管家，用最少工具调用完成任务。
        
可用工具：{tools_json}

概念：年轮(experience)=经验，烛台(candlestick)=备忘录

调用格式：
{{"action":"call_tool","tool_name":"工具名","parameters":{{...}},"reason":"原因"}}
{{"action":"call_tools","steps":[{{"tool_name":"工具名","parameters":{{...}}}}]}}
{{"action":"direct_answer","answer":"回答"}}
{{"action":"summarize","summary":"总结","steps":[...],"result":"结果"}}

规则：
- 整理过期记忆用smart_organize，每周报告用weekly_organize
- 先查询再操作，一次返回所有步骤
- 只返回JSON，无其他文字"""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request},
        ]
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=messages,
            max_tokens=500,
            temperature=0.3,
        )
        
        if not response.choices or not response.choices[0].message.content:
            return f"AI管家无法理解您的请求: {request}"
        
        raw = response.choices[0].message.content.strip()
        try:
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = _json_lib.loads(raw)
        except _json_lib.JSONDecodeError:
            return f"AI管家解析失败，请重试。原始响应: {raw[:200]}"
        
        action = result.get("action")
        
        if action == "direct_answer":
            return result.get("answer", "无法回答")
        
        if action == "summarize":
            steps = result.get("steps", [])
            steps_str = "\n".join([f"{i+1}. {s}" for i, s in enumerate(steps)])
            return f"📋 任务总结\n\n{result.get('summary', '')}\n\n步骤:\n{steps_str}\n\n结果: {result.get('result', '')}"
        
        if action == "call_tool":
            return await _execute_tool_call(result)
        
        if action == "call_tools":
            return await _execute_multiple_tool_calls(result)
        
        return f"AI管家返回了未知的操作类型: {action}"
        
    except Exception as e:
        logger.error(f"ai_manage failed: {e}")
        return f"AI管家执行失败: {e}"


async def _execute_tool_call(call_data):
    """执行单个工具调用。"""
    tool_name = call_data.get("tool_name")
    parameters = call_data.get("parameters", {})
    reason = call_data.get("reason", "")
    
    if not tool_name:
        return "AI管家未指定要调用的工具"
    
    tool_func = globals().get(tool_name)
    if not tool_func:
        return f"未知工具: {tool_name}"
    
    try:
        import inspect
        sig = inspect.signature(tool_func)
        valid_params = {k: v for k, v in parameters.items() if k in sig.parameters}
        
        if asyncio.iscoroutinefunction(tool_func):
            tool_result = await tool_func(**valid_params)
        else:
            tool_result = tool_func(**valid_params)
        
        if reason:
            return f"【调用原因】{reason}\n\n【执行结果】\n{tool_result}"
        else:
            return f"【执行结果】\n{tool_result}"
    except Exception as e:
        return f"工具调用失败: {tool_name} - {e}"


async def _execute_multiple_tool_calls(call_data):
    """执行多个工具调用。"""
    steps = call_data.get("steps", [])
    if not steps:
        return "AI管家未指定执行步骤"
    
    results = []
    for i, step in enumerate(steps):
        tool_name = step.get("tool_name")
        parameters = step.get("parameters", {})
        reason = step.get("reason", "")
        
        if not tool_name:
            results.append(f"步骤 {i+1}: 未指定工具")
            continue
        
        tool_func = globals().get(tool_name)
        if not tool_func:
            results.append(f"步骤 {i+1}: 未知工具 {tool_name}")
            continue
        
        try:
            if asyncio.iscoroutinefunction(tool_func):
                tool_result = await tool_func(**parameters)
            else:
                tool_result = tool_func(**parameters)
            
            result_str = f"步骤 {i+1}: {reason}\n{tool_result[:300]}"
            results.append(result_str)
        except Exception as e:
            results.append(f"步骤 {i+1}: {tool_name} 调用失败 - {e}")
    
    return "📋 多步骤执行结果\n\n" + "\n\n---\n\n".join(results)


@mcp.tool()
async def summarize_recent_events(days: int = 7, max_events: int = 10) -> str:
    """获取最近一段时间内的记忆事件概括。days指定天数(默认7天),max_events指定最大事件数(默认10)。返回最近事件的简单概括,供MCP调用者了解用户最近活动。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        import datetime
        today = datetime.date.today()
        cutoff_date = today - datetime.timedelta(days=days)
        cutoff_str = cutoff_date.isoformat()
        
        recent_buckets = []
        for b in all_buckets:
            created = b["metadata"].get("created", "")
            if created and created >= cutoff_str:
                recent_buckets.append(b)
        
        recent_buckets.sort(key=lambda x: x["metadata"].get("created", ""), reverse=True)
        recent_buckets = recent_buckets[:max_events]
        
        if not recent_buckets:
            return f"最近{days}天没有记忆事件。"
        
        events_summary = []
        for b in recent_buckets:
            meta = b["metadata"]
            name = meta.get("name", "未命名")
            bucket_type = meta.get("type", "dynamic")
            emotions = meta.get("emotions", [])
            domain = meta.get("domain", [])
            importance = meta.get("importance", 5)
            created = meta.get("created", "")[:10]
            
            emo_str = ""
            if emotions:
                emo_labels = [e["label"] for e in emotions[:2]]
                emo_str = f" 情绪:{','.join(emo_labels)}"
            
            domain_str = ""
            if domain:
                domain_str = f" 领域:{','.join(domain[:2])}"
            
            events_summary.append(f"[{created}] [{bucket_type}] {name}{emo_str}{domain_str} (重要度:{importance})")
        
        events_text = "\n".join(events_summary)
        
        if dehydrator and dehydrator.api_available:
            try:
                summary_prompt = f"""请概括以下最近的记忆事件，用简洁的语言总结用户最近的活动和状态。

事件列表：
{events_text}

要求：
1. 用一段简短的话（50-100字）概括用户最近的活动
2. 突出主要事件和情绪变化
3. 不要罗列，要连贯自然
4. 适合作为对话开场白或状态报告"""
                
                response = await dehydrator.client.chat.completions.create(
                    model=dehydrator.model,
                    messages=[
                        {"role": "system", "content": "你是一个记忆总结助手，擅长将多条事件概括为简短的状态报告。"},
                        {"role": "user", "content": summary_prompt},
                    ],
                    max_tokens=150,
                    temperature=0.3,
                )
                
                if response.choices and response.choices[0].message.content:
                    llm_summary = response.choices[0].message.content.strip()
                    return f"【最近{days}天概括】\n{llm_summary}\n\n【详细事件】\n{events_text}"
            except Exception as e:
                logger.warning(f"LLM summary failed: {e}")
        
        return f"【最近{days}天的记忆事件】\n{events_text}"
        
    except Exception as e:
        logger.error(f"summarize_recent_events failed: {e}")
        return f"获取最近事件失败: {e}"


# =============================================================
# Brain Export/Import (for backup and migration)
# 大脑导出/导入（用于备份和迁移）
# =============================================================
@mcp.tool()
async def export_brain(output_path: str = "") -> str:
    """
    导出大脑数据：将 buckets/ 目录和数据库打包为 zip 文件。
    
    参数:
    - output_path: 输出 zip 文件路径（可选），不指定则自动生成在当前目录
    
    返回:
    - 导出成功：zip 文件路径和统计信息
    - 导出失败：错误信息
    
    包含内容:
    - buckets/ 目录下所有记忆桶文件（permanent/dynamic/archive/feel/identity/pattern）
    - embeddings.db 向量数据库
    """
    try:
        import zipfile
        import tempfile
        import shutil
        
        buckets_dir = bucket_mgr.base_dir
        db_path = embedding_engine.db_path
        
        if not os.path.exists(buckets_dir):
            return f"错误：buckets 目录不存在: {buckets_dir}"
        
        if not os.path.exists(db_path):
            logger.warning(f"embeddings.db 不存在，将跳过: {db_path}")
        
        if output_path:
            output_path = os.path.abspath(output_path)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        else:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(os.getcwd(), f"brain_export_{timestamp}.zip")
        
        logger.info(f"Starting brain export to: {output_path}")
        
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Add all files in buckets directory
            if os.path.exists(buckets_dir):
                for root, dirs, files in os.walk(buckets_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, os.path.dirname(buckets_dir))
                        zipf.write(file_path, arcname)
                        logger.debug(f"Added to zip: {arcname}")
            
            # Add embeddings.db
            if os.path.exists(db_path):
                arcname = os.path.basename(db_path)
                zipf.write(db_path, arcname)
                logger.debug(f"Added to zip: {arcname}")
        
        file_size = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"Brain export completed: {output_path} ({file_size:.2f} MB)")
        
        return f"大脑导出成功！\n文件路径: {output_path}\n大小: {file_size:.2f} MB"
        
    except Exception as e:
        logger.error(f"Brain export failed: {e}")
        return f"大脑导出失败: {e}"


@mcp.tool()
async def import_brain(zip_path: str, overwrite: bool = False) -> str:
    """
    导入大脑数据：从 zip 文件恢复 buckets/ 目录和数据库。
    
    参数:
    - zip_path: zip 文件路径
    - overwrite: 是否覆盖现有数据（默认 False）
    
    返回:
    - 导入成功：统计信息
    - 导入失败：错误信息
    
    注意:
    - 如果 overwrite=False，会检查是否有冲突并跳过已有文件
    - 导入前会关闭所有数据库连接
    - 导入后会重新加载相关组件
    """
    try:
        import zipfile
        import tempfile
        import shutil
        
        zip_path = os.path.abspath(zip_path)
        
        if not os.path.exists(zip_path):
            return f"错误：zip 文件不存在: {zip_path}"
        
        buckets_dir = bucket_mgr.base_dir
        db_path = embedding_engine.db_path
        
        logger.info(f"Starting brain import from: {zip_path}")
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir_abs = os.path.abspath(tmp_dir)
            
            # Safe extraction (prevent Zip Slip vulnerability)
            # 安全解压（防止 Zip Slip 路径遍历漏洞）
            with zipfile.ZipFile(zip_path, 'r') as zipf:
                for member in zipf.namelist():
                    # Skip directory entries
                    if member.endswith('/'):
                        continue
                    
                    # Validate path to prevent path traversal
                    # 验证路径，防止路径遍历
                    member_path = os.path.join(tmp_dir_abs, member)
                    member_path_abs = os.path.abspath(member_path)
                    
                    if not member_path_abs.startswith(tmp_dir_abs):
                        logger.warning(f"Skipping unsafe zip entry: {member}")
                        continue
                    
                    # Create parent directories
                    os.makedirs(os.path.dirname(member_path), exist_ok=True)
                    
                    # Extract file
                    with zipf.open(member) as src_file:
                        with open(member_path, 'wb') as dst_file:
                            dst_file.write(src_file.read())
            
            # Check extracted structure
            extracted_buckets = os.path.join(tmp_dir, "buckets")
            extracted_db = os.path.join(tmp_dir, "embeddings.db")
            
            has_buckets = os.path.exists(extracted_buckets)
            has_db = os.path.exists(extracted_db)
            
            if not has_buckets and not has_db:
                return "错误：zip 文件中未找到 buckets/ 目录或 embeddings.db"
            
            # Import buckets
            if has_buckets:
                for root, dirs, files in os.walk(extracted_buckets):
                    for file in files:
                        src_path = os.path.join(root, file)
                        rel_path = os.path.relpath(src_path, extracted_buckets)
                        dst_path = os.path.join(buckets_dir, rel_path)
                        
                        if os.path.exists(dst_path) and not overwrite:
                            logger.debug(f"Skipping existing file: {rel_path}")
                            continue
                        
                        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                        shutil.copy2(src_path, dst_path)
                        logger.debug(f"Copied: {rel_path}")
            
            # Import database
            if has_db:
                if os.path.exists(db_path) and not overwrite:
                    logger.warning(f"embeddings.db already exists, skipping (overwrite={overwrite})")
                else:
                    shutil.copy2(extracted_db, db_path)
                    logger.debug(f"Copied embeddings.db")
            
            # Invalidate cache and reload
            bucket_mgr._invalidate_cache()
            
            # Re-initialize embedding engine to pick up new database
            # 重新初始化 embedding engine 以加载新数据库
            embedding_engine._init_db()
            
            logger.info("Cache invalidated and components reloaded after import")
        
        logger.info(f"Brain import completed: {zip_path}")
        
        return f"大脑导入成功！\n- buckets: {'已导入' if has_buckets else '跳过'} (overwrite={overwrite})\n- embeddings.db: {'已导入' if has_db else '跳过'} (overwrite={overwrite})"
        
    except Exception as e:
        logger.error(f"Brain import failed: {e}")
        return f"大脑导入失败: {e}"


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 50))
        limit = request.query_params.get("limit")
        
        if limit is not None:
            limit = int(limit)
            paginated = result[:limit]
            return JSONResponse({
                "buckets": paginated,
                "total": len(result),
                "page": 1,
                "page_size": limit,
                "total_pages": 1
            })
        
        total = len(result)
        start = (page - 1) * page_size
        end = start + page_size
        paginated = result[start:end]
        
        return JSONResponse({
            "buckets": paginated,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/identities", methods=["GET"])
async def api_identities(request):
    """List all identity profiles."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        identities = await identity_mgr.list_all()
        result = []
        for ident in identities:
            meta = ident.get("metadata", {})
            result.append({
                "id": ident["id"],
                "name": meta.get("name", ident["id"]),
                "aliases": meta.get("aliases", []),
                "basic_info": meta.get("basic_info", {}),
                "core_traits": meta.get("core_traits", []),
                "relationships": meta.get("relationships", []),
                "related_memories": meta.get("related_memories", []),
                "pinned": meta.get("pinned", False),
                "protected": meta.get("protected", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 0),
                "content": ident.get("content", ""),
            })
        result.sort(key=lambda x: x.get("activation_count", 0), reverse=True)
        return JSONResponse({"identities": result, "total": len(result)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
    })


async def _generate_one_line_summary_async(bucket_id: str, content: str):
    """
    Asynchronously generate and store one-line summary (20 chars max).
    Called when a memory bucket is created or digested into experience.
    异步生成并存储一句话摘要（最多20字）。
    在记忆桶创建或被消化入年轮时调用。
    """
    try:
        summary = await dehydrator.generate_one_line_summary(content)
        if summary:
            await bucket_mgr.update(bucket_id, one_line_summary=summary)
            logger.info(f"Generated one-line summary for bucket {bucket_id}: {summary}")
    except Exception as e:
        logger.warning(f"Failed to generate one-line summary for bucket {bucket_id}: {e}")


async def _generate_dehydrated_summary_async(bucket_id: str, content: str, metadata: dict = None):
    """
    Asynchronously generate and store dehydrated summary (lazy summarization).
    Called when a memory bucket is created or updated.
    This prevents real-time AI calls during retrieval, saving tokens and reducing latency.
    异步生成并存储脱水总结（惰性总结）。
    在记忆桶创建或更新时调用。
    这避免了检索时的实时AI调用，节省Token并减少延迟。
    """
    try:
        clean_meta = {k: v for k, v in (metadata or {}).items() if k != "tags"}
        summary = await dehydrator.dehydrate(content, clean_meta, brief=True)
        if summary:
            await bucket_mgr.update(bucket_id, dehydrated_summary=summary)
            logger.info(f"Generated dehydrated summary for bucket {bucket_id}")
    except Exception as e:
        logger.warning(f"Failed to generate dehydrated summary for bucket {bucket_id}: {e}")


async def _reinforce_related_experiences(source_bucket_id: str):
    """
    Reinforce related experiences when a memory is digested.
    Finds experiences that reference this bucket in their source_bucket_ids,
    increments their hit_count, and updates last_hit timestamp.
    This implements the reinforcement-based decay for experiences.
    当记忆被消化时，强化相关经验。
    查找在 source_bucket_ids 中引用该记忆桶的经验，
    增加其 hit_count，并更新 last_hit 时间戳。
    这实现了经验的强化频次衰减机制。
    """
    try:
        experiences = await bucket_mgr.find_by_domain("经验", include_archive=False)
        
        reinforced = 0
        for exp in experiences:
            meta = exp.get("metadata", {})
            source_ids = meta.get("source_bucket_ids", [])
            if source_bucket_id in source_ids:
                current_hit_count = meta.get("hit_count", 0)
                await bucket_mgr.update(
                    exp["id"],
                    hit_count=current_hit_count + 1,
                    last_hit=datetime.datetime.now().isoformat(),
                )
                logger.info(f"Reinforced experience {exp['id']}: hit_count={current_hit_count + 1}")
                reinforced += 1
        
        if reinforced > 0:
            logger.info(f"Reinforced {reinforced} experiences from digested bucket {source_bucket_id}")
    except Exception as e:
        logger.warning(f"Failed to reinforce related experiences for bucket {source_bucket_id}: {e}")


@mcp.custom_route("/api/bucket", methods=["POST"])
async def api_bucket_create(request):
    """Create a new bucket."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    content = body.get("content", "")
    tags = body.get("tags", [])
    importance = body.get("importance", 5)
    domain = body.get("domain", [])
    name = body.get("name", None)
    bucket_type = body.get("type", "dynamic")
    
    create_kwargs = {
        "content": content,
        "tags": tags,
        "importance": importance,
        "domain": domain,
        "name": name,
        "bucket_type": bucket_type,
        "dehydrator": dehydrator,
    }
    
    if body.get("valence") is not None:
        create_kwargs["valence"] = body["valence"]
    if body.get("arousal") is not None:
        create_kwargs["arousal"] = body["arousal"]
    
    if body.get("emotions"):
        create_kwargs["emotions"] = body["emotions"]
        if body.get("dominant_emotion"):
            create_kwargs["dominant_emotion"] = body["dominant_emotion"]
        if body.get("emotion_metrics"):
            create_kwargs["emotion_metrics"] = body["emotion_metrics"]
    else:
        try:
            analysis = await dehydrator.analyze(content)
            create_kwargs["emotions"] = analysis.get("emotions", [])
            create_kwargs["dominant_emotion"] = analysis.get("dominant_emotion", "")
            create_kwargs["emotion_metrics"] = analysis.get("emotion_metrics", {})
            if not domain:
                create_kwargs["domain"] = analysis.get("domain", ["未分类"])
            if not tags:
                create_kwargs["tags"] = analysis.get("tags", [])
        except Exception as e:
            logger.warning(f"Auto-tagging failed in API: {e}")
    
    bucket_id = await bucket_mgr.create(**create_kwargs)
    
    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception as e:
        logger.warning(f"Failed to store embedding: {e}")
    
    asyncio.create_task(_generate_one_line_summary_async(bucket_id, content))
    asyncio.create_task(_generate_dehydrated_summary_async(bucket_id, content, create_kwargs))
    
    return JSONResponse({"id": bucket_id}, status_code=201)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["PUT"])
async def api_bucket_update(request):
    """Update an existing bucket."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    update_kwargs = {}
    if "content" in body:
        update_kwargs["content"] = body["content"]
    if "tags" in body:
        update_kwargs["tags"] = body["tags"]
    if "importance" in body:
        update_kwargs["importance"] = body["importance"]
    if "domain" in body:
        update_kwargs["domain"] = body["domain"]
    if "name" in body:
        update_kwargs["name"] = body["name"]
    if "type" in body:
        update_kwargs["type"] = body["type"]
    if "emotions" in body:
        update_kwargs["emotions"] = body["emotions"]
    if "dominant_emotion" in body:
        update_kwargs["dominant_emotion"] = body["dominant_emotion"]
    if "aliases" in body:
        update_kwargs["aliases"] = body["aliases"]
    if "traits" in body:
        update_kwargs["traits"] = body["traits"]
    if "gender" in body:
        update_kwargs["gender"] = body["gender"]
    if "age" in body:
        update_kwargs["age"] = body["age"]
    if "occupation" in body:
        update_kwargs["occupation"] = body["occupation"]
    if "interests" in body:
        update_kwargs["interests"] = body["interests"]
    if "basic_info" in body:
        update_kwargs["basic_info"] = body["basic_info"]
    if "relationships" in body:
        update_kwargs["relationships"] = body["relationships"]
    if "notes" in body:
        update_kwargs["notes"] = body["notes"]
    if "decay_stage" in body:
        update_kwargs["decay_stage"] = body["decay_stage"]
    if "digested" in body:
        update_kwargs["digested"] = body["digested"]
    
    if not update_kwargs:
        return JSONResponse({"error": "no fields to update"}, status_code=400)
    
    success = await bucket_mgr.update(bucket_id, **update_kwargs)
    if not success:
        return JSONResponse({"error": "update failed"}, status_code=500)
    
    return JSONResponse({"success": True})


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
async def api_bucket_delete(request):
    """Delete a bucket."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    
    await bucket_mgr.delete(bucket_id)
    return JSONResponse({"success": True})


@mcp.custom_route("/api/bucket/{bucket_id}/related", methods=["POST"])
async def api_bucket_add_related(request):
    """Add a related bucket relationship."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    
    try:
        body = await request.json()
        related_id = body.get("related_id", "")
        if not related_id:
            return JSONResponse({"error": "related_id required"}, status_code=400)
        
        success = await bucket_mgr.add_related_bucket(bucket_id, related_id)
        if success:
            return JSONResponse({"success": True})
        return JSONResponse({"error": "failed to add relationship"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/parent", methods=["POST"])
async def api_bucket_set_parent(request):
    """Set parent bucket for hierarchical relationship."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    
    try:
        body = await request.json()
        parent_id = body.get("parent_id", "")
        if not parent_id:
            return JSONResponse({"error": "parent_id required"}, status_code=400)
        
        success = await bucket_mgr.set_parent_bucket(bucket_id, parent_id)
        if success:
            return JSONResponse({"success": True})
        return JSONResponse({"error": "failed to set parent"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/sequence", methods=["POST"])
async def api_bucket_add_sequence(request):
    """Add event to sequence chain."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    
    try:
        body = await request.json()
        event_id = body.get("event_id", "")
        position = body.get("position", None)
        
        if not event_id:
            return JSONResponse({"error": "event_id required"}, status_code=400)
        
        success = await bucket_mgr.add_event_sequence(bucket_id, event_id, position)
        if success:
            return JSONResponse({"success": True})
        return JSONResponse({"error": "failed to add sequence"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/importance-details", methods=["POST"])
async def api_bucket_update_importance_details(request):
    """Update multi-dimensional importance details."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    
    try:
        body = await request.json()
        details = body.get("details", {})
        
        success = await bucket_mgr.update_importance_details(bucket_id, details)
        if success:
            bucket = await bucket_mgr.get(bucket_id)
            importance = bucket.get("metadata", {}).get("importance", 5)
            imp_details = bucket.get("metadata", {}).get("importance_details", {})
            return JSONResponse({"success": True, "importance": importance, "importance_details": imp_details})
        return JSONResponse({"error": "failed to update importance details"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/directory", methods=["GET"])
async def api_directory(request):
    """Generate memory directory for frontend with AI-generated summaries."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    detail_level = request.query_params.get("detail", "medium")
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        by_type = {}
        for b in all_buckets:
            bucket_type = b.get("metadata", {}).get("type", "event")
            if bucket_type == "identity":
                continue
            if bucket_type not in by_type:
                by_type[bucket_type] = []
            by_type[bucket_type].append(b)
        
        identity_records = await identity_mgr.list_all()
        if identity_records:
            by_type["identity"] = identity_records
        
        type_names = {
            "identity": "身份档案",
            "pattern": "行为模式",
            "event": "事件记忆",
            "feel": "感受记忆",
            "permanent": "永久记忆",
            "archived": "归档记忆",
            "experience": "年轮",
            "candlestick": "烛台",
            "dynamic": "动态记忆",
        }
        
        sections = []
        for bucket_type, buckets in by_type.items():
            type_label = type_names.get(bucket_type, bucket_type)
            entries = []
            
            if detail_level == "full":
                for b in buckets[:50]:
                    meta = b.get("metadata", {})
                    entries.append({
                        "id": b["id"],
                        "name": meta.get("name", b["id"]),
                        "importance": meta.get("importance", 0),
                        "importance_details": meta.get("importance_details", {}),
                        "tags": meta.get("tags", [])[:3],
                        "emotions": [e["label"] for e in meta.get("emotions", [])[:3]],
                        "created": meta.get("created", ""),
                        "decay_stage": meta.get("decay_stage", 1),
                        "pinned": meta.get("pinned", False),
                        "protected": meta.get("protected", False),
                        "score": meta.get("score", 0),
                        "activation_count": meta.get("activation_count", 0),
                        "last_active": meta.get("last_active", ""),
                    })
            else:
                key_entries = []
                for b in buckets:
                    meta = b.get("metadata", {})
                    key_entries.append({
                        "id": b["id"],
                        "name": meta.get("name", b["id"]),
                        "importance": meta.get("importance", 0),
                        "importance_details": meta.get("importance_details", {}),
                        "emotions": [e["label"] for e in meta.get("emotions", [])[:3]],
                        "tags": meta.get("tags", [])[:3],
                        "created": meta.get("created", ""),
                        "decay_stage": meta.get("decay_stage", 1),
                        "pinned": meta.get("pinned", False),
                        "protected": meta.get("protected", False),
                        "score": meta.get("score", 0),
                        "activation_count": meta.get("activation_count", 0),
                        "last_active": meta.get("last_active", ""),
                    })
                key_entries.sort(key=lambda x: x.get("score", x["importance"]), reverse=True)
                entries = key_entries[:10]
            
            sections.append({
                "type": bucket_type,
                "label": type_label,
                "count": len(buckets),
                "entries": entries,
                "summary": "",
            })
        
        if dehydrator and dehydrator.api_available and all_buckets:
            try:
                buckets_summary = []
                for bucket_type, buckets in by_type.items():
                    type_label = type_names.get(bucket_type, bucket_type)
                    bucket_names = [b.get("metadata", {}).get("name", "") for b in buckets[:10] if b.get("metadata", {}).get("name")]
                    if bucket_names:
                        buckets_summary.append(f"{type_label}: {', '.join(bucket_names)}")
                
                summary_prompt = f"""请为以下记忆分类生成简洁的总结名称和一句话描述。

分类列表：
{chr(10).join(buckets_summary)}

要求：
1. 为每个分类生成一个简短的总结名称（8-16字），能概括该分类的核心内容
2. 同时生成一句话描述（20-40字），描述该分类的主题和特点
3. 返回JSON格式：{{"sections": [{{"type": "类型名", "summary_name": "总结名称", "summary_desc": "描述"}}]}}
4. 只返回JSON，不要其他文字"""
                
                response = await dehydrator.client.chat.completions.create(
                    model=dehydrator.model,
                    messages=[
                        {"role": "system", "content": "你是一个记忆分类总结助手，擅长为记忆数据生成简洁的分类名称和描述。"},
                        {"role": "user", "content": summary_prompt},
                    ],
                    max_tokens=500,
                    temperature=0.3,
                )
                
                if response.choices and response.choices[0].message.content:
                    import json
                    ai_result = json.loads(response.choices[0].message.content.strip())
                    if ai_result.get("sections"):
                        for ai_section in ai_result["sections"]:
                            for section in sections:
                                if ai_section["type"] in section["label"] or section["type"] in ai_section["type"]:
                                    section["summary_name"] = ai_section.get("summary_name", "")
                                    section["summary_desc"] = ai_section.get("summary_desc", "")
                                    break
            except Exception as e:
                logger.warning(f"AI directory summary failed: {e}")
        
        emotion_counts = {}
        for b in all_buckets:
            emotions = b.get("metadata", {}).get("emotions", [])
            for e in emotions:
                label = e.get("label", "")
                if label:
                    emotion_counts[label] = emotion_counts.get(label, 0) + 1
        
        top_tags = {}
        for b in all_buckets:
            tags = b.get("metadata", {}).get("tags", [])
            for t in tags:
                if t:
                    top_tags[t] = top_tags.get(t, 0) + 1
        
        return JSONResponse({
            "total": len(all_buckets),
            "sections": sections,
            "emotions": sorted(emotion_counts.items(), key=lambda x: x[1], reverse=True)[:5],
            "tags": sorted(top_tags.items(), key=lambda x: x[1], reverse=True)[:8],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query with advanced filtering options."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    
    try:
        limit = int(request.query_params.get("limit", 10))
        bucket_type = request.query_params.get("type", "")
        domain = request.query_params.get("domain", "")
        min_valence = float(request.query_params.get("min_valence", 0)) if request.query_params.get("min_valence") else None
        max_valence = float(request.query_params.get("max_valence", 1)) if request.query_params.get("max_valence") else None
        min_arousal = float(request.query_params.get("min_arousal", 0)) if request.query_params.get("min_arousal") else None
        max_arousal = float(request.query_params.get("max_arousal", 1)) if request.query_params.get("max_arousal") else None
        use_semantic = request.query_params.get("semantic", "false").lower() == "true"
        
        matches = await bucket_mgr.search(query, limit=limit)
        
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            b_type = meta.get("type", "")
            b_domain = meta.get("domain", [])
            b_valence = meta.get("valence", 0.5)
            b_arousal = meta.get("arousal", 0.3)
            
            if bucket_type and b_type != bucket_type:
                continue
            if domain and (not b_domain or domain not in b_domain):
                continue
            if min_valence is not None and b_valence < min_valence:
                continue
            if max_valence is not None and b_valence > max_valence:
                continue
            if min_arousal is not None and b_arousal < min_arousal:
                continue
            if max_arousal is not None and b_arousal > max_arousal:
                continue
            
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "type": b_type,
                "domain": b_domain,
                "valence": b_valence,
                "arousal": b_arousal,
                "importance": meta.get("importance", 0),
                "created": meta.get("created", ""),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        
        result.sort(key=lambda x: x["score"], reverse=True)
        
        return JSONResponse({
            "results": result,
            "total": len(result),
            "query": query,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/experiences", methods=["GET"])
async def api_experiences(request):
    """List all experiences."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    exp_type = request.query_params.get("type", "")
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        experiences = [b for b in all_buckets 
                       if b.get("metadata", {}).get("domain") and "经验" in b.get("metadata", {}).get("domain")]
        
        if exp_type:
            experiences = [e for e in experiences if e.get("metadata", {}).get("exp_type") == exp_type]
        
        result = []
        for e in experiences:
            meta = e.get("metadata", {})
            result.append({
                "id": e["id"],
                "title": meta.get("name", e["id"]),
                "content": e.get("content", ""),
                "exp_type": meta.get("exp_type", "user"),
                "source": meta.get("source", ""),
                "tags": meta.get("tags", []),
                "created": meta.get("created", ""),
                "updated": meta.get("updated", ""),
                "apply_count": meta.get("apply_count", 0),
                "last_applied": meta.get("last_applied", ""),
            })
        
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/experiences", methods=["POST"])
async def api_create_experience(request):
    """Create a new experience."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    title = body.get("title", "")
    content = body.get("content", "")
    exp_type = body.get("exp_type", "user")
    source = body.get("source", "")
    source_bucket_ids = body.get("source_bucket_ids", [])
    tags = body.get("tags", [])
    
    if not title or not content:
        return JSONResponse({"error": "title and content are required"}, status_code=400)
    
    if source and source not in source_bucket_ids:
        source_bucket_ids.append(source)
    
    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=8,
        domain=["经验"],
        name=title,
        bucket_type="permanent",
    )
    
    await bucket_mgr.update(
        bucket_id,
        exp_type=exp_type,
        source=source,
        source_bucket_ids=source_bucket_ids,
        apply_count=0,
        last_applied="",
        hit_count=0,
        last_hit="",
    )
    
    return JSONResponse({"id": bucket_id}, status_code=201)


@mcp.custom_route("/api/experiences/{exp_id}", methods=["PUT"])
async def api_update_experience(request):
    """Update an experience."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    exp_id = request.path_params.get("exp_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    try:
        exp = await bucket_mgr.get(exp_id)
        if not exp:
            return JSONResponse({"error": "not found"}, status_code=404)
        
        meta = exp.get("metadata", {})
        updates = {}
        
        if "title" in body:
            updates["title"] = body["title"]
        if "content" in body:
            updates["content"] = body["content"]
        if "exp_type" in body:
            updates["exp_type"] = body["exp_type"]
        if "source" in body:
            updates["source"] = body["source"]
        if "tags" in body:
            updates["tags"] = body["tags"]
        updates["updated"] = datetime.now().isoformat()
        
        if "source_bucket_ids" in body:
            current_ids = meta.get("source_bucket_ids", [])
            new_ids = body["source_bucket_ids"]
            if isinstance(new_ids, list):
                for bid in new_ids:
                    if bid not in current_ids:
                        current_ids.append(bid)
                updates["source_bucket_ids"] = current_ids
        
        meta.update(updates)
        success = await bucket_mgr.update(exp_id, body.get("content", exp.get("content", "")), meta)
        
        if success:
            return JSONResponse({"success": True})
        return JSONResponse({"error": "update failed"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/experiences/{exp_id}", methods=["DELETE"])
async def api_delete_experience(request):
    """Delete an experience."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    exp_id = request.path_params.get("exp_id", "")
    try:
        exp = await bucket_mgr.get(exp_id)
        if not exp:
            return JSONResponse({"error": "not found"}, status_code=404)
        
        await bucket_mgr.delete(exp_id)
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/experiences/{exp_id}/apply — Apply an experience
# =============================================================
@mcp.custom_route("/api/experiences/{exp_id}/apply", methods=["POST"])
async def api_apply_experience(request):
    """Apply an experience - increment apply count and update last applied time."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    exp_id = request.path_params.get("exp_id", "")
    try:
        exp = await bucket_mgr.get(exp_id)
        if not exp:
            return JSONResponse({"error": "not found"}, status_code=404)
        
        meta = exp.get("metadata", {})
        apply_count = meta.get("apply_count", 0) + 1
        last_applied = datetime.datetime.now().isoformat()
        
        success = await bucket_mgr.update(exp_id, content=exp.get("content", ""), apply_count=apply_count, last_applied=last_applied)
        
        if success:
            return JSONResponse({
                "success": True,
                "apply_count": apply_count,
                "last_applied": last_applied,
            })
        return JSONResponse({"error": "update failed"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/manage-relation", methods=["POST"])
async def api_manage_relation(request):
    """Manage relations between buckets: link/parent/chain."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        action = body.get("action", "")
        bucket_id = body.get("bucket_id", "")
        target_id = body.get("target_id", "")
        
        if not action or not bucket_id:
            return JSONResponse({"error": "action and bucket_id are required"}, status_code=400)
        
        if action == "link":
            if not target_id:
                return JSONResponse({"error": "target_id is required for link action"}, status_code=400)
            success = await bucket_mgr.add_related_bucket(bucket_id, target_id)
            return JSONResponse({"success": success, "message": f"关联已建立: {bucket_id} ↔ {target_id}"})
        
        elif action == "parent":
            if not target_id:
                return JSONResponse({"error": "target_id is required for parent action"}, status_code=400)
            success = await bucket_mgr.set_parent_bucket(bucket_id, target_id)
            return JSONResponse({"success": success, "message": f"层级已建立: {target_id} ⊃ {bucket_id}"})
        
        elif action == "chain":
            if not target_id:
                return JSONResponse({"error": "target_id is required for chain action"}, status_code=400)
            position = body.get("position", "after")
            success = await bucket_mgr.add_event_sequence(bucket_id, target_id, position)
            return JSONResponse({"success": success, "message": f"事件链已添加: {bucket_id} → {target_id}"})
        
        elif action == "unlink":
            if not target_id:
                return JSONResponse({"error": "target_id is required for unlink action"}, status_code=400)
            success = await bucket_mgr.remove_related_bucket(bucket_id, target_id)
            return JSONResponse({"success": success, "message": f"关联已移除: {bucket_id} ↔ {target_id}"})
        
        else:
            return JSONResponse({"error": f"未知action: {action}"}, status_code=400)
    
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/network", methods=["GET"])
async def api_network(request):
    """Get memory network visualization with multiple edge types."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}
        bucket_map = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            
            if meta.get("digested", False):
                continue
            
            bucket_map[bid] = meta
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": False,
                "decay_stage": meta.get("decay_stage", 1),
            })
            if embedding_engine and embedding_engine.enabled:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb

        edge_set = set()

        # Build edges from event sequence (same_event)
        for bid, meta in bucket_map.items():
            sequence = meta.get("event_sequence", [])
            for i in range(len(sequence) - 1):
                source = sequence[i]
                target = sequence[i + 1]
                if source in bucket_map and target in bucket_map:
                    key = tuple(sorted([source, target]))
                    if key not in edge_set:
                        edge_set.add(key)
                        edges.append({
                            "source": source,
                            "target": target,
                            "type": "same_event",
                            "label": "同一事件",
                            "weight": 1.0,
                        })

        # Build edges from related_buckets (related)
        for bid, meta in bucket_map.items():
            related = meta.get("related_buckets", [])
            for rel_id in related:
                if rel_id in bucket_map and rel_id != bid:
                    key = tuple(sorted([bid, rel_id]))
                    if key not in edge_set:
                        edge_set.add(key)
                        edges.append({
                            "source": bid,
                            "target": rel_id,
                            "type": "related",
                            "label": "相关内容",
                            "weight": 0.8,
                        })

        # Build edges from parent-child relationship (hierarchy)
        for bid, meta in bucket_map.items():
            parent_id = meta.get("parent_bucket")
            if parent_id and parent_id in bucket_map:
                key = tuple(sorted([bid, parent_id]))
                if key not in edge_set:
                    edge_set.add(key)
                    edges.append({
                        "source": parent_id,
                        "target": bid,
                        "type": "hierarchy",
                        "label": "父子关系",
                        "weight": 0.9,
                    })

        # Build edges from embeddings (similarity)
        ids = list(embeddings.keys())
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim > 0.5:
                    key = tuple(sorted([id_a, id_b]))
                    if key not in edge_set:
                        edge_set.add(key)
                        edges.append({
                            "source": id_a,
                            "target": id_b,
                            "type": "similarity",
                            "label": "语义相似",
                            "weight": round(sim, 3),
                        })

        # Build edges from shared tags (cooccurrence)
        tag_to_buckets = {}
        for bid, meta in bucket_map.items():
            tags = meta.get("tags", [])
            for tag in tags:
                if tag not in tag_to_buckets:
                    tag_to_buckets[tag] = []
                tag_to_buckets[tag].append(bid)

        for tag, bucket_ids in tag_to_buckets.items():
            if len(bucket_ids) >= 2:
                max_edges = min(5, len(bucket_ids) - 1)
                for i in range(min(10, len(bucket_ids))):
                    for j in range(i + 1, min(i + 1 + max_edges, len(bucket_ids))):
                        id_a = bucket_ids[i]
                        id_b = bucket_ids[j]
                        key = tuple(sorted([id_a, id_b]))
                        if key not in edge_set:
                            edge_set.add(key)
                            edges.append({
                                "source": id_a,
                                "target": id_b,
                                "type": "cooccurrence",
                                "label": "共享标签",
                                "weight": 0.5,
                            })

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/anchors — anchor operations for high-emotion moments
# /api/anchors — 高情绪瞬间锚点操作
# =============================================================
@mcp.custom_route("/api/anchors", methods=["GET"])
async def api_get_anchors(request):
    """Get all anchors, optionally filtered by active_only and anchor_type."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        # --- Parse query params ---
        active_only = request.query_params.get("active_only", "false").lower() == "true"
        anchor_type = request.query_params.get("anchor_type", None)

        anchors = await bucket_mgr.get_anchors(active_only=active_only, anchor_type=anchor_type)
        return JSONResponse({"anchors": anchors})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/anchors", methods=["POST"])
async def api_create_anchor(request):
    """Create a new behavioral & emotional pivot anchor."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()

        # --- New-format fields ---
        triggers = body.get("triggers", [])
        emotional_baseline = body.get("emotional_baseline", [])
        boundaries = body.get("boundaries", [])
        related_bucket_ids = body.get("related_bucket_ids", [])
        anchor_type = body.get("anchor_type", "dynamic")
        ttl_hours = body.get("ttl_hours")
        name = body.get("name", "")

        # --- Legacy fields (backward compat) ---
        bucket_id = body.get("bucket_id", "")
        emotion_intensity = body.get("emotion_intensity", 0.0)
        summary = body.get("summary", "")
        coordinates = body.get("coordinates", {})
        emotion_tags = body.get("emotion_tags", [])

        # --- Validate: need at least triggers or summary ---
        if not triggers and not summary and not boundaries:
            return JSONResponse(
                {"error": "at least one of triggers/summary/boundaries is required"},
                status_code=400,
            )

        anchor = await bucket_mgr.add_anchor(
            triggers=triggers,
            emotional_baseline=emotional_baseline,
            boundaries=boundaries,
            related_bucket_ids=related_bucket_ids,
            anchor_type=anchor_type,
            ttl_hours=ttl_hours,
            name=name,
            # Legacy
            bucket_id=bucket_id,
            emotion_intensity=emotion_intensity,
            summary=summary,
            coordinates=coordinates,
            emotion_tags=emotion_tags,
        )
        return JSONResponse({"anchor": anchor}, status_code=201)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/anchors/{anchor_id}/activate", methods=["POST"])
async def api_activate_anchor(request):
    """Activate an anchor."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    anchor_id = request.path_params.get("anchor_id", "")
    try:
        success = await bucket_mgr.activate_anchor(anchor_id)
        if success:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/anchors/{anchor_id}/deactivate", methods=["POST"])
async def api_deactivate_anchor(request):
    """Deactivate an anchor (dynamic anchors sink back to regular memory)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    anchor_id = request.path_params.get("anchor_id", "")
    try:
        success = await bucket_mgr.deactivate_anchor(anchor_id)
        if success:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/anchors/{anchor_id}", methods=["DELETE"])
async def api_delete_anchor(request):
    """Delete an anchor."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    anchor_id = request.path_params.get("anchor_id", "")
    try:
        success = await bucket_mgr.delete_anchor(anchor_id)
        if success:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/bucket-auto-anchor — auto detect and create anchors for high-emotion buckets
# /api/bucket-auto-anchor — 自动检测并为高情绪记忆桶创建锚点
# =============================================================
@mcp.custom_route("/api/bucket-auto-anchor", methods=["POST"])
async def api_auto_anchor(request):
    """Auto-detect high-emotion buckets and create pivot anchors (new format)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json() if request.method == "POST" else {}
        threshold = body.get("threshold", 0.6)

        all_buckets = await bucket_mgr.list_all(include_archive=False)
        all_anchors = await bucket_mgr.get_anchors()

        # Collect all bucket IDs already referenced by existing anchors
        existing_bucket_ids = set()
        for a in all_anchors:
            for bid in a.get("related_bucket_ids", []):
                existing_bucket_ids.add(bid)
            legacy_bid = a.get("bucket_id", "")
            if legacy_bid:
                existing_bucket_ids.add(legacy_bid)

        anchors_created = []
        buckets_with_emotions = 0
        buckets_below_threshold = 0
        buckets_skipped = 0

        for b in all_buckets:
            bucket_id = b.get("id")

            if bucket_id in existing_bucket_ids:
                buckets_skipped += 1
                continue

            meta = b.get("metadata", {})
            emotions = meta.get("emotions", [])

            if not isinstance(emotions, list) or len(emotions) == 0:
                continue

            buckets_with_emotions += 1

            max_intensity = 0.0
            dominant_emotion = ""
            emotion_labels = []
            for e in emotions:
                try:
                    intensity = float(e.get("intensity", 0.0))
                    label = e.get("label", "")
                    if label:
                        emotion_labels.append(label)
                    if intensity > max_intensity:
                        max_intensity = intensity
                        dominant_emotion = label
                except (ValueError, TypeError):
                    continue

            if max_intensity >= threshold:
                bucket_name = meta.get("name", b.get("name", ""))

                # --- Generate new-format anchor fields ---
                # --- 生成新格式锚点字段 ---
                triggers = []
                if dominant_emotion:
                    triggers.append(f"高{dominant_emotion}情绪")
                if bucket_name:
                    triggers.append(bucket_name)

                emotional_baseline = emotion_labels[:5] if emotion_labels else [dominant_emotion]

                # --- Auto-generate boundaries based on emotion type ---
                boundaries = ["严禁冷漠忽视", "严禁顺水推舟推开"]
                if dominant_emotion in ("anger", "愤怒", "rage"):
                    boundaries.append("严禁激化冲突")
                elif dominant_emotion in ("sadness", "悲伤", "fear", "恐惧"):
                    boundaries.append("严禁轻视感受")

                anchor = await bucket_mgr.add_anchor(
                    triggers=triggers,
                    emotional_baseline=emotional_baseline,
                    boundaries=boundaries,
                    related_bucket_ids=[bucket_id],
                    anchor_type="dynamic",
                    ttl_hours=48.0,
                    name=f"{dominant_emotion}_{bucket_name}" if bucket_name else f"{dominant_emotion}_anchor",
                )
                anchors_created.append(anchor)
            else:
                buckets_below_threshold += 1

        return JSONResponse({
            "total_scanned": len(all_buckets),
            "buckets_with_emotions": buckets_with_emotions,
            "buckets_below_threshold": buckets_below_threshold,
            "buckets_skipped": buckets_skipped,
            "anchors_created": len(anchors_created),
            "threshold_used": threshold,
            "anchors": anchors_created
        })
    except Exception as e:
        import traceback
        logger.error(f"Auto-anchor failed: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/candlesticks — candlestick operations
# /api/candlesticks — 烛台操作
# =============================================================
@mcp.custom_route("/api/candlesticks", methods=["GET"])
async def api_get_candlesticks(request):
    """Get all saved candlesticks."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        candlesticks = await bucket_mgr.get_candlesticks()
        return JSONResponse({"candlesticks": candlesticks})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/candlesticks", methods=["POST"])
async def api_create_candlestick(request):
    """Create a new candlestick."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        content = body.get("content", "")
        bucket_id = body.get("bucket_id", "")
        title = body.get("title", "")
        
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)
        
        candlestick = await bucket_mgr.save_candlestick(content, bucket_id, title)
        return JSONResponse({"candlestick": candlestick}, status_code=201)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/candlesticks/{candlestick_id}", methods=["GET"])
async def api_get_candlestick(request):
    """Get a specific candlestick by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    candlestick_id = request.path_params.get("candlestick_id", "")
    try:
        candlestick = await bucket_mgr.get_candlestick(candlestick_id)
        if candlestick:
            return JSONResponse({"candlestick": candlestick})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/candlesticks/{candlestick_id}", methods=["DELETE"])
async def api_delete_candlestick(request):
    """Delete a candlestick by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    candlestick_id = request.path_params.get("candlestick_id", "")
    try:
        success = await bucket_mgr.delete_candlestick(candlestick_id)
        if success:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/timelines — timeline operations
# /api/timelines — 时间链操作
# =============================================================
@mcp.custom_route("/api/timelines", methods=["GET"])
async def api_get_timelines(request):
    """Get all saved timelines with dynamic relative time."""
    from starlette.responses import JSONResponse
    from utils import format_relative_time
    err = _require_auth(request)
    if err: return err
    try:
        timelines = await bucket_mgr.get_timelines()
        # --- Attach dynamic relative time to each timeline and phase ---
        # --- 为每条时间链和每个阶段附加动态相对时间 ---
        for tl in timelines:
            tl["relative_created"] = format_relative_time(tl.get("created", ""))
            for phase in tl.get("phases", []):
                if isinstance(phase, dict):
                    phase["relative_time"] = format_relative_time(phase.get("time", ""))
        return JSONResponse({"timelines": timelines})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/timelines/{timeline_id}", methods=["GET"])
async def api_get_timeline(request):
    """Get a specific timeline by ID with dynamic relative time."""
    from starlette.responses import JSONResponse
    from utils import format_relative_time
    err = _require_auth(request)
    if err: return err
    timeline_id = request.path_params.get("timeline_id", "")
    try:
        timeline = await bucket_mgr.get_timeline(timeline_id)
        if timeline:
            # --- Attach dynamic relative time ---
            timeline["relative_created"] = format_relative_time(timeline.get("created", ""))
            for phase in timeline.get("phases", []):
                if isinstance(phase, dict):
                    phase["relative_time"] = format_relative_time(phase.get("time", ""))
            return JSONResponse({"timeline": timeline})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/timelines/{timeline_id}", methods=["DELETE"])
async def api_delete_timeline(request):
    """Delete a timeline by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    timeline_id = request.path_params.get("timeline_id", "")
    try:
        success = await bucket_mgr.delete_timeline(timeline_id)
        if success:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/timelines", methods=["POST"])
async def api_create_timeline(request):
    """Create a new timeline manually."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        title = body.get("title", "")
        events = body.get("events", [])
        
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        
        phases = []
        for event in events:
            phases.append({
                "time": event.get("time", ""),
                "description": event.get("description", ""),
                "key_points": event.get("key_points", []),
                "emotions": event.get("emotions", []),
            })
        
        timeline_data = {
            "title": title,
            "summary": body.get("summary", ""),
            "phases": phases,
        }
        
        timeline = await bucket_mgr.save_timeline(title, timeline_data)
        return JSONResponse({"timeline": timeline}, status_code=201)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic = bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                emotion = bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= bucket_mgr.fuzzy_threshold,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]
        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": bucket_mgr.fuzzy_threshold,
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],  # top 50 for debug
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/timeline", methods=["POST"])
async def api_timeline(request):
    """Generate a timeline by AI analysis of related memories."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    
    try:
        body = await request.json()
        query = body.get("query", "").strip()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        relevant_buckets = []
        
        if query:
            for bucket in all_buckets:
                meta = bucket.get("metadata", {})
                content = bucket.get("content", "")
                name = meta.get("name", "")
                if query.lower() in content.lower() or query.lower() in name.lower():
                    relevant_buckets.append(bucket)
        else:
            relevant_buckets = all_buckets
        
        relevant_buckets.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        
        if not relevant_buckets:
            return JSONResponse({
                "title": "未找到相关事件",
                "summary": f"没有找到与 '{query}' 相关的记忆事件",
                "phases": []
            })
        
        memories_text = ""
        for i, bucket in enumerate(relevant_buckets[:20]):
            meta = bucket.get("metadata", {})
            created = meta.get("created", "")
            name = meta.get("name", bucket["id"])
            content = bucket.get("content", "")[:500]
            emotions = meta.get("emotions", [])
            tags = meta.get("tags", [])
            
            emotions_str = ", ".join([f"{e.get('label', '')}({e.get('intensity', 0):.1f})" for e in emotions[:3]])
            tags_str = ", ".join(tags[:5])
            
            memories_text += f"""
--- 事件 {i+1} ---
时间: {created}
标题: {name}
情绪: {emotions_str if emotions_str else '无'}
标签: {tags_str if tags_str else '无'}
内容: {content}
"""
        
        if not dehydrator.api_available:
            phases = []
            for i, bucket in enumerate(relevant_buckets[:10]):
                meta = bucket.get("metadata", {})
                created = meta.get("created", "")
                name = meta.get("name", bucket["id"])
                content = bucket.get("content", "")[:200]
                emotions = meta.get("emotions", [])
                
                emotions_list = [e.get("label", "") for e in emotions[:3]]
                
                phases.append({
                    "time": created,
                    "description": content,
                    "key_points": [name],
                    "emotions": emotions_list
                })
            
            return JSONResponse({
                "title": f"'{query}' 相关事件时间线" if query else "所有事件时间线",
                "summary": f"共找到 {len(relevant_buckets)} 个相关事件，按时间顺序排列。AI 不可用时仅展示原始时间顺序。",
                "phases": phases
            })
        
        prompt = f"""
你是一个专业的事件分析专家。请根据以下记忆事件，梳理出一个清晰的时间链。

任务要求：
1. 根据事件的时间顺序和内容关联，将事件分成若干阶段（phases）
2. 每个阶段要有明确的时间范围、核心描述、关键点和情绪变化
3. 识别事件之间的因果关系和发展脉络
4. 用中文输出结构化的时间链数据

时间格式要求（重要）：
- time 字段必须使用绝对日期，格式为 YYYY-MM-DD（如 2024-03-15）
- 如果事件有明确的时间戳，直接提取日期部分
- 如果只有相对时间（如"3天前"），请根据当前日期推算出绝对日期
- 不要使用"昨天"、"上周"等相对时间描述作为 time 字段值

输入的记忆事件：
{memories_text}

输出格式（JSON）：
{{
  "title": "时间链标题",
  "summary": "对整个事件流程的简要总结（100字以内）",
  "phases": [
    {{
      "time": "2024-03-15",
      "description": "该阶段的核心事件描述",
      "key_points": ["关键点1", "关键点2"],
      "emotions": ["情绪1", "情绪2"]
    }}
  ]
}}

请直接输出JSON，不要包含markdown代码块标记。
"""
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个专业的事件分析专家，擅长梳理复杂的时间线和因果关系。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=2000
        )
        
        result_text = response.choices[0].message.content.strip()
        
        try:
            result_text = result_text.replace("```json", "").replace("```", "").strip()
            result = _json_lib.loads(result_text)
        except Exception:
            phases = []
            for i, bucket in enumerate(relevant_buckets[:10]):
                meta = bucket.get("metadata", {})
                created = meta.get("created", "")
                name = meta.get("name", bucket["id"])
                content = bucket.get("content", "")[:200]
                emotions = meta.get("emotions", [])
                
                emotions_list = [e.get("label", "") for e in emotions[:3]]
                
                phases.append({
                    "time": created,
                    "description": content,
                    "key_points": [name],
                    "emotions": emotions_list
                })
            
            result = {
                "title": f"'{query}' 相关事件时间线" if query else "所有事件时间线",
                "summary": f"共找到 {len(relevant_buckets)} 个相关事件。AI 解析失败，展示原始时间顺序。",
                "phases": phases
            }
        
        return JSONResponse(result)
    
    except Exception as e:
        logger.error(f"Timeline API error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    import time
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            content = f.read()
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "ETag": str(time.time()),
        }
        return HTMLResponse(content, headers=headers)
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@mcp.custom_route("/dashboard.js", methods=["GET"])
async def dashboard_js(request):
    """Serve the dashboard JavaScript file."""
    from starlette.responses import Response
    import os
    import time
    js_path = os.path.join(os.path.dirname(__file__), "dashboard.js")
    try:
        with open(js_path, "r", encoding="utf-8") as f:
            content = f.read()
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "ETag": str(time.time()),
        }
        return Response(content, media_type="application/javascript", headers=headers)
    except FileNotFoundError:
        return Response("console.error('dashboard.js not found');", media_type="application/javascript", status_code=404)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    dehy_api_key = dehy.get("api_key", "")
    emb_api_key = emb.get("api_key", "")
    dehy_masked_key = f"{dehy_api_key[:4]}...{dehy_api_key[-4:]}" if len(dehy_api_key) > 8 else ("***" if dehy_api_key else "")
    emb_masked_key = f"{emb_api_key[:4]}...{emb_api_key[-4:]}" if len(emb_api_key) > 8 else ("***" if emb_api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": dehy_masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
            "base_url": emb.get("base_url", ""),
            "api_key_masked": emb_masked_key,
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "") or os.environ.get("OMBRE_DEHYDRATION_BASE_URL", "") or "https://api.deepseek.com/v1"
        dehydrator.api_key = dehy.get("api_key", "") or os.environ.get("OMBRE_API_KEY", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")
        if "base_url" in e:
            emb["base_url"] = e["base_url"]
            embedding_engine.base_url = e["base_url"] or dehy.get("base_url", "") or os.environ.get("OMBRE_DEHYDRATION_BASE_URL", "") or "https://api.deepseek.com/v1"
            updated.append("embedding.base_url")
        if "api_key" in e and e["api_key"]:
            emb["api_key"] = e["api_key"]
            embedding_engine.api_key = e["api_key"]
            updated.append("embedding.api_key")
        if hasattr(embedding_engine, "client") and embedding_engine.api_key:
            from openai import AsyncOpenAI
            embedding_engine.client = AsyncOpenAI(
                api_key=embedding_engine.api_key,
                base_url=embedding_engine.base_url,
            )

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature", "api_key"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model", "base_url", "api_key"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/ai-test — Test AI API connection
# =============================================================
@mcp.custom_route("/api/ai-test", methods=["POST"])
async def api_ai_test(request):
    """Test AI API connection."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        if not dehydrator or not dehydrator.api_available:
            return JSONResponse({"ok": False, "error": "API Key未配置"}, status_code=400)
        
        response = await dehydrator.client.chat.completions.create(
            model=dehydrator.model,
            messages=[
                {"role": "system", "content": "你是一个测试助手，只需回复'OK'即可。"},
                {"role": "user", "content": "测试连接"},
            ],
            max_tokens=10,
            temperature=0,
        )
        
        if response.choices and response.choices[0].message.content:
            content = response.choices[0].message.content.strip()
            return JSONResponse({
                "ok": True,
                "model": dehydrator.model,
                "base_url": dehydrator.base_url,
                "response": content,
                "latency_ms": int(response.usage.total_tokens * 10),
            })
        return JSONResponse({"ok": False, "error": "API返回空内容"}, status_code=500)
    
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# =============================================================
# /api/echo-chamber — Echo Chamber API
# =============================================================
@mcp.custom_route("/api/echo-chamber", methods=["GET"])
async def api_echo_chamber(request):
    """Get echo chamber data including digests and pending actions."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        await housekeeper.ensure_started()
        summary = await housekeeper.review_digest()
        return JSONResponse(summary)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/echo-chamber/approve", methods=["POST"])
async def api_echo_chamber_approve(request):
    """Approve a pending action."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        action_id = body.get("action_id", "")
        if not action_id:
            return JSONResponse({"error": "action_id required"}, status_code=400)
        
        await approve_action(action_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/echo-chamber/reject", methods=["POST"])
async def api_echo_chamber_reject(request):
    """Reject a pending action."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        action_id = body.get("action_id", "")
        if not action_id:
            return JSONResponse({"error": "action_id required"}, status_code=400)
        
        await reject_action(action_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/run-housekeeper", methods=["POST"])
async def api_run_housekeeper(request):
    """Run daily housekeeper job."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        await housekeeper.ensure_started()
        results = await housekeeper.run_daily_job()
        
        parts = []
        daily_summary = results.get("daily_summary", {})
        if "error" in daily_summary:
            parts.append(f"❌ 每日总结失败: {daily_summary['error']}")
        else:
            parts.append(f"✅ 每日总结: 处理{daily_summary.get('buckets_processed', 0)}条记忆")
        
        chain_updates = results.get("chain_updates", {})
        if "error" in chain_updates:
            parts.append(f"❌ 时间链更新失败: {chain_updates['error']}")
        else:
            parts.append(f"✅ 时间链更新: 更新{chain_updates.get('chains_updated', 0)}条链")
        
        conflicts = results.get("conflicts", {})
        if "error" in conflicts:
            parts.append(f"❌ 冲突检测失败: {conflicts['error']}")
        else:
            parts.append(f"✅ 冲突检测: 发现{conflicts.get('conflicts_found', 0)}条冲突")
        
        return JSONResponse("\n".join(parts))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/ai-chat — AI chat interface (calls ai_manage internally)
# =============================================================
@mcp.custom_route("/api/ai-chat", methods=["POST"])
async def api_ai_chat(request):
    """AI chat interface - sends message to ai_manage and returns response."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"ok": False, "error": "消息不能为空"}, status_code=400)
        
        result = await ai_manage(message)
        
        return JSONResponse({
            "ok": True,
            "response": result,
        })
    except Exception as e:
        logger.error(f"api_ai_chat failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# /api/analytics — analytics and statistics
# /api/analytics — 分析统计
# =============================================================
# =============================================================
# /api/export — export memories
# /api/export — 导出记忆
# =============================================================
@mcp.custom_route("/api/export", methods=["GET"])
async def api_export(request):
    """Export memories in JSON format."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        export_type = request.query_params.get("type", "all")
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        
        if export_type != "all":
            buckets = [b for b in all_buckets if b.get("metadata", {}).get("type") == export_type]
        else:
            buckets = all_buckets
        
        export_data = {
            "export_time": datetime.datetime.now().isoformat(),
            "total_count": len(buckets),
            "type": export_type,
            "buckets": buckets,
        }
        
        return JSONResponse(export_data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/buckets/batch", methods=["DELETE"])
async def api_batch_delete_buckets(request):
    """Batch delete buckets."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        bucket_ids = body.get("ids", [])
        
        deleted = 0
        for bucket_id in bucket_ids:
            success = await bucket_mgr.delete(bucket_id)
            if success:
                deleted += 1
        
        return JSONResponse({
            "success": True,
            "deleted": deleted,
            "total_requested": len(bucket_ids),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/analytics", methods=["GET"])
async def api_analytics(request):
    """Get core analytics data."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        
        type_counts = {}
        domain_counts = {}
        date_counts = {}
        month_emotions = {}
        date_emotions = {}
        
        total_valence = 0.0
        total_arousal = 0.0
        val_count = 0
        aro_count = 0
        
        emotion_polarity = {
            '愤怒': -1, '生气': -1, '烦恼': -1, '暴怒': -1,
            '恐惧': -1, '害怕': -1, '焦虑': -1, '不安': -1,
            '悲伤': -1, '忧伤': -1, '悲痛': -1, '绝望': -1,
            '厌恶': -1, '反感': -1, '不悦': -1, '憎恨': -1,
            '喜悦': 1, '快乐': 1, '满意': 1, '幸福': 1,
            '信任': 1, '热爱': 1, '接受': 1, '迷恋': 1,
            '期待': 1, '希望': 1, '兴奋': 1, '狂喜': 1,
            '惊讶': 1, '好奇': 1, '震惊': 1, '惊愕': 1,
        }
        
        for b in all_buckets:
            meta = b.get("metadata", {})
            emotions = meta.get("emotions", [])
            valence = meta.get("valence", None)
            arousal = meta.get("arousal", None)
            b_type = meta.get("type", "dynamic")
            domains = meta.get("domain", [])
            created = meta.get("created", "")
            date_key = created[:10] if created else ""
            month_key = created[:7] if created else ""
            
            if month_key:
                if month_key not in month_emotions:
                    month_emotions[month_key] = {"positive": 0, "negative": 0, "count": 0}
            if date_key:
                if date_key not in date_emotions:
                    date_emotions[date_key] = {"positive": 0, "negative": 0, "count": 0}
            
            if emotions and isinstance(emotions, list) and len(emotions) > 0:
                for emo in emotions:
                    if isinstance(emo, dict):
                        emo_label = emo.get("label", str(emo))
                        intensity = float(emo.get("intensity", 0.5))
                    else:
                        emo_label = str(emo)
                        intensity = 0.5
                    
                    polarity = emotion_polarity.get(emo_label, 0)
                    if month_key:
                        if polarity > 0:
                            month_emotions[month_key]["positive"] += intensity
                        elif polarity < 0:
                            month_emotions[month_key]["negative"] += intensity
                        month_emotions[month_key]["count"] += 1
                    if date_key:
                        if polarity > 0:
                            date_emotions[date_key]["positive"] += intensity
                        elif polarity < 0:
                            date_emotions[date_key]["negative"] += intensity
                        date_emotions[date_key]["count"] += 1
            
            if valence is not None:
                total_valence += valence
                val_count += 1
            if arousal is not None:
                total_arousal += arousal
                aro_count += 1
            
            type_counts[b_type] = type_counts.get(b_type, 0) + 1
            
            for d in domains:
                domain_counts[d] = domain_counts.get(d, 0) + 1
            
            if created:
                date_counts[date_key] = date_counts.get(date_key, 0) + 1
        
        sorted_dates = sorted(date_counts.items(), key=lambda x: x[0])[-7:]
        
        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        
        avg_valence = round(total_valence / val_count, 2) if val_count > 0 else 0.5
        avg_arousal = round(total_arousal / aro_count, 2) if aro_count > 0 else 0.3
        
        import datetime
        today = datetime.date.today()
        months_data = []
        emotion_dates = []
        max_intensity = 1
        
        for date_key in sorted(date_emotions.keys()):
            emo = date_emotions[date_key]
            emotion_dates.append({
                "date": date_key,
                "positive": round(emo["positive"], 2),
                "negative": round(emo["negative"], 2),
                "count": emo["count"],
            })
            max_intensity = max(max_intensity, emo["positive"], emo["negative"])
        
        for month_key in sorted(month_emotions.keys()):
            emo = month_emotions[month_key]
            months_data.append({
                "month": month_key,
                "positive": round(emo["positive"], 2),
                "negative": round(emo["negative"], 2),
                "count": emo["count"],
            })
            max_intensity = max(max_intensity, emo["positive"], emo["negative"])
        
        recent_activity = []
        if sorted_dates:
            recent_activity = [{"date": d[0], "count": d[1]} for d in sorted_dates]
        else:
            for i in range(6, -1, -1):
                d = today - datetime.timedelta(days=i)
                recent_activity.append({"date": str(d), "count": 0})
        
        return JSONResponse({
            "total_buckets": len(all_buckets),
            "type_counts": type_counts,
            "domain_counts": dict(top_domains),
            "avg_valence": avg_valence,
            "avg_arousal": avg_arousal,
            "month_data": months_data,
            "emotion_dates": emotion_dates,
            "max_emotion_intensity": max_intensity,
            "recent_activity": recent_activity,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/export-brain", methods=["POST"])
async def api_export_brain(request):
    """Export brain data (buckets/ and database) to zip file."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        output_path = body.get("output_path", "")
        result = await export_brain(output_path)
        
        if result.startswith("大脑导出成功"):
            lines = result.split('\n')
            path = lines[1].replace("文件路径: ", "").strip() if len(lines) > 1 else ""
            size = lines[2].replace("大小: ", "").strip() if len(lines) > 2 else ""
            return JSONResponse({"ok": True, "path": path, "size": size, "message": result})
        else:
            return JSONResponse({"ok": False, "error": result}, status_code=500)
    except Exception as e:
        logger.error(f"API export-brain failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@mcp.custom_route("/api/import-brain", methods=["POST"])
async def api_import_brain(request):
    """Import brain data from zip file."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        zip_path = body.get("zip_path", "")
        overwrite = body.get("overwrite", False)
        
        if not zip_path:
            return JSONResponse({"ok": False, "error": "请提供 zip 文件路径"}, status_code=400)
        
        result = await import_brain(zip_path, overwrite)
        
        if result.startswith("大脑导入成功"):
            return JSONResponse({"ok": True, "message": result})
        else:
            return JSONResponse({"ok": False, "error": result}, status_code=500)
    except Exception as e:
        logger.error(f"API import-brain failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)
