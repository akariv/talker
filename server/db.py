"""Firestore helpers for client auth, conversations, and response queues.

Falls back to in-memory storage when Firestore is unavailable (local dev).
"""

import os
import time
import logging
from collections import defaultdict

log = logging.getLogger(__name__)

# Try to import Firestore; fall back to in-memory for local dev
try:
    from google.cloud import firestore
    _firestore_db = firestore.Client()
    USE_FIRESTORE = True
    log.info("Using Firestore backend")
except Exception:
    _firestore_db = None
    USE_FIRESTORE = False
    log.info("Firestore unavailable — using in-memory backend")

# ---- In-memory fallback ----
_mem_clients: dict[str, dict] = {}
_mem_conversations: dict[str, list[dict]] = defaultdict(list)
_mem_queue: dict[str, list[dict]] = defaultdict(list)
_mem_display_config: dict = {}

# Display config (calendar list, etc.) is small and rarely changes.
# Cache 5 min in-process to avoid a Firestore read on every panel render.
_DISPLAY_CONFIG_TTL_SECONDS = 300
_display_config_cache: dict = {"value": None, "fetched_at": 0.0}


def get_display_config() -> dict:
    """Return the panel's display config (currently {'calendars': [...]}).

    Cached for 5 minutes. On Firestore failure we keep returning the
    previous cached value (stale-on-error). Returns `{}` if no config has
    ever been seeded.
    """
    now = time.monotonic()
    cached = _display_config_cache
    if cached["value"] is not None and (now - cached["fetched_at"]) < _DISPLAY_CONFIG_TTL_SECONDS:
        return cached["value"]

    if not USE_FIRESTORE:
        return _mem_display_config

    try:
        doc = _firestore_db.collection("config").document("display").get()
        cfg = doc.to_dict() if doc.exists else {}
    except Exception as e:
        log.warning(f"display config fetch failed: {e}")
        return cached["value"] or {}

    cached["value"] = cfg
    cached["fetched_at"] = now
    return cfg


def set_display_config(cfg: dict) -> None:
    """Overwrite `config/display` in Firestore (or in-memory in dev)."""
    if USE_FIRESTORE:
        _firestore_db.collection("config").document("display").set(cfg)
    else:
        _mem_display_config.clear()
        _mem_display_config.update(cfg)
    _display_config_cache["value"] = None  # invalidate


def add_client(name: str, api_key: str, location: str = "", owner: str = "", system_prompt: str = ""):
    """Register a client (for setup/seeding)."""
    rec = {
        "api_key": api_key,
        "location": location,
        "owner": owner,
        "system_prompt": system_prompt,
        "created_at": time.time(),
    }
    if USE_FIRESTORE:
        _firestore_db.collection("clients").document(name).set(rec)
    else:
        _mem_clients[name] = rec


def get_client(name: str) -> dict | None:
    if USE_FIRESTORE:
        doc = _firestore_db.collection("clients").document(name).get()
        return doc.to_dict() if doc.exists else None
    return _mem_clients.get(name)


def get_all_clients() -> list[dict]:
    """Return all clients with their name, location, and owner."""
    if USE_FIRESTORE:
        docs = _firestore_db.collection("clients").get()
        results = []
        for doc in docs:
            d = doc.to_dict()
            d["name"] = doc.id
            results.append(d)
        return results
    return [{"name": k, **v} for k, v in _mem_clients.items()]


def get_conversation(client_name: str, limit: int = 50) -> list[dict]:
    if USE_FIRESTORE:
        docs = (
            _firestore_db.collection("conversations")
            .document(client_name)
            .collection("messages")
            .order_by("timestamp")
            .limit_to_last(limit)
            .get()
        )
        return [d.to_dict() for d in docs]
    return _mem_conversations[client_name][-limit:]


def append_message(client_name: str, role: str, content: str):
    msg = {"role": role, "content": content, "timestamp": time.time()}
    if USE_FIRESTORE:
        (
            _firestore_db.collection("conversations")
            .document(client_name)
            .collection("messages")
            .add(msg)
        )
    else:
        _mem_conversations[client_name].append(msg)


def enqueue_response(client_name: str, text: str, sequence: int):
    entry = {
        "text": text,
        "status": "pending",
        "sequence": sequence,
        "created_at": time.time(),
    }
    if USE_FIRESTORE:
        (
            _firestore_db.collection("response_queue")
            .document(client_name)
            .collection("pending")
            .add(entry)
        )
    else:
        _mem_queue[client_name].append(entry)


def poll_next_response(client_name: str) -> dict | None:
    """Get the next pending response, mark it as delivering. Returns None if empty."""
    if USE_FIRESTORE:
        docs = (
            _firestore_db.collection("response_queue")
            .document(client_name)
            .collection("pending")
            .where("status", "==", "pending")
            .get()
        )
        if not docs:
            return None
        # Sort by sequence in Python to avoid needing a composite index
        doc = min(docs, key=lambda d: d.to_dict().get("sequence", 0))
        doc.reference.update({"status": "delivering"})
        return {**doc.to_dict(), "_ref": doc.reference}

    # In-memory
    queue = _mem_queue[client_name]
    for entry in queue:
        if entry["status"] == "pending":
            entry["status"] = "delivering"
            return entry
    return None


def mark_delivered(entry: dict):
    if USE_FIRESTORE and "_ref" in entry:
        entry["_ref"].update({"status": "delivered"})
    else:
        entry["status"] = "delivered"


def mark_failed(entry: dict):
    if USE_FIRESTORE and "_ref" in entry:
        entry["_ref"].update({"status": "pending"})
    else:
        entry["status"] = "pending"
