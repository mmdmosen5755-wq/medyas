"""Thin AI client. Two providers behind one interface, tool-use included:
  anthropic -> POST {base}/v1/messages           (default; Claude)
  openai    -> POST {base}/chat/completions      (OpenAI, OpenRouter, Gemini-compat, local servers ...)

Internal message format = Anthropic's blocks, so the agent loop is written once:
  assistant: [{"type":"text"}, {"type":"tool_use","id","name","input"}]
  user:      [{"type":"tool_result","tool_use_id","content"}]
"""
import json
import re
from dataclasses import dataclass, field

import httpx

from . import config
from .db import get_settings

DEFAULT_MODELS = {"anthropic": {"chat": "claude-sonnet-5-5", "fast": "claude-haiku-5-5"},
                  "openai": {"chat": "gpt-4o", "fast": "gpt-4o-mini"}}
DEFAULT_BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}


class AIError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class AIReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0
    blocks: list[dict] = field(default_factory=list)  # assistant content in canonical form


async def conf() -> dict:
    cfg = await get_settings()
    prov = cfg["ai_provider"] if cfg["ai_provider"] in DEFAULT_MODELS else "anthropic"
    return {
        "provider": prov,
        "key": cfg["ai_api_key"].strip(),
        "base": (cfg["ai_base_url"].strip() or DEFAULT_BASE[prov]).rstrip("/"),
        "chat": cfg["ai_model_chat"].strip() or DEFAULT_MODELS[prov]["chat"],
        "fast": cfg["ai_model_fast"].strip() or DEFAULT_MODELS[prov]["fast"],
    }


async def available() -> bool:
    return bool((await conf())["key"])


def _clean(msg: str, key: str) -> str:
    return msg.replace(key, "***") if key else msg


async def _post(url: str, headers: dict, body: dict, key: str) -> dict:
    kw = {"proxy": config.AI_PROXY} if config.AI_PROXY else {}
    last = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), **kw) as c:
                r = await c.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}"
        else:
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {_clean(r.text[:300], key)}"
            if r.status_code not in (429, 500, 502, 503, 529):
                break
        import asyncio
        await asyncio.sleep(2 * (attempt + 1))
    raise AIError(last or "unknown error")


# ------------------------------------------------------------------ anthropic
async def _anthropic(c: dict, model: str, system: str, messages: list, tools, max_tokens: int) -> AIReply:
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
    j = await _post(f"{c['base']}/v1/messages",
                    {"x-api-key": c["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    body, c["key"])
    out = AIReply(blocks=[b for b in j.get("content", []) if b.get("type") in ("text", "tool_use")])
    for b in out.blocks:
        if b["type"] == "text":
            out.text += b["text"]
        else:
            out.tool_calls.append(ToolCall(b["id"], b["name"], b.get("input") or {}))
    u = j.get("usage") or {}
    out.tokens = int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
    return out


# ------------------------------------------------------------------ openai-compatible
def _to_openai(system: str, messages: list) -> list:
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            txt = "".join(b["text"] for b in c if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in c if b["type"] == "tool_use"]
            msg = {"role": "assistant", "content": txt or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in c:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


async def _openai(c: dict, model: str, system: str, messages: list, tools, max_tokens: int) -> AIReply:
    body = {"model": model, "max_tokens": max_tokens, "messages": _to_openai(system, messages)}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["schema"]}} for t in tools]
    j = await _post(f"{c['base']}/chat/completions",
                    {"Authorization": f"Bearer {c['key']}", "content-type": "application/json"}, body, c["key"])
    try:
        msg = j["choices"][0]["message"]
    except (KeyError, IndexError):
        raise AIError("پاسخ نامعتبر از سرویس هوش مصنوعی") from None
    out = AIReply(text=msg.get("content") or "")
    if out.text:
        out.blocks.append({"type": "text", "text": out.text})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        call = ToolCall(tc["id"], tc["function"]["name"], args if isinstance(args, dict) else {})
        out.tool_calls.append(call)
        out.blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
    out.tokens = int((j.get("usage") or {}).get("total_tokens", 0))
    return out


# ------------------------------------------------------------------ public API
async def complete(system: str, messages: list, *, kind: str = "chat", tools: list | None = None,
                   max_tokens: int = 1500) -> AIReply:
    """kind: 'chat' = smart model (conversation, rewriting) · 'fast' = cheap model (classification, policy gate)."""
    c = await conf()
    if not c["key"]:
        raise AIError("کلید هوش مصنوعی در پنل مدیریت تنظیم نشده است")
    model = c["fast"] if kind == "fast" else c["chat"]
    fn = _anthropic if c["provider"] == "anthropic" else _openai
    return await fn(c, model, system, messages, tools, max_tokens)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model answer (tolerates ``` fences and chatter).
    Whichever opener comes first wins, so a one-element array is not mistaken for its inner object."""
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    pairs = sorted(((t.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if t.find(o) != -1))
    for i, _, closer in pairs:
        j = t.rfind(closer)
        if j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    raise AIError("پاسخ مدل قابل خواندن نبود")
