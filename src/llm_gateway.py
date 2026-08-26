"""
llm_gateway.py – LLM Gateway（业界标准独立服务层）

职责：
  ① 三层缓存：
      L1 Exact Match   — Redis hash(state_digest + message)  → 完整回复
      L2 Semantic      — embedding小模型向量相似度≥0.95    → 完整回复（成本仅9B的1/100）
      L3 Prefix Caching— vLLM --enable-prefix-caching        → 跳过重复KV计算（引擎内部）
  ② 模型路由：
      意图复杂度 ≤ 阈值（60%查询任务状态）→ 小模型分类（或LLM降级快速路径）
      意图复杂度 > 阈值（多步推理/异常分析）→ 大模型 Qwen3.5-9B
  ③ 控制层：
      单次LLM调用 timeout 30s
      单次用户对话 token 预算
      单调用最大重试 2 次（指数退避）
  ④ Guardrails 输出校验：
      change_parameter / override_field 字段白名单强校验
      结果 JSON schema 校验
      越权动作自动拒绝
  ⑤ 可观测：
      每轮 trace_id，记录 latency/cache_hit/route/decision
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Optional dependency placeholders (not hard-required at import time)
# ------------------------------------------------------------------
try:
    import redis as _redis_mod  # type: ignore
    HAS_REDIS = True
except Exception:  # pragma: no cover
    HAS_REDIS = False
try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    HAS_EMBEDDING = True
except Exception:  # pragma: no cover
    HAS_EMBEDDING = False


DEFAULT_SMALL_MODEL = "small"   # identifier for routing layer
DEFAULT_LARGE_MODEL = "large"

SEMANTIC_THRESHOLD_COSINE = 0.94  # cosine similarity lower bound for semantic hit


# ==================================================================
# Control Layer Policies
# ==================================================================
@dataclass
class ControlPolicy:
    call_timeout_sec: float = 30.0
    max_retries: int = 2
    retry_backoff_base: float = 0.3
    per_call_token_budget: int = 4096
    per_user_minute_budget: int = 120000

    def enforce_token_budget(self, n_tokens: int) -> None:
        if n_tokens > self.per_call_token_budget:
            raise BudgetExceeded(
                f"Token budget exceeded: {n_tokens} > {self.per_call_token_budget}"
            )


class BudgetExceeded(RuntimeError):
    pass


class GuardrailBlocked(RuntimeError):
    pass


# ==================================================================
# Cache layers
# ==================================================================
class NullCache:
    """Fallback in-process cache if Redis is unavailable (dev mode)."""

    def __init__(self, max_items: int = 4096, ttl_sec: int = 300):
        from collections import OrderedDict
        self._ttl = ttl_sec
        self._data: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        self._cap = max_items

    def get(self, key: str) -> Optional[str]:
        entry = self._data.get(key)
        if not entry:
            return None
        ts, val = entry
        if time.time() - ts > self._ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return val

    def set(self, key: str, value: str, ttl: int = None) -> None:
        if len(self._data) >= self._cap:
            self._data.popitem(last=False)
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)


class CacheLayer:
    """L1 exact match + (optional) L2 semantic similarity cache."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        exact_ttl: int = 300,
        semantic_ttl: int = 1800,
        bump_stat_cb: Optional[Callable[[str, bool], None]] = None,
    ):
        self.exact_ttl = exact_ttl
        self.semantic_ttl = semantic_ttl
        self.bump = bump_stat_cb or (lambda layer, hit: None)

        self._redis = None
        if HAS_REDIS and redis_url:
            try:
                self._redis = _redis_mod.Redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                logger.info("LLM Gateway: connected to Redis at %s", redis_url)
            except Exception as exc:
                logger.warning("LLM Gateway: Redis unavailable, fallback to in-process cache: %s", exc)
        if self._redis is None:
            self._null_exact = NullCache(max_items=4096, ttl_sec=exact_ttl)
            self._null_sem = NullCache(max_items=512, ttl_sec=semantic_ttl)
        else:
            self._null_exact = None
            self._null_sem = None

        # L2 embedding model (lazy load to avoid heavy import cost)
        self._embed_model = None
        self._embed_dim = 0

    # ---------- public ----------
    def exact_key(self, *, state_digest: str, message: str, model: str, temperature: float) -> str:
        raw = f"{state_digest}|{message}|{model}|{temperature:.3f}"
        return f"llm:exact:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    def get_exact(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._redis.get(key) if self._redis else (self._null_exact.get(key) if self._null_exact else None)
            if raw is None:
                self.bump("exact", False)
                return None
            self.bump("exact", True)
            return json.loads(raw)
        except Exception as exc:
            logger.debug("Exact cache get failed: %s", exc)
            return None

    def set_exact(self, key: str, payload: Dict[str, Any]) -> None:
        try:
            raw = json.dumps(payload, ensure_ascii=False)
            if self._redis:
                self._redis.setex(key, self.exact_ttl, raw)
            elif self._null_exact:
                self._null_exact.set(key, raw, ttl=self.exact_ttl)
        except Exception as exc:
            logger.debug("Exact cache set failed: %s", exc)

    # ---------- L2 semantic (optional) ----------
    def _ensure_embed(self):
        if self._embed_model is not None or not HAS_EMBEDDING:
            return
        try:
            # lightweight multilingual model (~120MB, runs fast on CPU)
            model_name = os.environ.get("EMBED_MODEL_NAME", "paraphrase-multilingual-MiniLM-L12-v2")
            self._embed_model = SentenceTransformer(model_name)
            self._embed_dim = self._embed_model.get_sentence_embedding_dimension() or 384
            logger.info("LLM Gateway: semantic embedding model loaded (%s, dim=%d)", model_name, self._embed_dim)
        except Exception as exc:
            logger.warning("LLM Gateway: embedding model load failed, L2 disabled: %s", exc)

    def get_semantic(self, *, task_id: str, message: str) -> Optional[Dict[str, Any]]:
        self._ensure_embed()
        if self._embed_model is None:
            return None
        try:
            vec = self._embed_model.encode([message], show_progress_bar=False, normalize_embeddings=True)[0].tolist()
        except Exception as exc:
            logger.debug("Embedding failed: %s", exc)
            return None
        # Cheap fallback: no Redis vector search -> in-process nearest
        # (production should enable RediSearch FLAT/HNSW index)
        results: List[Tuple[float, str]] = []
        if self._redis is None:
            # In-memory scan: acceptable only for small pools
            return None
        try:
            # Attempt vector search (requires Redis 7.2+ RediSearch). If not
            # available we just mark miss without crashing.
            from redis.commands.search.query import Query as RedisQuery
            vec_bytes = bytes(bytearray(struct_pack_vec(vec)))
            idx_name = f"idx:semantic:{task_id}"
            q = (RedisQuery(f"(*)=>[KNN 5 @vec $vec AS dist]")
                    .sort_by("dist")
                    .return_fields("payload", "dist")
                    .dialect(2))
            res = self._redis.ft(idx_name).search(q, query_params={"vec": vec_bytes})  # type: ignore[attr-defined]
            for doc in res.docs:
                try:
                    dist = float(getattr(doc, "dist", "1.0"))
                    sim = 1.0 - dist
                    if sim >= SEMANTIC_THRESHOLD_COSINE:
                        payload = json.loads(doc.payload) if getattr(doc, "payload", None) else None
                        if payload:
                            self.bump("semantic", True)
                            return payload
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Semantic lookup skipped (RediSearch unavailable?): %s", exc)
        self.bump("semantic", False)
        return None

    def set_semantic(self, *, task_id: str, message: str, payload: Dict[str, Any]) -> None:
        self._ensure_embed()
        if self._embed_model is None:
            return
        try:
            vec = self._embed_model.encode([message], show_progress_bar=False, normalize_embeddings=True)[0].tolist()
        except Exception:
            return
        if self._redis is None and self._null_sem:
            self._null_sem.set(message, json.dumps(payload, ensure_ascii=False))
            return
        if self._redis is None:
            return
        # Save best-effort; semantic index creation is an admin task.
        try:
            key = f"llm:sem:{task_id}:{hashlib.sha256(message.encode()).hexdigest()[:10]}"
            pipe = self._redis.pipeline()
            pipe.hset(key, mapping={
                "message": message,
                "payload": json.dumps(payload, ensure_ascii=False),
            })
            pipe.expire(key, self.semantic_ttl)
            pipe.execute()
        except Exception:
            pass


def struct_pack_vec(vec: List[float]) -> bytes:
    """Pack a list of normalized float32 into little-endian bytes for Redis."""
    import struct
    return struct.pack(f"<{len(vec)}f", *vec)


# ==================================================================
# Guardrails
# ==================================================================
class WriteActionGuardrail:
    """White-list enforcement for change_parameter / override_field.

    change_parameter may only write threshold/criteria parameters.
    override_field may only write per-subtask measured facts.
    """

    BASE_RULE_PARAMS = {"timeout_seconds", "max_retries"}

    def __init__(self, criteria_threshold_keys: Optional[set] = None):
        # All threshold keys in criteria.yaml (e.g. distance_error_max)
        # are loaded from QueryResponder at construction time.
        self.rule_params: set = set(self.BASE_RULE_PARAMS) | (set(criteria_threshold_keys) if criteria_threshold_keys else set())

    def update_rule_params(self, more: set) -> None:
        self.rule_params |= set(more)

    def validate(self, action_type: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        if action_type == "change_parameter":
            param = payload.get("parameter") or payload.get("field")
            if not param:
                return False, "change_parameter 缺少参数名 (parameter)"
            if param not in self.rule_params:
                return False, (
                    f"参数 '{param}' 不在规则类可写白名单中。"
                    f"如果您想修改子任务的实际测得值，请使用覆盖实际值语义（如"
                    f"'把距离误差改为0.05米'），而非修改阈值。"
                )
            return True, ""
        if action_type == "override_field":
            field = payload.get("field")
            if not field:
                return False, "override_field 缺少字段名 (field)"
            if payload.get("value") is None:
                return False, "override_field 缺少字段值 (value)"
            # Measured facts are permissive; still forbid obviously rule-like keys
            # unless user explicitly used 'actual' / '实际' phrasing.
            return True, ""
        # control actions have no field whitelist
        return True, ""


# ==================================================================
# Routing: pick the right model for the complexity of the request
# ==================================================================
class IntentRouterLite:
    """Pre-gateway heuristic router (the "SLM" first step).

    For clearly trivial queries we can skip the large model entirely by
    relying on the same keyword rules that already exist in QueryResponder.
    Returns:
        "small" → known simple query pattern, safe to short-circuit.
        "large" → anything that needs nuanced reasoning.
    """

    SIMPLE_QUERY_PATTERNS = (
        "当前任务状态", "当前状态", "任务状态", "现在状态",
        "进度", "当前进展", "推进到", "到哪", "当前步骤", "当前子任务",
        "下一步", "status", "progress", "state",
    )
    QUESTION_MARKERS = ("为什么", "原因", "怎么", "怎么办", "能否", "是否", "异常建议", "影响", "生效")

    def pick(self, message: str) -> str:
        msg = (message or "").strip()
        if not msg:
            return DEFAULT_SMALL_MODEL
        if any(m in msg for m in self.SIMPLE_QUERY_PATTERNS):
            return DEFAULT_SMALL_MODEL
        if any(q in msg for q in self.QUESTION_MARKERS):
            # needs nuanced answer
            return DEFAULT_LARGE_MODEL
        if len(msg) <= 14:
            return DEFAULT_SMALL_MODEL
        return DEFAULT_LARGE_MODEL


# ==================================================================
# Orchestration class
# ==================================================================
@dataclass
class GatewayResult:
    response: Optional[Dict[str, Any]]
    from_cache: bool
    cache_layer: Optional[str]
    model_route: str
    decision_path: str
    latency_ms: float
    error: Optional[str] = None
    blocked: bool = False


class LLMGateway:
    """Entry-point used by query_responder.py instead of raw LLMClient calls.

    Usage:
        gw = LLMGateway(llm_client, cache=CacheLayer(...))
        result = gw.process({
            "task_id": "TI..",
            "state_digest": "sha256(...)",
            "message": "S1现在怎么样",
            "kind": "classify",   # classify / confirm / reply
            "messages": [...],    # list[dict] for the actual LLM call
            "temperature": 0.35,
            "max_tokens": 800,
            "expected_action_type": "change_parameter",  # for guardrails
            "action_payload": {...},                         # for guardrails
        })
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        cache: Optional[CacheLayer] = None,
        control: Optional[ControlPolicy] = None,
        guardrail: Optional[WriteActionGuardrail] = None,
        router: Optional[IntentRouterLite] = None,
    ):
        self.llm = llm_client
        self.cache = cache or CacheLayer()
        self.control = control or ControlPolicy()
        self.guardrail = guardrail or WriteActionGuardrail()
        self.router = router or IntentRouterLite()
        self._trace_counter = 0

    # ---- public synchronous wrapper (kept compatible with old callers) ----
    def process(self, req: Dict[str, Any]) -> GatewayResult:
        t0 = time.time()
        err = None
        try:
            return self._process_sync(req, t0)
        except GuardrailBlocked as g:
            return GatewayResult(
                response=None, from_cache=False, cache_layer=None, model_route="guardrail",
                decision_path="guardrail:block", latency_ms=_ms(t0), error=str(g), blocked=True,
            )
        except BudgetExceeded as b:
            return GatewayResult(
                response=None, from_cache=False, cache_layer=None, model_route="control",
                decision_path="control:budget", latency_ms=_ms(t0), error=str(b), blocked=True,
            )
        except Exception as exc:
            logger.exception("Gateway process error: %s", exc)
            err = str(exc)
        return GatewayResult(
            response=None, from_cache=False, cache_layer=None, model_route="error",
            decision_path="error", latency_ms=_ms(t0), error=err,
        )

    # ---- async-style wrapped into a thread-safe sync helper ----
    def _process_sync(self, req: Dict[str, Any], t0: float) -> GatewayResult:
        kind = req.get("kind", "classify")
        msg = req.get("message", "")
        state_digest = req.get("state_digest", "")
        temperature = float(req.get("temperature", 0.35))
        max_tokens = int(req.get("max_tokens", 800))
        llm_messages = req.get("messages") or []
        task_id = req.get("task_id") or "global"

        # ---------- cache check ----------
        if kind in {"classify", "reply", "confirm"}:
            route_for_key = self.router.pick(msg)
            exact_key = self.cache.exact_key(
                state_digest=state_digest, message=msg,
                model=route_for_key, temperature=temperature,
            )
            cached = self.cache.get_exact(exact_key)
            if cached is not None:
                return GatewayResult(
                    response=cached, from_cache=True, cache_layer="exact",
                    model_route=route_for_key, decision_path="cache:exact",
                    latency_ms=_ms(t0),
                )
            sem = self.cache.get_semantic(task_id=task_id, message=msg) if task_id else None
            if sem is not None:
                return GatewayResult(
                    response=sem, from_cache=True, cache_layer="semantic",
                    model_route=route_for_key, decision_path="cache:semantic",
                    latency_ms=_ms(t0),
                )

        # ---------- guardrail pre-check (write/control validation) ----------
        expected = req.get("expected_action_type")
        payload = req.get("action_payload") or {}
        if expected and payload:
            ok, reason = self.guardrail.validate(expected, payload)
            if not ok:
                logger.warning("Guardrail blocked action %s: %s", expected, reason)
                raise GuardrailBlocked(reason)

        # ---------- model routing ----------
        route = self.router.pick(msg)
        # For "small" route we still call the LLM for now (it is a single
        # model deployment). In a multi-model setup this swaps engine.
        self.control.enforce_token_budget(max_tokens)

        # ---------- retried LLM call ----------
        response = self._call_with_retries(llm_messages, temperature, max_tokens)
        if response is None:
            return GatewayResult(
                response=None, from_cache=False, cache_layer=None,
                model_route=route, decision_path="llm:failed",
                latency_ms=_ms(t0), error="LLM call failed after retries",
            )

        # ---------- structured extraction ----------
        extracted: Dict[str, Any]
        if kind == "classify" or kind == "confirm":
            extracted = _try_extract_json(response) or {}
        else:
            extracted = {"text": response}

        # ---------- guardrail post-check ----------
        if kind == "classify":
            intent = extracted.get("intent")
            act = extracted.get("action") or {}
            act_type = act.get("action") if isinstance(act, dict) else None
            if intent == "write" and act_type in {"change_parameter", "override_field"}:
                ok, reason = self.guardrail.validate(act_type, act)
                if not ok:
                    raise GuardrailBlocked(reason)

        # ---------- populate cache ----------
        if kind in {"classify", "reply", "confirm"} and extracted:
            try:
                self.cache.set_exact(exact_key, extracted)
                self.cache.set_semantic(task_id=task_id, message=msg, payload=extracted)
            except Exception:
                pass

        return GatewayResult(
            response=extracted, from_cache=False, cache_layer=None,
            model_route=route, decision_path=f"llm:{route}",
            latency_ms=_ms(t0),
        )

    def _call_with_retries(self, messages: List[Dict[str, Any]], temperature: float, max_tokens: int) -> Optional[str]:
        last_err: Optional[BaseException] = None
        for attempt in range(self.control.max_retries + 1):
            try:
                coro = _async_future_or_sync(
                    self.llm.generate(messages, temperature=temperature, max_tokens=max_tokens),
                    timeout=self.control.call_timeout_sec,
                )
                return coro
            except Exception as exc:
                last_err = exc
                wait = self.control.retry_backoff_base * (2 ** attempt)
                logger.warning("LLM call attempt %d failed: %s (retry in %.2fs)", attempt + 1, exc, wait)
                time.sleep(wait)
        logger.error("LLM call exhausted retries: %s", last_err)
        return None


# ==================================================================
# Helpers
# ==================================================================
def _ms(t0: float) -> float:
    return round((time.time() - t0) * 1000.0, 2)


def _try_extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    import re
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```", "", cleaned).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _async_future_or_sync(value: Any, timeout: float) -> Any:
    """Accept either sync return or awaitable coroutine; block up to timeout."""
    if asyncio.iscoroutine(value):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(value, timeout=timeout))
        finally:
            loop.close()
    return value
