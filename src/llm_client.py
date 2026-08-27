"""
llm_client.py — 本地 vLLM 推理接口封装。

保持原接口不变；同时让模块在未安装 vLLM 的测试环境中可被导入，
真正调用 generate 时再给出清晰错误。
"""

import json
import re
from typing import Any

try:
    from vllm import SamplingParams
except Exception:  # pragma: no cover - 仅用于无 vLLM 的静态测试环境
    SamplingParams = None


class LLMClient:
    def __init__(self, llm_instance: Any, tokenizer: Any):
        self.llm = llm_instance
        self.tok = tokenizer

    def _build_prompt(self, messages: list[dict], enable_thinking: bool = False) -> str:
        return self.tok.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=enable_thinking,
        )

    def generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: list[str] | None = None,
    ) -> str:
        if SamplingParams is None:
            raise RuntimeError("vLLM is not installed; LLMClient.generate requires vllm.SamplingParams")
        prompt = self._build_prompt(messages)
        sampling_params = SamplingParams(
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop or [],
        )
        outputs = self.llm.generate([prompt], sampling_params)
        return outputs[0].outputs[0].text.strip()

    def extract_json(
        self,
        messages: list[dict],
        max_tokens: int = 800,
    ) -> dict | None:
        raw = self.generate(messages, temperature=0.1, max_tokens=max_tokens)
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"```", "", cleaned).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
