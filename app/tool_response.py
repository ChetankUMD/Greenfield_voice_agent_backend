"""
Vapi tool-call request/response helpers.

Vapi sends tool calls as:
  { "message": { "toolCallList": [{ "id", "function": { "name", "arguments" } }] } }
  or with a flat { "id", "name", "arguments" } shape inside toolCallList.

Every tool endpoint must reply:
  { "results": [{ "toolCallId": "<same id>", "result": "<string>" }] }
"""
import json
from typing import Any, Optional


def tool_response(tool_call_id: str, result: str) -> dict:
    return {"results": [{"toolCallId": tool_call_id, "result": result}]}


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def extract_tool_call(body: Any) -> Optional[dict]:
    """
    Return a normalised { id, name, arguments } dict from any Vapi payload shape,
    or None if the payload cannot be parsed.
    """
    if not body or not isinstance(body, dict):
        return None

    msg = body.get("message") or {}

    # Standard shape: message.toolCallList or message.toolCalls
    raw_list = msg.get("toolCallList") or msg.get("toolCalls")
    if raw_list:
        raw = raw_list[0]
        if "function" in raw:
            # OpenAI-style: { id, function: { name, arguments } }
            fn = raw["function"]
            return {
                "id": raw.get("id", "tool-call"),
                "name": fn.get("name", ""),
                "arguments": _parse_args(fn.get("arguments", {})),
            }
        # Flat style: { id, name, arguments }
        return {
            "id": raw.get("id", "tool-call"),
            "name": raw.get("name", ""),
            "arguments": _parse_args(raw.get("arguments", {})),
        }

    # Vapi tool tester sends raw args with no message envelope
    if not body.get("message") and not isinstance(body, list):
        return {"id": "tool-test", "name": "", "arguments": body}

    return None
