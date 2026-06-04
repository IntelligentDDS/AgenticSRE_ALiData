"""
AgenticSRE LLM Client
OpenAI-compatible LLM wrapper supporting chat and structured JSON output.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from openai import OpenAI

from configs.config_loader import get_config

logger = logging.getLogger(__name__)


class LLMClient:
    """OpenAI-compatible LLM client with JSON mode support."""

    def __init__(self, config=None):
        cfg = config or get_config().llm
        self.client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
        )
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> str:
        """Send chat completion request and return text response."""
        try:
            resp = self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens or self.max_tokens,
            )
            content = resp.choices[0].message.content or ""
            logger.debug(f"LLM response ({len(content)} chars, "
                        f"tokens: {resp.usage.total_tokens if resp.usage else '?'})")
            return content
        except Exception as e:
            logger.error(f"LLM chat failed: {e}")
            raise

    def json_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send chat completion and parse JSON response."""
        # Append JSON instruction
        system_msg = messages[0] if messages and messages[0]["role"] == "system" else None
        if system_msg:
            if "json" not in system_msg["content"].lower():
                messages = list(messages)
                messages[0] = {
                    "role": "system",
                    "content": system_msg["content"] + "\n\nRespond with valid JSON only."
                }

        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens)

        # Try to extract JSON from markdown code blocks
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            inside = False
            for line in lines:
                if line.strip().startswith("```") and not inside:
                    inside = True
                    continue
                elif line.strip() == "```" and inside:
                    break
                elif inside:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM JSON response, returning raw text wrapper")
            return {"raw_response": text, "parse_error": True}

    def summarize(self, text: str, instruction: str = "Summarize the following") -> str:
        """Convenience method for text summarization."""
        return self.chat([
            {"role": "system", "content": "You are a concise technical summarizer."},
            {"role": "user", "content": f"{instruction}:\n\n{text}"}
        ])

    async def async_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> str:
        """Async version of chat — runs sync LLM call in thread pool."""
        return await asyncio.to_thread(
            self.chat, messages, temperature, max_tokens, model
        )

    async def async_json_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Async version of json_chat — runs sync LLM call in thread pool."""
        return await asyncio.to_thread(
            self.json_chat, messages, temperature, max_tokens
        )
