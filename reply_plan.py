"""Validated ordered chat bubbles; no punctuation splitting or UI commands."""


def response_schema(sticker_ids, max_parts=4, allow_group_actions=False):
    schema = {"type": "object", "additionalProperties": False,
            "properties": {
                "messages": {"type": "array", "minItems": 0, "maxItems": max_parts,
                             "items": {"type": "object", "additionalProperties": False,
                                       "properties": {"kind": {"type": "string", "enum": ["text", "sticker"]},
                                                      "text": {"type": "string"},
                                                      "sticker_id": {"type": "string", "enum": [""] + sorted(sticker_ids)}},
                                       "required": ["kind", "text", "sticker_id"]}},
                "sticker_description": {"type": "string"},
                "sticker_tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["messages", "sticker_description", "sticker_tags"]}
    if allow_group_actions:
        schema["properties"]["actions"] = {
            "type": "array", "minItems": 0, "maxItems": 2,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"kind": {"type": "string", "enum": ["rename_self", "rename_group"]},
                                     "value": {"type": "string"}},
                      "required": ["kind", "value"]}}
        schema["required"].append("actions")
    return schema


def parse_messages(data, sticker_ids, max_parts=4):
    if not isinstance(data, dict):
        raise ValueError("回复格式无效")
    parts = data.get("messages")
    if "messages" not in data:  # Compatibility with saved draft/probe outputs.
        parts = [{"kind": "sticker" if data.get("sticker_id") else "text",
                  "text": data.get("reply"), "sticker_id": data.get("sticker_id", "")}]
    if not isinstance(parts, list) or not 0 <= len(parts) <= max_parts:
        raise ValueError("回复条数无效")
    result = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") not in ("text", "sticker"):
            raise ValueError("回复消息类型无效")
        text = part.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 300:
            raise ValueError("每条消息需要非空正文或表情备用文字，且不超过300字")
        key = part.get("sticker_id", "")
        valid = part["kind"] == "sticker" and isinstance(key, str) and key in sticker_ids
        result.append({"kind": "sticker" if valid else "text", "text": text.strip(),
                       "sticker_id": key if valid else ""})
    if sum(len(p["text"]) for p in result) > 1200:
        raise ValueError("单次回复过长")
    return result


def describe_messages(messages):
    return "\n".join("[表情]" + p.get("description", "") if p["kind"] == "sticker"
                     else p["text"] for p in messages)
