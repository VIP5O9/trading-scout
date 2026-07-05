from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from src.llm.runtime import LLMClient


class SchemaValidationError(Exception):
    """Raised after the retry also fails. Message includes the final validation error."""


def extract_json_block(text: str) -> str:
    """Return the content of the LAST ```json fenced block if present;
    else the last ``` fenced block; else the substring from the first "{" to its balanced
    closing "}" (track nesting through strings); raise ValueError if none found."""
    # Try to find last ```json block
    start_marker = "```json"
    end_marker = "```"
    last_start = text.rfind(start_marker)
    if last_start != -1:
        content_start = last_start + len(start_marker)
        end = text.find(end_marker, content_start)
        if end != -1:
            return text[content_start:end].strip()
    # Try last ``` block
    last_triple = text.rfind("```")
    if last_triple != -1:
        # Check if there is a closing ``` after this
        end = text.find("```", last_triple + 3)
        if end != -1:
            # Determine if it's a ```json or just ```
            # The content is between the first newline after the opening ``` and the closing ```
            # But we need to handle the case where the opening ``` might have a language specifier
            # We'll take everything after the opening ``` line
            opening_line_end = text.find("\n", last_triple)
            if opening_line_end != -1:
                content_start = opening_line_end + 1
            else:
                content_start = last_triple + 3
            return text[content_start:end].strip()
    # Fallback: find balanced braces
    brace_start = text.find("{")
    if brace_start == -1:
        raise ValueError("No JSON block found")
    depth = 0
    in_string = False
    escape = False
    for i in range(brace_start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[brace_start:i+1]
    raise ValueError("No JSON block found")


async def complete_json(
    llm: LLMClient,
    system: str,
    messages: list[dict],
    model_cls: type[BaseModel],
    max_tokens: int = 3000,
) -> BaseModel:
    """Call llm.complete with a system prompt requesting a JSON block matching the schema.
    Retries once on failure. Returns validated model instance."""
    schema_str = json.dumps(model_cls.model_json_schema(), indent=2)
    system_prompt = (
        system
        + "\n\nReply with a SINGLE fenced ```json block and nothing else, matching this JSON schema:\n```json\n"
        + schema_str
        + "\n```"
    )

    for attempt in range(2):
        try:
            response = await llm.complete(system_prompt, messages, max_tokens=max_tokens)
            block = extract_json_block(response)
            parsed = json.loads(block)
            validated = model_cls.model_validate(parsed)
            return validated
        except (ValueError, json.JSONDecodeError, ValidationError) as e:
            if attempt == 1:
                raise SchemaValidationError(str(e))
            # Prepare retry message
            error_msg = str(e)
            messages = messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": f"Your previous reply failed validation: {error_msg}. Reply again with ONLY the corrected fenced json block matching the schema."},
            ]
    # Should never reach here
    raise SchemaValidationError("Unexpected error")
