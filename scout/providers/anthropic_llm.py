"""Anthropic, wrapped to the LLMProvider interface via forced tool-calls."""
import anthropic
from .base import LLMProvider, LLMToolResult


class AnthropicLLMProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def extract(self, system: str, user_content: str, tool_name: str,
                tool_description: str, input_schema: dict,
                max_tokens: int = 1000) -> LLMToolResult:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[{"name": tool_name, "description": tool_description, "input_schema": input_schema}],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_content}],
        )
        tool_use = next(b for b in resp.content if b.type == "tool_use")
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        return LLMToolResult(tool_name=tool_use.name, input=tool_use.input,
                              raw_text=" ".join(text_blocks))
