"""Local Ollama, wrapped to the LLMProvider interface.

STUB -- not wired into pipeline.py by default. This exists to prove the
interface is honest (a second real implementation, not just one class
pretending to be an abstraction) and to give you a concrete starting point
once you want cheap/local extraction for simple pages, reserving Anthropic
calls for the harder analyze.py judgment calls.

Ollama's /api/chat does not support forced tool-choice as reliably as
Anthropic's API -- smaller local models frequently ignore or malform tool
schemas. Treat this as needing a validate-and-retry loop before it's
trustworthy for the UNDERSTAND step; don't just swap it in and assume parity.
"""
import json
import requests
from .base import LLMProvider, LLMToolResult


class OllamaLLMProvider(LLMProvider):
    def __init__(self, model: str = "llama3.1", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def extract(self, system: str, user_content: str, tool_name: str,
                tool_description: str, input_schema: dict,
                max_tokens: int = 1000) -> LLMToolResult:
        prompt = (
            f"{system}\n\nRespond with ONLY a single JSON object matching this "
            f"schema, no prose, no markdown fences:\n{json.dumps(input_schema)}\n\n"
            f"Content:\n{user_content}"
        )
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Ollama did not return valid JSON for tool '{tool_name}'. "
                f"Raw output: {raw[:300]}"
            ) from e
        return LLMToolResult(tool_name=tool_name, input=parsed, raw_text="")
