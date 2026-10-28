"""Local index of native WeChat favorites learned only from the allowed group."""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image


def normalized(image):
    return np.asarray(image.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS), dtype=np.float32) / 255


def visual_distance(left, right):
    """Conservative matching: uncertain animation frames must not send another image."""
    a, b = normalized(left), normalized(right)
    return float(np.sqrt(np.mean((a - b) ** 2)))


def choose_preview(reference, images, threshold=0.025, margin=0.018):
    references = reference if isinstance(reference, (list, tuple)) else [reference]
    if not references:
        return None
    # Multiple verified frames belong to one asset. Ambiguity is still checked
    # between different candidate widgets, never between frames of one GIF.
    ranked = sorted((min(visual_distance(ref, im) for ref in references), i) for i, im in enumerate(images))
    if not ranked or ranked[0][0] > threshold:
        return None
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < margin:
        return None
    return ranked[0][1]


def source_match(source, preview):
    """Locate a picker thumbnail inside its source row, excluding chat chrome."""
    import cv2
    src = np.asarray(source.convert("RGB"))
    thumb = np.asarray(preview.convert("RGB"))
    if float(np.std(thumb)) < 8:
        return 0.0
    # Source bubbles have a bounded height. Only try plausible rendered sizes;
    # no filesystem scanning or matching against messages from other chats.
    best, best_height = 0.0, None
    ratio = preview.width / preview.height
    maximum = min(source.height, 420)

    def score_at(height):
        width = round(height * ratio)
        if width >= source.width:
            return 0.0
        resized = cv2.resize(thumb, (width, height), interpolation=cv2.INTER_AREA)
        return float(cv2.minMaxLoc(cv2.matchTemplate(src, resized, cv2.TM_CCOEFF_NORMED))[1])

    for height in range(64, maximum + 1, 3):
        score = score_at(height)
        if score > best:
            best, best_height = score, height
    # DPI scaling often produces a 198px bubble, between the coarse 196/199px
    # samples. Refine the strongest scale instead of weakening identity checks.
    if best_height is not None:
        for height in range(max(64, best_height - 2), min(maximum, best_height + 2) + 1):
            best = max(best, score_at(height))
    return best


def choose_source_preview(source, images):
    ranked = sorted(((source_match(source, im), i) for i, im in enumerate(images)), reverse=True)
    if not ranked or ranked[0][0] < 0.965:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.025:
        return None
    return ranked[0][1]


class StickerLibrary:
    def __init__(self, root, group):
        self.root, self.group = Path(root), group
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.unverified_path = self.root / 'unverified_ids.json'
        self.unverified_ids = set(json.loads(self.unverified_path.read_text(encoding='utf-8'))
                                  if self.unverified_path.exists() else [])
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS stickers (
            id TEXT PRIMARY KEY, group_name TEXT NOT NULL, preview_file TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
            sender TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            last_used REAL, uses INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sticker_frames (
            sticker_id TEXT NOT NULL, digest TEXT NOT NULL, preview_file TEXT NOT NULL,
            PRIMARY KEY(sticker_id,digest))""")
        self.db.commit()

    def add(self, preview, sender):
        image = preview.convert("RGB")
        key = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()[:24]
        now = time.time()
        with self.lock, self.db:
            for row in self.db.execute("SELECT * FROM stickers WHERE group_name=?", (self.group,)):
                path = self.root / row["preview_file"]
                if path.is_file():
                    with Image.open(path) as prior:
                        if visual_distance(image, prior) < 0.012:
                            key = row["id"]
                            break
            path = self.root / (key + ".png")
            if not path.exists():
                image.save(path)
            self.db.execute("""INSERT INTO stickers(id,group_name,preview_file,sender,first_seen,last_seen)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                (key, self.group, path.name, sender, now, now))
        return key

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM stickers WHERE id=? AND group_name=?", (key, self.group)).fetchone()
        if row is None:
            return None
        result = dict(row)
        path = (self.root / result.pop("preview_file")).resolve()
        if path.parent != self.root.resolve() or not path.is_file():
            return None
        result["preview_path"] = str(path)
        with self.lock:
            variants = self.db.execute("SELECT preview_file FROM sticker_frames WHERE sticker_id=? ORDER BY digest LIMIT 12", (key,)).fetchall()
        result["preview_paths"] = [str(path)]
        for variant in variants:
            candidate = (self.root / variant[0]).resolve()
            if candidate.parent == self.root.resolve() and candidate.is_file():
                result["preview_paths"].append(str(candidate))
        result["tags"] = json.loads(result.pop("tags_json"))
        return result

    def add_frames(self, key, frames):
        """Only callers holding a verified native favorite may bind GIF frames."""
        if self.get(key) is None:
            raise ValueError("表情索引不存在或不属于本群")
        with self.lock, self.db:
            for frame in frames:
                count = self.db.execute("SELECT COUNT(*) FROM sticker_frames WHERE sticker_id=?", (key,)).fetchone()[0]
                if count >= 12:
                    break
                image = frame.convert("RGB")
                digest = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()[:24]
                path = self.root / (key + "-frame-" + digest + ".png")
                if not path.exists():
                    image.save(path)
                self.db.execute("INSERT OR IGNORE INTO sticker_frames(sticker_id,digest,preview_file) VALUES(?,?,?)",
                                (key, digest, path.name))
            if frames and key in self.unverified_ids:
                self.unverified_ids.remove(key)
                temp = self.unverified_path.with_suffix('.tmp')
                temp.write_text(json.dumps(sorted(self.unverified_ids)), encoding='utf-8')
                temp.replace(self.unverified_path)

    def annotate(self, key, description, tags):
        description = str(description).strip()[:120]
        clean_tags = [str(t).strip()[:20] for t in tags[:6] if str(t).strip()]
        if not description:
            return
        with self.lock, self.db:
            self.db.execute("UPDATE stickers SET description=?,tags_json=? WHERE id=? AND group_name=?",
                            (description, json.dumps(clean_tags, ensure_ascii=False), key, self.group))

    def recognize_source(self, image):
        with self.lock:
            rows = self.db.execute("SELECT id,preview_file FROM stickers WHERE group_name=? ORDER BY last_seen DESC LIMIT 24",
                                   (self.group,)).fetchall()
        ids, previews = [], []
        for row in rows:
            path = self.root / row["preview_file"]
            if path.is_file():
                with Image.open(path) as preview:
                    previews.append(preview.convert("RGB"))
                ids.append(row["id"])
        chosen = choose_source_preview(image, previews)
        if chosen is None:
            return None
        key = ids[chosen]
        with self.lock, self.db:
            self.db.execute("UPDATE stickers SET last_seen=? WHERE id=?", (time.time(), key))
        return key

    def candidates(self, settings, recent=None, now=None):
        if not settings.get("sticker_replies", True):
            return []
        now = time.time() if now is None else now
        recent = recent or []
        between = settings.get("sticker_min_text_replies", 4)
        own = [row for row in recent if row.get("role") == "assistant"]
        if between and any(row.get("kind") == "sticker" for row in own[-between:]):
            return []
        with self.lock:
            last = self.db.execute("SELECT MAX(last_used) FROM stickers WHERE group_name=?", (self.group,)).fetchone()[0]
            if last is not None and now - last < settings.get("sticker_cooldown_seconds", 90):
                return []
            rows = self.db.execute("""SELECT id,description,tags_json,last_used FROM stickers
                WHERE group_name=? AND description<>'' ORDER BY last_seen DESC LIMIT 48""", (self.group,)).fetchall()
        return [{"id": row["id"], "description": row["description"], "tags": json.loads(row["tags_json"])}
                for row in rows if row['id'] not in self.unverified_ids and (row["last_used"] is None
                or now - row["last_used"] >= settings.get("sticker_same_cooldown_seconds", 900))][:24]

    def mark_used(self, key):
        with self.lock, self.db:
            self.db.execute("UPDATE stickers SET last_used=?,uses=uses+1 WHERE id=? AND group_name=?",
                            (time.time(), key, self.group))

    def stats(self):
        with self.lock:
            row = self.db.execute("SELECT COUNT(*),SUM(description<>''),SUM(uses) FROM stickers WHERE group_name=?", (self.group,)).fetchone()
        return {"saved": row[0], "described": row[1] or 0, "sent": row[2] or 0,
                "awaiting_revalidation": len(self.unverified_ids)}
