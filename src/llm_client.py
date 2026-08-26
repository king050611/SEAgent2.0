"""
llm_client.py – 本地 vLLM 推理接口封装（升级版）。

支持同步 generate() 接口 + 异步 generate_async() 接口，并通过超时/重试
策略与 LLM Gateway 的控制层保持兼容。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Awaitable, List, Optional, Union

logger = logging.getLogger(__name__)

try:
    from vllm import SamplingParams
except Exception:  # pragma: no cover - 仅用于无 vLLM 的静态测试环境
    SamplingParams = None


class LLMClient:
    """封装本地 vLLM 推理引擎。

    - generate(): 完全兼容的同步接口（老代码不用改）
    - generate_async(): 接入 vLLM AsyncLLMEngine 的异步接口（Gateway 用）
    - 超时/重试/控制层策略由 LLM Gateway 负责，本类保持轻量。
    """

    def __init__(self, llm_instance: Any = None, tokenizer: Any = None, *, async_engine: Any = None):
        self.llm = llm_instance
        self.tok = tokenizer
        self.async_llm = async_engine
        self._lock = threading.Lock()

    # ---------- prompt building ----------
    def _build_prompt(self, messages: list, enable_thinking: bool = False) -> str:
        if self.tok is None:
            # Fallback for environments without tokenizer: raw text concatenation
            parts = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
            parts.append("<|im_start|>assistant\n")
            return "".join(parts)
        kwargs = dict(tokenize=False, add_generation_prompt=True)
        if enable_thinking:
            kwargs["enable_thinking"] = enable_thinking
        try:
            return self.tok.apply_chat_template(messages, **kwargs)
        except Exception:
            # Some tokenizer implementations do not accept enable_thinking
            kwargs.pop("enable_thinking", None)
            return self.tok.apply_chat_template(messages, **kwargs)

    # ---------- public: synchronous (original API kept intact) ----------
    def generate(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: Optional[List[str]] = None,
        timeout_sec: Optional[float] = None,
    ) -> str:
        if SamplingParams is None:
            raise RuntimeError("vLLM is not installed; LLMClient.generate requires vllm.SamplingParams")
        prompt = self._build_prompt(messages)
        sampling_params = SamplingParams(
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop or [],
        )
        if self.llm is None and self.async_llm is not None:
            return asyncio.run(self._async_call(prompt, sampling_params, timeout_sec))
        with self._lock:
            outputs = self.llm.generate([prompt], sampling_params)
        return outputs[0].outputs[0].text.strip()

    # ---------- public: async wrapper ----------
    async def generate_async(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: Optional[List[str]] = None,
        timeout_sec: Optional[float] = None,
    ) -> str:
        if SamplingParams is None:
            raise RuntimeError("vLLM is not installed; LLMClient.generate_async requires vllm.SamplingParams")
        prompt = self._build_prompt(messages)
        sampling_params = SamplingParams(
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop or [],
        )
        if self.async_llm is None:
            return await asyncio.to_thread(self._sync_call, prompt, sampling_params, timeout_sec)
        return await self._async_call(prompt, sampling_params, timeout_sec)

    # ---------- internal ----------
    def _sync_call(self, prompt: str, sampling_params: Any, timeout: Optional[float]) -> str:
        start = time.time()
        with self._lock:
            outputs = self.llm.generate([prompt], sampling_params)
        text = outputs[0].outputs[0].text.strip()
        if timeout and (time.time() - start) > timeout:
            logger.warning("sync LLM call timed out after %.2fs (returning what we have)", time.time() - start)
        return text

    async def _async_call(self, prompt: str, sampling_params: Any, timeout: Optional[float]) -> str:
        if self.async_llm is None:
            raise RuntimeError("AsyncLLMEngine not attached")
        try:
            final = None
            async for output in self.async_llm.generate(prompt, sampling_params, request_id=str(id(prompt))):
                final = output
            if final is None:
                return ""
            return final.outputs[0].text.strip()
        except asyncio.TimeoutError:
            logger.warning("AsyncLLMEngine timed out")
            raise
        except Exception as exc:
            logger.error("AsyncLLMEngine error: %s", exc)
            raise

    # ---------- helpers ----------
    def extract_json(
        self,
        messages: List[dict],
        max_tokens: int = 800,
        temperature: float = 0.1,
    ) -> Optional[dict]:
        raw = self.generate(messages, temperature=temperature, max_tokens=max_tokens)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        if not text:
            return None
        cleaned = re.sub(r"```(?:json)?\s*", "", text)
        cleaned = re.sub(r"```", "", cleaned).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
