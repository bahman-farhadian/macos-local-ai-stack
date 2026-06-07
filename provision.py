#!/usr/bin/env python3
"""
provision.py — idempotent first-run configuration for the local AI stack

Launched in the background by start-open-webui.sh. Safe to run on every start;
it checks current state before changing anything. Progress is written to
data/provision.log.

It makes ONE read-only HTTP call (GET /api/version) just to learn when Open
WebUI has finished its database migrations. Everything else is written straight
to the SQLite database — no sign-in, no authenticated API calls — so it can
never block Open WebUI's event loop or fail on a flaky connection:

    - admin account            created if no user exists yet
    - two filter functions     memory, local-vision-bridge
    - per-user defaults        memory on for every account
    - gpt-oss:20b model        filter order + system prompt + params
                               ([local_vision_bridge, memory])
    - gemma4:26b-mlx model     filter order + system prompt + params
                               ([local_vision_bridge, memory] — text-only,
                               needs the bridge to see images, same as gpt-oss)

Credentials come from .env: WEBUI_ADMIN_EMAIL, WEBUI_ADMIN_PASSWORD,
WEBUI_ADMIN_NAME (default "Admin").

Exit codes
  0  Completed successfully.
  1  Fatal — server never responded, or a required file is missing.
"""

import json
import os
import sqlite3
import sys
import time
import uuid
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Always use the HTTP backend directly — the provisioner is a server-side script
# and should never go through the ssl-proxy. WEBUI_URL is for browser access only.
BASE_URL   = "http://127.0.0.1:8080"
DB_PATH    = Path(__file__).parent / "data" / "webui.db"
SCRIPT_DIR = Path(__file__).parent.resolve()
FUNC_DIR   = SCRIPT_DIR / "functions"
CONFIG_DIR = SCRIPT_DIR / "config"

FUNCTION_FILES = {
    "memory": {
        "path": FUNC_DIR / "memory.py",
        "name": "Memory",
        "type": "filter",
        "meta": {"description": "LLM-extracted user facts — recalls them each session via [USER] tags.", "type": "filter"},
        "valves": {
            # No extraction_model valve: the filter extracts ONLY with
            # whichever model is serving the active chat (body["model"]) —
            # never a different one. See functions/memory.py docstring.
            "embedding_model": "",
            "max_user_memories": 8,
            "max_chat_memories": 4,
            "use_semantic": False,
            "enabled": True,
        },
    },
    "local_vision_bridge": {
        "path":  FUNC_DIR / "local-vision-bridge.py",
        "name":  "Local Vision Bridge",
        "type":  "filter",
        "meta":  {"description": "Converts image attachments to text descriptions before other filters run.", "type": "filter"},
        "valves": {
            "priority":          -10,
            "vision_server_url": "http://127.0.0.1:11434/v1/chat/completions",
            "vision_model":      "llama3.2-vision:11b",
        },
    },
}

# Models and their filter pipelines.
# Both gpt-oss:20b and gemma4:26b-mlx are text-only models — neither can see
# images on its own, so both route through the vision bridge before memory.
MODELS = {
    "gpt-oss:20b":    ["local_vision_bridge", "memory"],
    "gemma4:26b-mlx": ["local_vision_bridge", "memory"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[provision] {msg}", flush=True)

def die(msg: str) -> None:
    print(f"[provision] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def wait_for_server(timeout: int = 240) -> None:
    """Wait for Open WebUI's internal HTTP backend to respond.

    Polls http://127.0.0.1:8080/api/version directly (never through the proxy).
    On first boot, Open WebUI runs database migrations and downloads embedding
    weights (~1 minute) before it answers. We poll gently — one request every
    3 seconds with a short timeout — so we never flood the single worker.
    """
    log("Waiting for Open WebUI to finish starting "
        "(first boot can take ~1 minute) ...")
    deadline = time.time() + timeout
    waited = 0
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(f"{BASE_URL}/api/version", timeout=4)
            if r.status == 200:
                log("Open WebUI is up.")
                return
        except Exception:
            pass
        time.sleep(3)
        waited += 3
        if waited % 30 == 0:
            log(f"  still waiting ({waited}s) — see tail -f ./data/webui.log")
    die(f"Open WebUI did not respond within {timeout}s. "
        f"Check: tail -50 ./data/webui.log")


def _hash_password(password: str) -> str:
    try:
        import bcrypt
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    except ImportError:
        from passlib.context import CryptContext
        return CryptContext(schemes=["bcrypt"]).hash(password)


def _db_create_admin(email: str, password: str, name: str) -> None:
    """Create admin directly in SQLite — used when the signup API is disabled."""
    if not DB_PATH.exists():
        die(f"Database not found at {DB_PATH}.")
    user_id = str(uuid.uuid4())
    pw_hash = _hash_password(password)
    now = int(time.time())
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "INSERT INTO user (id, name, email, role, profile_image_url, "
        "created_at, updated_at, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, name, email.lower(), "admin", "/user.png", now, now, now),
    )
    db.execute(
        "INSERT INTO auth (id, email, password, active) VALUES (?,?,?,1)",
        (user_id, email.lower(), pw_hash),
    )
    db.commit()
    db.close()
    log(f"Admin account written to database (email: {email}, name: {name}).")


def ensure_admin_db(email: str, password: str, name: str) -> str:
    """Make sure an admin account exists. Returns the admin user's id.

    Writes directly to SQLite — never uses the HTTP API. This is reliable
    even while Open WebUI is still busy with first-boot work. Open WebUI may
    also create the admin itself from the WEBUI_ADMIN_* env vars; this check
    is idempotent and skips creation if any user already exists.
    """
    if not DB_PATH.exists():
        die(f"Database not found at {DB_PATH}. Open WebUI has not initialised.")
    db = sqlite3.connect(DB_PATH, timeout=15)
    row = db.execute(
        "SELECT id FROM user WHERE email=? LIMIT 1", (email.lower(),)
    ).fetchone()
    if row:
        db.close()
        log(f"Admin account already exists ({email}).")
        return row[0]

    any_user = db.execute("SELECT id FROM user LIMIT 1").fetchone()
    db.close()
    if any_user:
        # A different admin exists — use it rather than creating a duplicate.
        log("An account already exists; skipping admin creation.")
        return any_user[0]

    _db_create_admin(email, password, name)
    db = sqlite3.connect(DB_PATH, timeout=15)
    row = db.execute(
        "SELECT id FROM user WHERE email=? LIMIT 1", (email.lower(),)
    ).fetchone()
    db.close()
    return row[0] if row else ""


# ---------------------------------------------------------------------------
# Function installation — SQLite (the functions HTTP API requires session
# cookies and is not reachable via Bearer token)
# ---------------------------------------------------------------------------

def db_function_exists(db: sqlite3.Connection, fn_id: str) -> bool:
    row = db.execute("SELECT 1 FROM function WHERE id=?", (fn_id,)).fetchone()
    return row is not None


def db_install_function(db: sqlite3.Connection, fn_id: str, spec: dict, user_id: str) -> None:
    code_path: Path = spec["path"]
    if not code_path.exists():
        die(f"Function source not found: {code_path}")
    code = code_path.read_text()

    now = int(time.time())
    meta = json.dumps(spec["meta"])
    valves = json.dumps(spec["valves"])

    if db_function_exists(db, fn_id):
        db.execute(
            "UPDATE function SET content=?, meta=?, valves=?, updated_at=? WHERE id=?",
            (code, meta, valves, now, fn_id),
        )
        log(f"  Updated function: {fn_id}")
    else:
        db.execute(
            "INSERT INTO function (id, user_id, name, type, content, meta, valves, "
            "is_active, is_global, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,1,1,?,?)",
            (fn_id, user_id, spec["name"], spec["type"], code, meta, valves, now, now),
        )
        log(f"  Installed function: {fn_id}")
    db.commit()


def db_valves_match(db: sqlite3.Connection, fn_id: str, expected: dict) -> bool:
    row = db.execute("SELECT valves FROM function WHERE id=?", (fn_id,)).fetchone()
    if not row or not row[0]:
        return False
    try:
        stored = json.loads(row[0])
    except json.JSONDecodeError:
        return False
    for k, v in expected.items():
        if stored.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Model configuration — SQLite (filter order + system prompt + parameters)
#
# Written directly to the model table so provisioning needs NO authenticated
# API call. Open WebUI reads the model table fresh on each /api/models request
# (the browser triggers that on load), so the change is picked up without a
# restart.
# ---------------------------------------------------------------------------

DEFAULT_CAPABILITIES = {
    "vision": True, "file_upload": True, "web_search": True,
    "file_context": True, "image_generation": True, "code_interpreter": True,
    "terminal": True, "citations": True, "status_updates": True,
    "builtin_tools": True,
}


def _model_params_from_env() -> dict:
    """Build the model params dict (system prompt + sampling) from model.env."""
    cfg = load_model_env()
    persona = cfg.get("SYSTEM_USER_PERSONA", "")
    template = cfg.get("SYSTEM_PROMPT", "")
    system = template.replace("{USER_PERSONA}", persona).strip() if template else ""

    def _num(key, cast):
        try:
            return cast(cfg[key])
        except (KeyError, ValueError):
            return None

    params: dict = {}
    if system:
        params["system"] = system
    for key, cast, name in (
        ("MODEL_TEMPERATURE", float, "temperature"),
        ("MODEL_TOP_P", float, "top_p"),
        ("MODEL_TOP_K", int, "top_k"),
        ("MODEL_MAX_TOKENS", int, "max_tokens"),
    ):
        val = _num(key, cast)
        if val is not None:
            params[name] = val
    return params


def ensure_model_db(db: sqlite3.Connection, user_id: str, model_id: str, filter_order: list) -> None:
    """Create or update a model's custom config (filter order + system prompt + params)."""
    new_params = _model_params_from_env()
    now = int(time.time())

    row = db.execute(
        "SELECT meta, params FROM model WHERE id=?", (model_id,)
    ).fetchone()

    if row:
        try:
            meta = json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        try:
            params = json.loads(row[1]) if row[1] else {}
        except (json.JSONDecodeError, TypeError):
            params = {}

        if meta.get("filterIds") == filter_order and all(
            params.get(k) == v for k, v in new_params.items()
        ):
            log(f"  {model_id}: already configured")
            return

        meta["filterIds"] = filter_order
        params.update(new_params)
        db.execute(
            "UPDATE model SET meta=?, params=?, updated_at=? WHERE id=?",
            (json.dumps(meta), json.dumps(params), now, model_id),
        )
        db.commit()
        log(f"  {model_id}: filters and system prompt updated")
        return

    meta = {
        "profile_image_url": "/static/favicon.png",
        "description": None,
        "capabilities": DEFAULT_CAPABILITIES,
        "filterIds": filter_order,
    }
    db.execute(
        "INSERT INTO model (id, user_id, base_model_id, name, meta, params, "
        "created_at, updated_at, is_active) VALUES (?,?,?,?,?,?,?,?,1)",
        (model_id, user_id, None, model_id,
         json.dumps(meta), json.dumps(new_params), now, now),
    )
    db.commit()
    log(f"  {model_id}: created with filters and system prompt")


# ---------------------------------------------------------------------------
# User defaults — enable memory for every user via SQLite
#
# Open WebUI has no admin API to update other users' settings, so we write
# directly to the database. This runs on every start, which means new users
# who sign up between starts will have their settings applied on the next restart.
# ---------------------------------------------------------------------------

def ensure_user_defaults_all(db: sqlite3.Connection) -> None:
    """Enable the memory toggle for every user account."""
    rows = db.execute("SELECT id, settings FROM user").fetchall()
    updated = 0
    for user_id, settings_json in rows:
        try:
            settings = json.loads(settings_json) if settings_json else {}
        except (json.JSONDecodeError, TypeError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        ui = settings.get("ui", {})

        if ui.get("memory") is not True:
            ui["memory"] = True
            settings["ui"] = ui
            db.execute(
                "UPDATE user SET settings=? WHERE id=?",
                (json.dumps(settings), user_id),
            )
            updated += 1

    if updated:
        db.commit()
        log(f"  Default settings applied to {updated} user(s).")
    else:
        log("  Default settings already correct for all users.")


# ---------------------------------------------------------------------------
# Model config — read config/model.env and apply to models
# ---------------------------------------------------------------------------

def load_model_env() -> dict:
    """Parse config/model.env into a dict of KEY→value strings."""
    path = CONFIG_DIR / "model.env"
    if not path.exists():
        return {}
    result = {}
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val.startswith('"'):
            val = val[1:]                       # remove opening quote
            if val.endswith('"'):
                val = val[:-1]                  # single-line quoted value
            else:
                # multi-line: collect until a line that ends with "
                parts = [val]
                while i < len(lines):
                    nxt = lines[i]
                    i += 1
                    if nxt.endswith('"'):
                        parts.append(nxt[:-1])
                        break
                    parts.append(nxt)
                val = "\n".join(parts)
        result[key] = val
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    email    = os.environ.get("WEBUI_ADMIN_EMAIL", "").strip()
    password = os.environ.get("WEBUI_ADMIN_PASSWORD", "").strip()
    name     = os.environ.get("WEBUI_ADMIN_NAME", "Admin").strip() or "Admin"

    if not email or not password:
        die(
            "WEBUI_ADMIN_EMAIL and WEBUI_ADMIN_PASSWORD must be set.\n"
            "  Copy .env.example to .env, fill in your credentials, and\n"
            "  source .env before running start-open-webui.sh."
        )

    wait_for_server()

    # ----------------------------------------------------------------------
    # Phase 1 — everything that can be done via SQLite (no HTTP, never fails)
    # ----------------------------------------------------------------------

    # Admin account (idempotent, SQLite-direct).
    user_id = ensure_admin_db(email, password, name)
    if not user_id:
        die("Could not create or find the admin account.")

    # Install filter functions (memory + local_vision_bridge).
    log("Installing filter functions ...")
    db = sqlite3.connect(DB_PATH, timeout=15)
    for fn_id, spec in FUNCTION_FILES.items():
        if not db_function_exists(db, fn_id):
            log(f"  {fn_id}: installing")
            db_install_function(db, fn_id, spec, user_id)
        elif not db_valves_match(db, fn_id, spec["valves"]):
            log(f"  {fn_id}: updating (settings changed)")
            db_install_function(db, fn_id, spec, user_id)
        else:
            log(f"  {fn_id}: already up to date")

    # Per-user defaults: memory enabled.
    log("Applying user defaults ...")
    ensure_user_defaults_all(db)

    # Model configs: filter order + system prompt + sampling params.
    for model_id, filter_order in MODELS.items():
        log(f"Configuring {model_id} (filters + system prompt) ...")
        ensure_model_db(db, user_id, model_id, filter_order)

    db.close()

    log("Provisioning complete. The UI is ready to use.")


if __name__ == "__main__":
    main()
