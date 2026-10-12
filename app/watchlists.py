"""Watchlist CRUD + per-watchlist config validation."""
from __future__ import annotations

import logging
from typing import Any

from . import db
from .scoring import presets
from .sources import LABELS, REGISTRY

log = logging.getLogger("watchlistarr.watchlists")

ACCENTS = [
    "#7c5cff", "#22d3ee", "#f472b6", "#34d399", "#fbbf24",
    "#fb7185", "#a78bfa", "#38bdf8", "#4ade80", "#f97316",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_per_run": 1,
    "rules": [],
    "root_folder": None,
    "quality_profile": None,
    "tags": [],
    "search_on_add": True,
}


def list_watchlists() -> list[dict[str, Any]]:
    return [
        db.row_to_watchlist(r)
        for r in db.query("SELECT * FROM watchlists ORDER BY sort_order, id")
    ]


def get_watchlist(wl_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM watchlists WHERE id=?", (wl_id,))
    return db.row_to_watchlist(row) if row else None


def _merged_config(raw: Any) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
    if not isinstance(cfg.get("rules"), list):
        cfg["rules"] = []
    try:
        cfg["max_per_run"] = max(0, int(cfg.get("max_per_run") or 0))
    except (TypeError, ValueError):
        cfg["max_per_run"] = 1
    cfg["tags"] = [t for t in (cfg.get("tags") or []) if isinstance(t, str) and t.strip()]
    return cfg


def create_watchlist(
    *,
    name: str,
    source_type: str,
    source_ref: dict[str, Any],
    config: dict[str, Any] | None = None,
    accent: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if source_type not in REGISTRY:
        raise ValueError(
            f"Unknown source '{source_type}'. Choose one of: {', '.join(sorted(REGISTRY))}."
        )
    order = (db.one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM watchlists") or {"m": 0})["m"]
    wl_id = db.insert(
        "INSERT INTO watchlists(name, source_type, source_ref, enabled, accent, sort_order,"
        " config, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            (name or "Untitled").strip()[:120],
            source_type,
            db.json.dumps(source_ref or {}),
            1 if enabled else 0,
            accent or ACCENTS[order % len(ACCENTS)],
            order + 1,
            db.json.dumps(_merged_config(config)),
            db.now(), db.now(),
        ),
    )
    return get_watchlist(wl_id)  # type: ignore[return-value]


def update_watchlist(wl_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
    wl = get_watchlist(wl_id)
    if wl is None:
        return None
    fields: dict[str, Any] = {}
    if "name" in patch and str(patch["name"]).strip():
        fields["name"] = str(patch["name"]).strip()[:120]
    if "enabled" in patch:
        fields["enabled"] = 1 if patch["enabled"] else 0
    if "accent" in patch and str(patch["accent"]).startswith("#"):
        fields["accent"] = str(patch["accent"])[:9]
    if "source_ref" in patch and isinstance(patch["source_ref"], dict):
        merged = dict(wl["source_ref"])
        merged.update({k: v for k, v in patch["source_ref"].items() if v not in (None, "")})
        fields["source_ref"] = db.json.dumps(merged)
    if "config" in patch and isinstance(patch["config"], dict):
        fields["config"] = db.json.dumps(_merged_config({**wl["config"], **patch["config"]}))
    if not fields:
        return wl
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE watchlists SET {sets} WHERE id=?", [*fields.values(), wl_id])
    return get_watchlist(wl_id)


def delete_watchlist(wl_id: int) -> bool:
    cur = db.execute("DELETE FROM watchlists WHERE id=?", (wl_id,))
    return cur.rowcount > 0


def reorder(wl_ids: list[int]) -> None:
    for i, wid in enumerate(wl_ids):
        db.execute("UPDATE watchlists SET sort_order=? WHERE id=?", (i + 1, wid))


def catalogue() -> dict[str, Any]:
    """Everything the add/edit watchlist form needs."""
    return {
        "sources": [
            {"type": key, "label": LABELS[key], "fields": _fields_for(key)}
            for key in sorted(REGISTRY)
        ],
        "accents": ACCENTS,
        "presets": presets(),
    }


def _fields_for(source_type: str) -> list[dict[str, Any]]:
    if source_type == "letterboxd":
        return [
            {"name": "list_url", "label": "List URL", "required": False,
             "placeholder": "https://letterboxd.com/user/list/slug/",
             "help": "Paste the whole list URL and the username/slug fill in automatically."},
            {"name": "username", "label": "Username", "required": False, "placeholder": "dave"},
            {"name": "list_slug", "label": "List slug", "required": False,
             "placeholder": "film-archive",
             "help": "Leave the slug empty to use the member's full watchlist."},
            {"name": "max_items", "label": "Max items", "type": "number", "required": False,
             "help": "0 or blank = all of them."},
        ]
    return [
        {"name": "list_url", "label": "List URL or ID", "required": True,
         "placeholder": "https://www.imdb.com/list/ls012733158/"},
        {"name": "max_items", "label": "Max items", "type": "number", "required": False},
    ]