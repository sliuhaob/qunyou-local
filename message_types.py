"""Conservative classification of the UIA message bodies we can interpret."""

import re

TEXT_CLASS = "mmui::ChatTextItemView"
CARD_CLASS = "mmui::ChatBubbleItemView"
REFER_CLASS = "mmui::ChatBubbleReferItemView"
MESSAGE_CLASSES = (TEXT_CLASS, CARD_CLASS, REFER_CLASS)
STICKER_LABELS = {"动画表情", "[动画表情]", "表情", "[表情]"}


def message_kind(class_name, body):
    body = str(body).strip()
    if not body:
        return None
    if class_name == TEXT_CLASS:
        return "text"
    if class_name == CARD_CLASS and body.startswith("[链接]"):
        return "link"
    # WeChat also exposes a sticker's accessible caption, e.g. 动画表情 [敲打].
    # Match only that complete label form, not arbitrary quoted card content.
    if class_name in (CARD_CLASS, REFER_CLASS) and (
            body in STICKER_LABELS or re.fullmatch(r"动画表情\s+\[[^\[\]\r\n]{1,80}\]", body)):
        return "sticker"
    return None  # Files, payments, voice, video, photos and system rows.


def context_entry(event, kind):
    entry = {"role": "account_self" if event["is_self"] else "group_member",
             "sender": event["sender"], "text": event["text"][:600]}
    if kind != "text":
        entry["kind"] = kind
    if event.get("member_key"):
        entry["member_key"] = event["member_key"]
    return entry
