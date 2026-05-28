from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import textwrap
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, request
from openai import OpenAI


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file(".env.local")
load_env_file(".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

JST = ZoneInfo("Asia/Tokyo")
DB_PATH = os.getenv("DB_PATH", "data/kondate_ai.sqlite3")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", OPENAI_MODEL)

SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "8"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
TASK_SECRET = os.getenv("TASK_SECRET", "")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


DEFAULT_AVOID = [
    "固形のチーズ",
    "辛い食べ物",
    "ネバネバ系",
    "ピーマン",
    "椎茸",
]

DEFAULT_NOTES = [
    "大人2人分。30代前半の男女。",
    "夕食のみ。白米は1人あたり茶碗1杯。",
    "健康方針は、お腹が出にくい食事。高たんぱく、野菜多め、脂質と糖質は過度にしない。",
    "ダイエット食ではなく、おいしさと満足感を重視する。",
    "予算は2人分で理想1000円、許容1500円。基本はLIFEのスーパーで買う。",
    "買い物は2から3日分まとめて行う。",
    "使える調理器具はフライパン、鍋、炊飯器、電子レンジ、IH一口。",
    "常備調味料は醤油、味噌、酒、酢、ウスターソース、みりん、砂糖、はちみつ、オイスターソース、ナツメグ、塩こしょう、マヨネーズ、コンソメ、ほんだし、米。",
    "朝8時に夕食案を3つ出す。微妙と言われたら別案を3つ出す。",
    "採用した料理は、作ったと返信された後に在庫を減らす。",
]


@contextmanager
def db() -> Any:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                quantity TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(kind, content)
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                user_id TEXT,
                target_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS meal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                meal_date TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

    seed_defaults()


def seed_defaults() -> None:
    with db() as conn:
        for item in DEFAULT_AVOID:
            conn.execute(
                "INSERT OR IGNORE INTO preferences (kind, content, source) VALUES (?, ?, ?)",
                ("avoid", item, "initial"),
            )
        for note in DEFAULT_NOTES:
            conn.execute(
                "INSERT OR IGNORE INTO preferences (kind, content, source) VALUES (?, ?, ?)",
                ("note", note, "initial"),
            )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_inventory() -> list[dict[str, str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT name, quantity, note FROM inventory ORDER BY name"
        ).fetchall()
    return rows_to_dicts(rows)


def set_inventory(items: list[dict[str, str]]) -> None:
    with db() as conn:
        conn.execute("DELETE FROM inventory")
        for item in items:
            name = clean_text(item.get("name", ""))
            if not name:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO inventory (name, quantity, note, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (
                    name,
                    clean_text(item.get("quantity", "")),
                    clean_text(item.get("note", "")),
                ),
            )


def add_inventory_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    added: list[dict[str, str]] = []
    with db() as conn:
        for item in items:
            name = clean_text(item.get("name", ""))
            quantity = clean_text(item.get("quantity", ""))
            note = clean_text(item.get("note", ""))
            if not name:
                continue

            existing = conn.execute(
                "SELECT quantity, note FROM inventory WHERE name = ?",
                (name,),
            ).fetchone()
            if existing:
                merged_quantity = merge_quantity(existing["quantity"], quantity)
                merged_note = merge_note(existing["note"], note)
                conn.execute(
                    """
                    UPDATE inventory
                    SET quantity = ?, note = ?, updated_at = datetime('now')
                    WHERE name = ?
                    """,
                    (merged_quantity, merged_note, name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO inventory (name, quantity, note)
                    VALUES (?, ?, ?)
                    """,
                    (name, quantity, note),
                )
            added.append({"name": name, "quantity": quantity, "note": note})
    return added


def merge_quantity(current: str, incoming: str) -> str:
    current = clean_text(current)
    incoming = clean_text(incoming)
    if current and incoming:
        return f"{current} + {incoming}"
    return incoming or current


def merge_note(current: str, incoming: str) -> str:
    current = clean_text(current)
    incoming = clean_text(incoming)
    if current and incoming and incoming not in current:
        return f"{current}; {incoming}"
    return current or incoming


def get_preferences() -> dict[str, list[str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT kind, content FROM preferences ORDER BY id"
        ).fetchall()
    prefs: dict[str, list[str]] = {"avoid": [], "like": [], "dislike": [], "note": []}
    for row in rows:
        prefs.setdefault(row["kind"], []).append(row["content"])
    return prefs


def add_preference(kind: str, content: str, source: str = "user") -> None:
    content = clean_text(content)
    if not content:
        return
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO preferences (kind, content, source) VALUES (?, ?, ?)",
            (kind, content, source),
        )


def register_conversation(source: dict[str, Any]) -> str:
    source_type = source.get("type", "user")
    user_id = source.get("userId")
    if source_type == "group":
        target_id = source.get("groupId")
    elif source_type == "room":
        target_id = source.get("roomId")
    else:
        target_id = user_id

    if not target_id:
        raise ValueError("LINE source has no target id")

    with db() as conn:
        conn.execute(
            """
            INSERT INTO conversations (conversation_id, source_type, user_id, target_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                source_type = excluded.source_type,
                user_id = COALESCE(excluded.user_id, conversations.user_id),
                target_id = excluded.target_id,
                enabled = 1,
                updated_at = datetime('now')
            """,
            (target_id, source_type, user_id, target_id),
        )
    return target_id


def get_push_targets() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT target_id FROM conversations
            WHERE enabled = 1
            ORDER BY source_type = 'group' DESC, updated_at DESC
            """
        ).fetchall()
    return [row["target_id"] for row in rows]


def save_message(conversation_id: str, role: str, content: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (conversation_id, role, content[:4000]),
        )
        conn.execute(
            """
            DELETE FROM messages
            WHERE conversation_id = ?
              AND id NOT IN (
                SELECT id FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 30
              )
            """,
            (conversation_id, conversation_id),
        )


def get_recent_messages(conversation_id: str, limit: int = 12) -> list[dict[str, str]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    return [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(rows)
    ]


def create_pending_action(
    conversation_id: str,
    action_type: str,
    payload: dict[str, Any],
) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE pending_actions
            SET status = 'superseded', updated_at = datetime('now')
            WHERE conversation_id = ? AND action_type = ? AND status = 'pending'
            """,
            (conversation_id, action_type),
        )
        conn.execute(
            """
            INSERT INTO pending_actions (conversation_id, action_type, payload)
            VALUES (?, ?, ?)
            """,
            (conversation_id, action_type, json.dumps(payload, ensure_ascii=False)),
        )


def get_pending_action(
    conversation_id: str,
    action_type: str | None = None,
) -> dict[str, Any] | None:
    params: list[Any] = [conversation_id]
    type_clause = ""
    if action_type:
        type_clause = "AND action_type = ?"
        params.append(action_type)
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT id, action_type, payload FROM pending_actions
            WHERE conversation_id = ? {type_clause} AND status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "action_type": row["action_type"],
        "payload": json.loads(row["payload"]),
    }


def finish_pending_action(action_id: int, status: str = "done") -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE pending_actions
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (status, action_id),
        )


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def require_openai() -> OpenAI:
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return openai_client


def context_summary(conversation_id: str | None = None) -> str:
    inventory = get_inventory()
    prefs = get_preferences()

    inv_text = "\n".join(
        f"- {item['name']} {item['quantity']}".strip()
        for item in inventory
    ) or "- なし"

    pref_text = "\n".join(
        [
            f"避ける: {', '.join(prefs.get('avoid', [])) or 'なし'}",
            f"好き: {', '.join(prefs.get('like', [])) or '未登録'}",
            f"苦手: {', '.join(prefs.get('dislike', [])) or '未登録'}",
            "メモ:",
            *[f"- {note}" for note in prefs.get("note", [])],
        ]
    )

    history_text = ""
    if conversation_id:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT meal_date, title FROM meal_history
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 7
                """,
                (conversation_id,),
            ).fetchall()
        if rows:
            history_text = "\n最近作った献立:\n" + "\n".join(
                f"- {row['meal_date']}: {row['title']}" for row in rows
            )

    return f"""現在の在庫:
{inv_text}

好み・制約:
{pref_text}
{history_text}"""


def openai_json(messages: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
    client = require_openai()
    response = client.chat.completions.create(
        model=model or OPENAI_MODEL,
        temperature=0.7,
        response_format={"type": "json_object"},
        messages=messages,
    )
    content = response.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("OpenAI returned non-JSON content: %s", content[:500])
        return {}


def openai_text(messages: list[dict[str, str]]) -> str:
    client = require_openai()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.7,
        max_tokens=900,
        messages=messages,
    )
    return clean_text(response.choices[0].message.content)


def extract_inventory_from_text(text: str) -> list[dict[str, str]]:
    messages = [
        {
            "role": "system",
            "content": (
                "ユーザー文から買った食材や現在ある食材を抽出します。"
                "食品・調味料だけを対象にし、数量があれば quantity に入れてください。"
                "必ず JSON だけで返してください。形式: "
                '{"items":[{"name":"鶏もも肉","quantity":"300g","note":""}]}'
            ),
        },
        {"role": "user", "content": text},
    ]
    data = openai_json(messages)
    return normalize_items(data.get("items", []))


def extract_receipt_items(image_bytes: bytes) -> list[dict[str, str]]:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "レシート画像から献立に使える食材・調味料だけを抽出します。"
                "日用品、袋、割引、合計金額は除外してください。"
                "数量や個数が読める場合は quantity に入れてください。"
                "必ず JSON だけで返してください。形式: "
                '{"items":[{"name":"卵","quantity":"10個","note":"レシート読み取り"}]}'
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "このレシートから食材を抽出してください。"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                },
            ],
        },
    ]
    data = openai_json(messages, model=OPENAI_VISION_MODEL)
    return normalize_items(data.get("items", []))


def normalize_items(raw_items: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not isinstance(raw_items, list):
        return items
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        if not name:
            continue
        items.append(
            {
                "name": name,
                "quantity": clean_text(item.get("quantity")),
                "note": clean_text(item.get("note")),
            }
        )
    return items


def generate_meal_options(
    conversation_id: str,
    reason: str = "",
    alternatives: bool = False,
) -> dict[str, Any]:
    intent = "別案を3つ提案してください。" if alternatives else "夕食案を3つ提案してください。"
    system = f"""
あなたは、プロの栄養士であり、日本トップクラスの料理人として家庭の夕食を提案する献立AIです。
2人分の夕食を、健康寄りだがおいしく満足感がある内容で提案します。
太りにくさ・お腹が出にくい食事を意識し、極端なダイエット食にはしません。

制約:
- 固形のチーズ、辛い食べ物、ネバネバ系、ピーマン、椎茸は使わない。
- 白米は1人茶碗1杯を前提にする。
- 調理器具はフライパン、鍋、炊飯器、電子レンジ、IH一口。
- 予算は2人分で理想1000円、許容1500円。LIFEのスーパーで買いやすい材料を優先。
- 今ある在庫を優先し、不足があれば買い足しを明記。

必ず JSON だけで返してください。
形式:
{{
  "options": [
    {{
      "label": "A",
      "title": "献立名",
      "menu": ["主菜", "副菜", "汁物など"],
      "why": "おすすめ理由",
      "belly_friendly_point": "お腹が出にくい工夫",
      "estimated_cost_yen": 1000,
      "uses_inventory": ["使う在庫"],
      "ingredients_to_buy": ["買い足す材料と分量"],
      "used_ingredients": [{{"name":"食材名","quantity":"使う量"}}],
      "recipe_steps": ["作り方1", "作り方2", "作り方3"]
    }}
  ]
}}
"""
    user = f"""{intent}

{context_summary(conversation_id)}

ユーザーの補足・却下理由:
{reason or "なし"}"""

    data = openai_json(
        [
            {"role": "system", "content": textwrap.dedent(system).strip()},
            {"role": "user", "content": user},
        ]
    )
    options = data.get("options", [])
    if not isinstance(options, list) or not options:
        raise RuntimeError("献立案を生成できませんでした")

    normalized = []
    labels = ["A", "B", "C"]
    for i, option in enumerate(options[:3]):
        if not isinstance(option, dict):
            continue
        option["label"] = labels[i]
        normalized.append(option)

    payload = {"options": normalized, "created_at": now_text()}
    create_pending_action(conversation_id, "meal_options", payload)
    return payload


def format_meal_options(payload: dict[str, Any]) -> str:
    lines = ["今日の夕食案を3つ出します。微妙なら「微妙」と送ってください。", ""]
    for option in payload.get("options", []):
        label = clean_text(option.get("label"))
        title = clean_text(option.get("title"))
        menu = as_list_text(option.get("menu"))
        buy = as_list_text(option.get("ingredients_to_buy")) or "買い足しなし"
        steps = option.get("recipe_steps") or []
        if not isinstance(steps, list):
            steps = [clean_text(steps)]

        lines.extend(
            [
                f"{label}. {title}",
                f"献立: {menu}",
                f"理由: {clean_text(option.get('why'))}",
                f"お腹対策: {clean_text(option.get('belly_friendly_point'))}",
                f"目安費用: {clean_text(option.get('estimated_cost_yen'))}円",
                f"買い足し: {buy}",
                "作り方:",
            ]
        )
        lines.extend(f"{idx}. {clean_text(step)}" for idx, step in enumerate(steps, 1) if clean_text(step))
        lines.append("")
    lines.append("作るものが決まったら A / B / C で返してください。")
    return "\n".join(lines).strip()


def as_list_text(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(clean_text(v) for v in value if clean_text(v))
    return clean_text(value)


def select_meal_option(conversation_id: str, text: str) -> str | None:
    selected = normalize_selection(text)
    if selected is None:
        return None

    pending = get_pending_action(conversation_id, "meal_options")
    if not pending:
        return "いま選べる献立案がありません。「献立」または「別案」と送ってください。"

    options = pending["payload"].get("options", [])
    if selected >= len(options):
        return "A / B / C のどれかで選んでください。"

    option = options[selected]
    finish_pending_action(pending["id"], "selected")
    create_pending_action(conversation_id, "selected_meal", option)
    add_preference("like", clean_text(option.get("title")), "selected")

    return (
        f"{option.get('label')}. {option.get('title')} でいきましょう。\n\n"
        f"{format_single_recipe(option)}\n\n"
        "実際に作ったら「作った」と送ってください。在庫をその時点で更新します。"
    )


def normalize_selection(text: str) -> int | None:
    normalized = clean_text(text).upper()
    mapping = {
        "A": 0,
        "Ａ": 0,
        "1": 0,
        "１": 0,
        "B": 1,
        "Ｂ": 1,
        "2": 1,
        "２": 1,
        "C": 2,
        "Ｃ": 2,
        "3": 2,
        "３": 2,
    }
    return mapping.get(normalized)


def format_single_recipe(option: dict[str, Any]) -> str:
    steps = option.get("recipe_steps") or []
    if not isinstance(steps, list):
        steps = [clean_text(steps)]
    lines = [
        f"献立: {as_list_text(option.get('menu'))}",
        f"買い足し: {as_list_text(option.get('ingredients_to_buy')) or '買い足しなし'}",
        "作り方:",
    ]
    lines.extend(f"{idx}. {clean_text(step)}" for idx, step in enumerate(steps, 1) if clean_text(step))
    return "\n".join(lines)


def mark_selected_meal_cooked(conversation_id: str) -> str:
    pending = get_pending_action(conversation_id, "selected_meal")
    if not pending:
        return "作った献立がまだ選ばれていません。先に A / B / C で選んでください。"

    option = pending["payload"]
    today = datetime.now(JST).strftime("%Y-%m-%d")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO meal_history (conversation_id, meal_date, title, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                conversation_id,
                today,
                clean_text(option.get("title")),
                json.dumps(option, ensure_ascii=False),
            ),
        )

    updated_inventory, note = reconcile_inventory_after_cooking(
        get_inventory(),
        option.get("used_ingredients", []),
    )
    if updated_inventory is not None:
        set_inventory(updated_inventory)

    finish_pending_action(pending["id"], "done")
    inventory_text = format_inventory(get_inventory())
    note_text = f"\n\n在庫メモ: {note}" if note else ""
    return f"作った記録をつけました。おつかれさまです。\n\n現在の在庫:\n{inventory_text}{note_text}"


def reconcile_inventory_after_cooking(
    inventory: list[dict[str, str]],
    used_ingredients: Any,
) -> tuple[list[dict[str, str]] | None, str]:
    used = normalize_items(used_ingredients)
    if not used:
        return inventory, "使った食材の詳細がないため、在庫はそのままにしました。"

    try:
        data = openai_json(
            [
                {
                    "role": "system",
                    "content": (
                        "料理後の在庫を更新します。現在の在庫と使った食材を見て、"
                        "残った在庫だけを JSON で返してください。"
                        "数量計算が曖昧な場合は、無理に削除せず quantity に「残り目安」を書いてください。"
                        '形式: {"items":[{"name":"卵","quantity":"残り4個","note":""}], "note":"補足"}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_inventory": inventory,
                            "used_ingredients": used,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        return normalize_items(data.get("items", inventory)), clean_text(data.get("note"))
    except Exception as exc:
        logger.exception("Failed to reconcile inventory: %s", exc)
        return None, "在庫更新でエラーが出たため、在庫は変更していません。"


def should_treat_as_inventory(text: str) -> bool:
    normalized = clean_text(text)
    keywords = ["買った", "追加", "在庫追加", "今ある材料", "材料:", "材料：", "食材"]
    has_keyword = any(keyword in normalized for keyword in keywords)
    has_quantity = any(char.isdigit() for char in normalized) or any(
        unit in normalized for unit in ["個", "本", "枚", "g", "kg", "ml", "パック", "袋", "玉"]
    )
    has_separator = "、" in normalized or "," in normalized or "\n" in normalized
    return has_keyword or (has_quantity and has_separator)


def learn_from_text(text: str) -> None:
    normalized = clean_text(text)
    dislike_words = ["苦手", "嫌い", "いや", "嫌", "無理", "避けたい"]
    like_words = ["好き", "おいしい", "美味しい", "よかった", "また食べたい"]

    if any(word in normalized for word in dislike_words):
        add_preference("dislike", normalized, "conversation")
    elif any(word in normalized for word in like_words):
        add_preference("like", normalized, "conversation")


def chat_reply(conversation_id: str, user_text: str) -> str:
    learn_from_text(user_text)
    messages = [
        {
            "role": "system",
            "content": (
                "あなたはLINEで会話する献立AIです。"
                "ユーザーの好みを尊重し、断られたら理由を学習し、次の提案に反映します。"
                "返答は親しみやすく簡潔に。必要なら献立・在庫・別案の操作を案内します。\n\n"
                + context_summary(conversation_id)
            ),
        },
        *get_recent_messages(conversation_id),
        {"role": "user", "content": user_text},
    ]
    return openai_text(messages)


def handle_text_message(event: dict[str, Any], conversation_id: str) -> str:
    text = clean_text(event.get("message", {}).get("text"))
    save_message(conversation_id, "user", text)

    receipt_pending = get_pending_action(conversation_id, "receipt_items")
    if receipt_pending and is_yes(text):
        added = add_inventory_items(receipt_pending["payload"].get("items", []))
        finish_pending_action(receipt_pending["id"])
        reply = "レシートの食材を在庫に追加しました。\n\n" + format_added_items(added)
        save_message(conversation_id, "assistant", reply)
        return reply
    if receipt_pending and is_no(text):
        finish_pending_action(receipt_pending["id"], "cancelled")
        reply = "レシートの登録をキャンセルしました。"
        save_message(conversation_id, "assistant", reply)
        return reply

    selected_reply = select_meal_option(conversation_id, text)
    if selected_reply:
        save_message(conversation_id, "assistant", selected_reply)
        return selected_reply

    if text in {"在庫", "ざいこ", "材料", "食材"}:
        reply = "現在の在庫:\n" + format_inventory(get_inventory())
    elif text in {"ヘルプ", "help", "使い方"}:
        reply = help_text()
    elif "作った" in text or "つくった" in text:
        reply = mark_selected_meal_cooked(conversation_id)
    elif any(word in text for word in ["微妙", "別案", "他の案", "ほかの案", "違う"]):
        payload = generate_meal_options(conversation_id, reason=text, alternatives=True)
        reply = format_meal_options(payload)
    elif text in {"献立", "今日の献立", "提案", "夕食"}:
        payload = generate_meal_options(conversation_id)
        reply = format_meal_options(payload)
    elif should_treat_as_inventory(text):
        items = extract_inventory_from_text(text)
        if items:
            added = add_inventory_items(items)
            reply = "在庫に追加しました。\n\n" + format_added_items(added)
        else:
            reply = "食材をうまく読み取れませんでした。例: 鶏もも肉 300g、玉ねぎ 2個"
    else:
        reply = chat_reply(conversation_id, text)

    save_message(conversation_id, "assistant", reply)
    return reply


def handle_image_message(event: dict[str, Any], conversation_id: str) -> str:
    message_id = event.get("message", {}).get("id")
    if not message_id:
        return "画像IDを取得できませんでした。もう一度送ってください。"

    image_bytes = fetch_line_message_content(message_id)
    items = extract_receipt_items(image_bytes)
    if not items:
        return "レシートから食材を読み取れませんでした。もう少し明るい写真で再送してください。"

    create_pending_action(conversation_id, "receipt_items", {"items": items})
    return (
        "レシートから次の食材を読み取りました。在庫に追加してよければ「はい」と送ってください。\n\n"
        + format_added_items(items)
    )


def is_yes(text: str) -> bool:
    return clean_text(text).lower() in {"はい", "yes", "y", "ok", "お願い", "登録", "追加して"}


def is_no(text: str) -> bool:
    return clean_text(text).lower() in {"いいえ", "no", "n", "キャンセル", "やめる"}


def format_inventory(items: list[dict[str, str]]) -> str:
    if not items:
        return "なし"
    return "\n".join(
        f"- {item['name']} {item['quantity']}".strip()
        for item in items
    )


def format_added_items(items: list[dict[str, str]]) -> str:
    if not items:
        return "追加なし"
    return "\n".join(
        f"- {item['name']} {item['quantity']}".strip()
        for item in items
    )


def help_text() -> str:
    return """使えるメッセージ:
- 「献立」: 今日の夕食を3案出します
- 「微妙」: 別案を3つ出します
- 「A」「B」「C」: 作る案を選びます
- 「作った」: 作った記録をつけ、在庫を更新します
- 「在庫」: 今ある材料を確認します
- 「鶏もも 300g、玉ねぎ 2個」: 食材を追加します
- レシート写真: 食材を読み取って、確認後に在庫追加します"""


def verify_line_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        logger.error("LINE_CHANNEL_SECRET is not configured")
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def line_api_request(path: str, payload: dict[str, Any]) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not configured")

    url = f"https://api.line.me{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.error("LINE API error %s: %s", exc.code, detail)
        raise


def fetch_line_message_content(message_id: str) -> bytes:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not configured")

    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def reply_text(reply_token: str, text: str) -> None:
    messages = [{"type": "text", "text": chunk} for chunk in chunk_text(text)[:5]]
    line_api_request(
        "/v2/bot/message/reply",
        {"replyToken": reply_token, "messages": messages},
    )


def push_text(target_id: str, text: str) -> None:
    messages = [{"type": "text", "text": chunk} for chunk in chunk_text(text)[:5]]
    line_api_request(
        "/v2/bot/message/push",
        {"to": target_id, "messages": messages},
    )


def chunk_text(text: str, max_len: int = 4500) -> list[str]:
    text = clean_text(text)
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current.strip())
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        chunks.append(current.strip())
    return chunks or [text[:max_len]]


def handle_line_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    source = event.get("source", {})
    conversation_id = register_conversation(source)
    reply_token = event.get("replyToken")

    if event_type in {"follow", "join"}:
        reply = (
            "献立AIを登録しました。\n"
            "朝8時に夕食案を3つ送ります。\n\n"
            + help_text()
        )
        if reply_token:
            reply_text(reply_token, reply)
        return

    if event_type != "message":
        return

    message_type = event.get("message", {}).get("type")
    try:
        if message_type == "text":
            reply = handle_text_message(event, conversation_id)
        elif message_type == "image":
            reply = handle_image_message(event, conversation_id)
        else:
            reply = "テキストかレシート画像で送ってください。"
    except Exception as exc:
        logger.exception("Failed to handle LINE event: %s", exc)
        reply = "すみません、処理中にエラーが出ました。少し時間を置いてもう一度送ってください。"

    if reply_token:
        reply_text(reply_token, reply)


def send_daily_suggestions() -> dict[str, Any]:
    targets = get_push_targets()
    if not targets:
        logger.info("No LINE push targets registered")
        return {"sent": 0, "targets": []}

    sent = 0
    errors: list[str] = []
    for target_id in targets:
        try:
            payload = generate_meal_options(target_id)
            push_text(target_id, "おはようございます。今日の夕食案です。\n\n" + format_meal_options(payload))
            sent += 1
        except Exception as exc:
            logger.exception("Failed to send daily suggestion to %s: %s", target_id, exc)
            errors.append(target_id)
    return {"sent": sent, "targets": targets, "errors": errors}


def now_text() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def scheduler_loop() -> None:
    last_run_date = ""
    while True:
        now = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")
        if (
            now.hour == SCHEDULE_HOUR
            and now.minute == SCHEDULE_MINUTE
            and last_run_date != today
        ):
            logger.info("Running daily suggestion job")
            try:
                send_daily_suggestions()
                last_run_date = today
            except Exception as exc:
                logger.exception("Daily suggestion job failed: %s", exc)
            time.sleep(70)
        time.sleep(20)


@app.route("/callback", methods=["POST"])
def callback() -> tuple[str, int]:
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not verify_line_signature(body, signature):
        abort(400)

    payload = request.get_json(force=True, silent=True) or {}
    for event in payload.get("events", []):
        handle_line_event(event)
    return "OK", 200


@app.route("/health", methods=["GET"])
def health() -> Any:
    return jsonify(
        {
            "status": "ok",
            "time": now_text(),
            "line_access_token_configured": bool(LINE_CHANNEL_ACCESS_TOKEN),
            "line_secret_configured": bool(LINE_CHANNEL_SECRET),
            "openai_configured": bool(OPENAI_API_KEY),
            "schedule": f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} Asia/Tokyo",
        }
    )


@app.route("/tasks/daily", methods=["GET", "POST"])
def trigger_daily() -> Any:
    if TASK_SECRET:
        expected = f"Bearer {TASK_SECRET}"
        if request.headers.get("Authorization") != expected:
            abort(401)
    return jsonify(send_daily_suggestions())


init_db()

if ENABLE_SCHEDULER:
    threading.Thread(target=scheduler_loop, daemon=True).start()
    logger.info("Scheduler started at %02d:%02d JST", SCHEDULE_HOUR, SCHEDULE_MINUTE)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
