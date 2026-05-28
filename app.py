from __future__ import annotations

import base64
import copy
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
import urllib.parse
import urllib.request
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

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_STATE_KEY = os.getenv("SUPABASE_STATE_KEY", "global")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)
STORAGE_BACKEND = "supabase" if USE_SUPABASE else "sqlite"

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
state_lock = threading.RLock()
storage_init_error = ""

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
    "同じ料理名は30日以内に出さず、似た主菜は14日以内に避ける。",
    "同じ主たんぱく質を2日連続にしない。調理法も焼く、煮る、蒸す、炒めるで偏らせない。",
]


def now_text() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def default_state() -> dict[str, Any]:
    return {
        "inventory": [],
        "preferences": [],
        "conversations": [],
        "messages": [],
        "pending_actions": [],
        "meal_history": [],
        "counters": {
            "message": 0,
            "pending_action": 0,
            "meal_history": 0,
        },
    }


def ensure_state_shape(state: dict[str, Any]) -> dict[str, Any]:
    baseline = default_state()
    for key, value in baseline.items():
        state.setdefault(key, copy.deepcopy(value))
    for key, value in baseline["counters"].items():
        state["counters"].setdefault(key, value)

    changed = False
    for item in DEFAULT_AVOID:
        changed = add_preference_to_state(state, "avoid", item, "initial") or changed
    for note in DEFAULT_NOTES:
        changed = add_preference_to_state(state, "note", note, "initial") or changed
    if changed:
        state["_changed_by_defaults"] = True
    return state


def add_preference_to_state(
    state: dict[str, Any],
    kind: str,
    content: str,
    source: str,
) -> bool:
    content = clean_text(content)
    if not content:
        return False
    for pref in state["preferences"]:
        if pref.get("kind") == kind and pref.get("content") == content:
            return False
    state["preferences"].append(
        {
            "kind": kind,
            "content": content,
            "source": source,
            "created_at": now_text(),
        }
    )
    return True


def supabase_request(
    method: str,
    path: str,
    payload: Any | None = None,
    prefer: str | None = None,
) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase API error {exc.code}: {detail}") from exc


def load_state() -> dict[str, Any]:
    with state_lock:
        if USE_SUPABASE:
            key = urllib.parse.quote(SUPABASE_STATE_KEY, safe="")
            rows = supabase_request(
                "GET",
                f"bot_state?key=eq.{key}&select=value",
            )
            if rows:
                state = rows[0].get("value") or default_state()
            else:
                state = default_state()
            ensure_state_shape(state)
            if state.pop("_changed_by_defaults", False) or not rows:
                save_state(state)
            return state

        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (SUPABASE_STATE_KEY,),
            ).fetchone()
            state = json.loads(row[0]) if row else default_state()
            ensure_state_shape(state)
            if state.pop("_changed_by_defaults", False) or not row:
                save_state(state)
            return state


def save_state(state: dict[str, Any]) -> None:
    with state_lock:
        if USE_SUPABASE:
            payload = [{"key": SUPABASE_STATE_KEY, "value": state}]
            supabase_request(
                "POST",
                "bot_state?on_conflict=key",
                payload,
                prefer="resolution=merge-duplicates,return=minimal",
            )
            return

        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now')
                """,
                (SUPABASE_STATE_KEY, json.dumps(state, ensure_ascii=False)),
            )


def init_storage() -> None:
    global storage_init_error
    try:
        state = load_state()
        save_state(state)
        storage_init_error = ""
        logger.info("Storage initialized using %s", STORAGE_BACKEND)
    except Exception as exc:
        storage_init_error = str(exc)
        logger.exception("Storage initialization failed: %s", exc)


def get_inventory() -> list[dict[str, str]]:
    state = load_state()
    return sorted(
        [
            {
                "name": clean_text(item.get("name")),
                "quantity": clean_text(item.get("quantity")),
                "note": clean_text(item.get("note")),
            }
            for item in state["inventory"]
            if clean_text(item.get("name"))
        ],
        key=lambda item: item["name"],
    )


def set_inventory(items: list[dict[str, str]]) -> None:
    state = load_state()
    normalized = []
    seen = set()
    for item in items:
        name = clean_text(item.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(
            {
                "name": name,
                "quantity": clean_text(item.get("quantity")),
                "note": clean_text(item.get("note")),
            }
        )
    state["inventory"] = normalized
    save_state(state)


def add_inventory_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    state = load_state()
    added: list[dict[str, str]] = []
    by_name = {
        clean_text(item.get("name")): item
        for item in state["inventory"]
        if clean_text(item.get("name"))
    }

    for item in items:
        name = clean_text(item.get("name"))
        quantity = clean_text(item.get("quantity"))
        note = clean_text(item.get("note"))
        if not name:
            continue
        if name in by_name:
            existing = by_name[name]
            existing["quantity"] = merge_quantity(clean_text(existing.get("quantity")), quantity)
            existing["note"] = merge_note(clean_text(existing.get("note")), note)
        else:
            by_name[name] = {"name": name, "quantity": quantity, "note": note}
            state["inventory"].append(by_name[name])
        added.append({"name": name, "quantity": quantity, "note": note})

    save_state(state)
    return added


def add_meal_history_entry(
    conversation_id: str,
    title: str,
    details: dict[str, Any],
) -> None:
    state = load_state()
    state["counters"]["meal_history"] += 1
    state["meal_history"].append(
        {
            "id": state["counters"]["meal_history"],
            "conversation_id": conversation_id,
            "meal_date": datetime.now(JST).strftime("%Y-%m-%d"),
            "title": clean_text(title) or "自分で作った料理",
            "details": details,
            "created_at": now_text(),
        }
    )
    save_state(state)


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
    state = load_state()
    prefs: dict[str, list[str]] = {"avoid": [], "like": [], "dislike": [], "note": []}
    for pref in state["preferences"]:
        kind = clean_text(pref.get("kind"))
        content = clean_text(pref.get("content"))
        if kind and content:
            prefs.setdefault(kind, []).append(content)
    return prefs


def add_preference(kind: str, content: str, source: str = "user") -> None:
    state = load_state()
    if add_preference_to_state(state, kind, content, source):
        save_state(state)


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

    state = load_state()
    found = None
    for conversation in state["conversations"]:
        if conversation.get("conversation_id") == target_id:
            found = conversation
            break

    if found:
        found.update(
            {
                "source_type": source_type,
                "user_id": user_id or found.get("user_id"),
                "target_id": target_id,
                "enabled": True,
                "updated_at": now_text(),
            }
        )
    else:
        state["conversations"].append(
            {
                "conversation_id": target_id,
                "source_type": source_type,
                "user_id": user_id,
                "target_id": target_id,
                "enabled": True,
                "created_at": now_text(),
                "updated_at": now_text(),
            }
        )
    save_state(state)
    return target_id


def get_push_targets() -> list[str]:
    state = load_state()
    conversations = [
        conv for conv in state["conversations"] if conv.get("enabled", True)
    ]
    conversations.sort(
        key=lambda conv: (
            0 if conv.get("source_type") == "group" else 1,
            clean_text(conv.get("updated_at")),
        )
    )
    return [clean_text(conv.get("target_id")) for conv in conversations if clean_text(conv.get("target_id"))]


def save_message(conversation_id: str, role: str, content: str) -> None:
    state = load_state()
    state["counters"]["message"] += 1
    state["messages"].append(
        {
            "id": state["counters"]["message"],
            "conversation_id": conversation_id,
            "role": role,
            "content": content[:4000],
            "created_at": now_text(),
        }
    )

    kept_messages = []
    for message in state["messages"]:
        same_conversation = message.get("conversation_id") == conversation_id
        if not same_conversation:
            kept_messages.append(message)
            continue
        recent_ids = [
            item["id"]
            for item in sorted(
                [
                    msg
                    for msg in state["messages"]
                    if msg.get("conversation_id") == conversation_id
                ],
                key=lambda msg: int(msg.get("id", 0)),
                reverse=True,
            )[:30]
        ]
        if message.get("id") in recent_ids:
            kept_messages.append(message)
    state["messages"] = kept_messages
    save_state(state)


def get_recent_messages(conversation_id: str, limit: int = 12) -> list[dict[str, str]]:
    state = load_state()
    messages = [
        msg for msg in state["messages"] if msg.get("conversation_id") == conversation_id
    ]
    messages.sort(key=lambda msg: int(msg.get("id", 0)))
    return [
        {"role": clean_text(msg.get("role")), "content": clean_text(msg.get("content"))}
        for msg in messages[-limit:]
    ]


def create_pending_action(
    conversation_id: str,
    action_type: str,
    payload: dict[str, Any],
) -> None:
    state = load_state()
    for action in state["pending_actions"]:
        if (
            action.get("conversation_id") == conversation_id
            and action.get("action_type") == action_type
            and action.get("status") == "pending"
        ):
            action["status"] = "superseded"
            action["updated_at"] = now_text()

    state["counters"]["pending_action"] += 1
    state["pending_actions"].append(
        {
            "id": state["counters"]["pending_action"],
            "conversation_id": conversation_id,
            "action_type": action_type,
            "payload": payload,
            "status": "pending",
            "created_at": now_text(),
            "updated_at": now_text(),
        }
    )
    save_state(state)


def get_pending_action(
    conversation_id: str,
    action_type: str | None = None,
) -> dict[str, Any] | None:
    state = load_state()
    actions = [
        action
        for action in state["pending_actions"]
        if action.get("conversation_id") == conversation_id
        and action.get("status") == "pending"
        and (action_type is None or action.get("action_type") == action_type)
    ]
    if not actions:
        return None
    actions.sort(key=lambda action: int(action.get("id", 0)), reverse=True)
    return copy.deepcopy(actions[0])


def finish_pending_action(action_id: int, status: str = "done") -> None:
    state = load_state()
    for action in state["pending_actions"]:
        if int(action.get("id", 0)) == int(action_id):
            action["status"] = status
            action["updated_at"] = now_text()
            break
    save_state(state)


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
        state = load_state()
        rows = [
            row
            for row in state["meal_history"]
            if row.get("conversation_id") == conversation_id
        ]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        if rows:
            history_text = "\n最近作った献立:\n" + "\n".join(
                f"- {row.get('meal_date')}: {row.get('title')}" for row in rows[:14]
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
- 同じ料理名は30日以内に出さない。
- 似た主菜や同じ味付けは14日以内に避ける。
- 同じ主たんぱく質を2日連続にしない。

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
        lines.extend(
            f"{idx}. {clean_text(step)}"
            for idx, step in enumerate(steps, 1)
            if clean_text(step)
        )
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
    lines.extend(
        f"{idx}. {clean_text(step)}"
        for idx, step in enumerate(steps, 1)
        if clean_text(step)
    )
    return "\n".join(lines)


def mark_selected_meal_cooked(conversation_id: str) -> str:
    pending = get_pending_action(conversation_id, "selected_meal")
    if not pending:
        return "作った献立がまだ選ばれていません。先に A / B / C で選んでください。"

    option = pending["payload"]
    add_meal_history_entry(
        conversation_id,
        clean_text(option.get("title")),
        {"source": "selected_meal", "option": option},
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


def should_adjust_inventory(text: str) -> bool:
    normalized = clean_text(text)
    if any(word in normalized for word in ["買った", "購入", "追加", "在庫追加", "レシート"]):
        return False

    keywords = [
        "使った",
        "使いました",
        "消費",
        "減らして",
        "減らす",
        "減った",
        "捨てた",
        "廃棄",
        "なくなった",
        "残り",
        "在庫修正",
        "在庫を修正",
        "上書き",
        "残量",
        "自分で",
        "別料理",
    ]
    has_keyword = any(keyword in normalized for keyword in keywords)
    has_quantity = any(char.isdigit() for char in normalized) or any(
        unit in normalized for unit in ["個", "本", "枚", "g", "kg", "ml", "パック", "袋", "玉", "半分"]
    )
    cooked_with_items = ("作った" in normalized or "つくった" in normalized) and has_quantity
    return (has_keyword and has_quantity) or cooked_with_items


def apply_manual_inventory_update(conversation_id: str, text: str) -> str:
    current_inventory = get_inventory()
    system = """
あなたは家庭の食材在庫を更新するアシスタントです。
ユーザーの自然文から、在庫を増やす・減らす・残量を上書きする操作を判断します。

ルール:
- 「買った」「購入」「追加」は add。
- 「使った」「消費」「食べた」「捨てた」「廃棄」「自分で料理した」は consume。
- 「残り」「在庫修正」「上書き」「残量」は set。
- current_inventory にない食材を consume しようとしている場合は、勝手に追加しない。
- 数量計算が曖昧なときは、無理に正確計算せず quantity に「残り目安: ...」のように書く。
- 同じ食材名はできるだけ統一する。例: 鶏もも、鶏もも肉。
- 自分で料理した内容が分かる場合は meal_title に料理名を書く。
- 在庫更新後の全在庫を inventory_after に入れる。

必ずJSONだけで返してください。
形式:
{
  "action": "add | consume | set | noop",
  "inventory_after": [{"name":"卵","quantity":"残り4個","note":""}],
  "changed_items": [{"name":"卵","quantity":"2個","change":"consume"}],
  "meal_title": "カレー",
  "note": "補足",
  "preference_notes": ["今後覚えるべき好みや事情"]
}
"""
    user = json.dumps(
        {
            "user_message": text,
            "current_inventory": current_inventory,
        },
        ensure_ascii=False,
    )
    data = openai_json(
        [
            {"role": "system", "content": textwrap.dedent(system).strip()},
            {"role": "user", "content": user},
        ]
    )

    action = clean_text(data.get("action")).lower()
    if action not in {"add", "consume", "set", "noop"}:
        action = "noop"

    if action == "noop":
        return (
            "在庫更新の内容をうまく読み取れませんでした。\n"
            "例: 「使った: 卵2個、豚こま200g」または「在庫修正: 卵 残り4個」"
        )

    inventory_after = normalize_items(data.get("inventory_after", []))
    if not inventory_after and current_inventory:
        return "在庫更新後の内容を確認できなかったため、変更しませんでした。もう少し具体的に送ってください。"

    set_inventory(inventory_after)

    changed_items = normalize_items(data.get("changed_items", []))
    meal_title = clean_text(data.get("meal_title"))
    if meal_title and action == "consume":
        add_meal_history_entry(
            conversation_id,
            meal_title,
            {
                "source": "manual_inventory_update",
                "user_message": text,
                "changed_items": changed_items,
            },
        )

    preference_notes = data.get("preference_notes", [])
    if isinstance(preference_notes, list):
        for note in preference_notes:
            add_preference("note", clean_text(note), "manual_inventory_update")

    action_label = {
        "add": "追加",
        "consume": "消費",
        "set": "修正",
    }.get(action, "更新")
    changed_text = format_inventory(changed_items)
    note_text = clean_text(data.get("note"))
    note_block = f"\n\nメモ: {note_text}" if note_text else ""
    return (
        f"在庫を{action_label}しました。\n\n"
        f"変更内容:\n{changed_text}\n\n"
        f"現在の在庫:\n{format_inventory(get_inventory())}"
        f"{note_block}"
    )


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
    elif should_adjust_inventory(text):
        reply = apply_manual_inventory_update(conversation_id, text)
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
- 「使った: 卵2個、豚こま200g」: 在庫を減らします
- 「捨てた: キャベツ半玉」: 在庫を減らします
- 「在庫修正: 卵 残り4個」: 残量を上書きします
- 「今日は自分でカレー作った。鶏もも300g、玉ねぎ2個使った」: 在庫を減らし、料理履歴にも残します
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
            "storage_backend": STORAGE_BACKEND,
            "supabase_configured": USE_SUPABASE,
            "storage_ready": not bool(storage_init_error),
            "storage_error": storage_init_error,
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


init_storage()

if ENABLE_SCHEDULER:
    threading.Thread(target=scheduler_loop, daemon=True).start()
    logger.info("Scheduler started at %02d:%02d JST", SCHEDULE_HOUR, SCHEDULE_MINUTE)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
