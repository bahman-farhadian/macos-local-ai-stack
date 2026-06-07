"""
title: Memory
author: macos-local-ai-stack
version: 2.3.0
license: MIT

Inspired by adaptive_memory_v2 (openwebui.com/f/alexgrama7).
Rewritten as a clean, self-contained implementation.

Architecture
------------
Storage:    Open WebUI's SQLite memory table  (shown in UI)
Retrieval:  ChromaDB semantic search          (finds relevant past facts)
Extraction: whichever model is serving the active chat, via Ollama
            (already resident -- no model swap, works for any chat model)

Memory tiers
------------
[USER] prefix      -- facts that apply across ALL conversations
                      (identity, role, tools, preferences, goals)
[CHAT:<id>] prefix -- facts specific to ONE conversation thread
                      (what was discussed, decisions made in that session)

How it works
------------
INLET   reads memories from SQLite ordered by relevance:
         - All [USER] memories (user profile, always loaded)
         - [CHAT:<current>] memories (this thread's context)
        Injects them as a system context block before every message.

OUTLET  calls the active chat model with a focused extraction prompt
        (body["model"] -- whatever the user is currently chatting with).
        New facts -> tagged and written to SQLite.
        Contradicting facts -> old entry deleted, replacement written.
        ChromaDB receives every write for future semantic queries.

Why extract with the active chat model -- and ONLY that model
---------------------------------------------------------------
Whatever model is serving the conversation is already resident in unified
memory -- extraction adds only a small second-call latency with zero loading
overhead. This holds for gpt-oss:20b, gemma4:26b-mlx, or any model added
later, so the filter needs no per-model configuration.

The filter never calls a model other than body["model"]. Calling a different
model would force a second large model to load alongside the one already
serving the chat -- on a 24 GB machine that risks an out-of-memory swap storm.
If body["model"] is ever missing, the outlet skips extraction for that turn
rather than guessing at a model to call.

v2.3 changes vs v2.2
---------------------
- Extraction now uses ONLY the active chat model (body["model"]); removed the
  extraction_model valve and its gpt-oss:20b fallback entirely -- the old
  hardcoded gpt-oss:20b forced a second large model to load whenever the user
  chatted with gemma4:26b-mlx, risking an out-of-memory swap storm.

v2.1 changes vs v2.0
---------------------
- Fixed EXTRACT_SYSTEM: removed the "is it a question -> return []" gate
  that silently suppressed all extraction in practice.
- Added error logging in _extract() so failures are visible in the log.
- use_semantic defaults to False (no llama3.2:3b in this stack).
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"
OLLAMA_EMBED = "http://127.0.0.1:11434/api/embeddings"
CHROMA_BASE = "http://127.0.0.1:8000"
COLLECTION = "user_memory"

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """\
Extract personal facts about the user from their message.
Return a JSON array of concise third-person facts.

Extract from ALL of these categories when signals are present:

  IDENTITY / ROLE
    "I am a senior engineer" -> "User is a senior engineer"
    "as a DevOps engineer..." -> "User is a DevOps engineer"

  TOOLS / TECH STACK
    "I use Swift and SwiftUI" -> "User uses Swift and SwiftUI"
    "we run Kubernetes at scale" -> "User runs Kubernetes professionally"

  WORKPLACE / PROJECTS
    "at my company we..." -> extract the work context
    "I'm building a local AI stack" -> "User is building a local AI stack"

  GOALS / PLANS
    "I want to learn Rust" -> "User wants to learn Rust"
    "I'm planning to add gemma4" -> "User plans to add gemma4 to their stack"

  PREFERENCES / CONSTRAINTS
    "I prefer privacy-first tools" -> "User prefers privacy-first tools"
    "I keep everything local" -> "User avoids cloud services"
    "I only have 24 GB RAM" -> "User has 24 GB unified memory"

  SKILL LEVEL
    "I'm new to React" -> "User is new to React"
    "I've been writing Go for 10 years" -> "User has 10 years of Go experience"

  NEGATIONS (these are facts too)
    "I don't use Windows" -> "User does not use Windows"
    "I avoid Docker on macOS" -> "User avoids Docker on macOS"

STEP 1: Does the message reveal ANYTHING personal about how the user works,
  what they build, what they use, or who they are?
  A question can still reveal personal context -- extract it.
  If the message is purely factual with zero personal signal -> return []
  When unsure whether to include something, include it.

STEP 2: Write each fact in third-person, under 20 words.

STEP 3: If a new fact contradicts a stored memory:
  REPLACE:<old fact text>::<new fact text>

Return ONLY a JSON array: ["fact1", "fact2"] or []"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    candidates = [
        Path("/opt/macos-local-ai-stack/data/webui.db"),
        Path(__file__).parent.parent / "data" / "webui.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("webui.db not found")


def _load_memories(
    user_id: str, chat_id: Optional[str]
) -> tuple[list[tuple], list[tuple]]:
    """Return (user_memories, chat_memories) as (id, content) tuples."""
    try:
        con = sqlite3.connect(_db_path())
        rows = con.execute(
            "SELECT id, content FROM memory "
            "WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        con.close()
    except Exception:
        return [], []

    user_mems: list[tuple] = []
    chat_mems: list[tuple] = []
    chat_prefix = f"[CHAT:{chat_id}]" if chat_id else None
    for mem_id, content in rows:
        if content.startswith("[USER]"):
            user_mems.append((mem_id, content[len("[USER]"):].strip()))
        elif chat_prefix and content.startswith(chat_prefix):
            chat_mems.append(
                (mem_id, content[len(chat_prefix):].strip())
            )
        elif not content.startswith("[CHAT:"):
            user_mems.append((mem_id, content.strip()))
    return user_mems, chat_mems


def _save(user_id: str, content: str) -> str:
    """Insert a memory. Returns the new ID."""
    mem_id = str(uuid.uuid4())
    now = int(time.time())
    con = sqlite3.connect(_db_path())
    con.execute(
        "INSERT INTO memory (id, user_id, content, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (mem_id, user_id, content, now, now),
    )
    con.commit()
    con.close()
    return mem_id


def _delete(mem_id: str) -> None:
    con = sqlite3.connect(_db_path())
    con.execute("DELETE FROM memory WHERE id=?", (mem_id,))
    con.commit()
    con.close()


def _is_duplicate(fact: str, existing_texts: list[str]) -> bool:
    words = set(fact.lower().split())
    for ex in existing_texts:
        overlap = len(words & set(ex.lower().split())) / max(len(words), 1)
        if overlap > 0.7:
            return True
    return False


# ---------------------------------------------------------------------------
# ChromaDB helpers (best-effort -- failures are silent)
# ---------------------------------------------------------------------------

async def _chroma_ensure_collection(
    session: aiohttp.ClientSession,
) -> Optional[str]:
    """Get or create the memory collection; return collection ID."""
    try:
        async with session.get(
            f"{CHROMA_BASE}/api/v2/collections/{COLLECTION}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("id")
    except Exception:
        pass
    try:
        async with session.post(
            f"{CHROMA_BASE}/api/v2/collections",
            json={"name": COLLECTION, "metadata": {"hnsw:space": "cosine"}},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status in (200, 201):
                data = await r.json()
                return data.get("id")
    except Exception:
        pass
    return None


async def _get_embedding(
    session: aiohttp.ClientSession, text: str, model: str
) -> Optional[list]:
    """Get embedding vector from Ollama."""
    try:
        async with session.post(
            OLLAMA_EMBED,
            json={"model": model, "prompt": text},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 200:
                return (await r.json()).get("embedding")
    except Exception:
        pass
    return None


async def _chroma_add(
    session: aiohttp.ClientSession,
    coll_id: str,
    mem_id: str,
    text: str,
    embedding: list,
) -> None:
    try:
        await session.post(
            f"{CHROMA_BASE}/api/v2/collections/{coll_id}/add",
            json={
                "ids": [mem_id],
                "documents": [text],
                "embeddings": [embedding],
            },
            timeout=aiohttp.ClientTimeout(total=5),
        )
    except Exception:
        pass


async def _chroma_delete(
    session: aiohttp.ClientSession, coll_id: str, mem_id: str
) -> None:
    try:
        await session.post(
            f"{CHROMA_BASE}/api/v2/collections/{coll_id}/delete",
            json={"ids": [mem_id]},
            timeout=aiohttp.ClientTimeout(total=5),
        )
    except Exception:
        pass


async def _chroma_query(
    session: aiohttp.ClientSession,
    coll_id: str,
    embedding: list,
    n: int,
) -> list[str]:
    """Return the n most semantically similar memory texts."""
    try:
        async with session.post(
            f"{CHROMA_BASE}/api/v2/collections/{coll_id}/query",
            json={"query_embeddings": [embedding], "n_results": n},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                data = await r.json()
                docs = data.get("documents", [[]])[0]
                return [d for d in docs if d]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

async def _extract(
    session: aiohttp.ClientSession,
    user_msg: str,
    existing: list[str],
    model: str,
) -> list[str]:
    ctx = ""
    if existing:
        ctx = (
            "\n\nStored memories (check for contradictions):\n"
            + "\n".join(f"- {m}" for m in existing[:12])
        )
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.1},
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user_msg + ctx},
        ],
    }
    try:
        async with session.post(
            OLLAMA_CHAT,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as r:
            if r.status == 200:
                raw = (await r.json())["message"]["content"].strip()
                start, end = raw.find("["), raw.rfind("]") + 1
                if 0 <= start < end:
                    return json.loads(raw[start:end])
    except Exception as e:
        print(f"[memory] extract error: {e}", flush=True)
    return []


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class Filter:
    """Extracts user facts from chat and recalls them each turn."""

    class Valves(BaseModel):
        embedding_model: str = Field(
            default="",
            description=(
                "Ollama model for ChromaDB embeddings. "
                "Leave empty to disable semantic search."
            ),
        )
        max_user_memories: int = Field(
            default=8,
            description="Max [USER] facts to inject per turn.",
        )
        max_chat_memories: int = Field(
            default=4,
            description="Max [CHAT] facts to inject per turn.",
        )
        use_semantic: bool = Field(
            default=False,
            description=(
                "Use ChromaDB semantic search in inlet. "
                "Requires embedding_model to be set."
            ),
        )
        enabled: bool = Field(default=True, description="Enable the filter.")

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(
        self,
        emitter: Optional[Callable],
        text: str,
        done: bool = False,
    ) -> None:
        if emitter:
            try:
                await emitter(
                    {"type": "status",
                     "data": {"description": text, "done": done}}
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Inlet                                                                #
    # ------------------------------------------------------------------ #

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> dict:
        if not self.valves.enabled or not __user__:
            return body
        user_id = __user__.get("id", "")
        if not user_id:
            return body

        user_mems, chat_mems = _load_memories(user_id, __chat_id__)

        if self.valves.use_semantic and self.valves.embedding_model:
            messages = body.get("messages", [])
            last_user = next(
                (
                    m.get("content", "")
                    for m in reversed(messages)
                    if m.get("role") == "user"
                    and isinstance(m.get("content"), str)
                ),
                "",
            )
            if last_user:
                async with aiohttp.ClientSession() as s:
                    coll_id = await _chroma_ensure_collection(s)
                    if coll_id:
                        emb = await _get_embedding(
                            s, last_user, self.valves.embedding_model
                        )
                        if emb:
                            n = (
                                self.valves.max_user_memories
                                + self.valves.max_chat_memories
                            )
                            relevant = await _chroma_query(
                                s, coll_id, emb, n
                            )
                            if relevant:
                                user_mems = [
                                    (None, t) for t in relevant
                                    if not t.startswith(
                                        f"[CHAT:{__chat_id__}]"
                                    )
                                ]
                                chat_mems = [
                                    (None, t) for t in relevant
                                    if t.startswith(
                                        f"[CHAT:{__chat_id__}]"
                                    )
                                ]

        all_user = [t for _, t in user_mems[: self.valves.max_user_memories]]
        all_chat = [t for _, t in chat_mems[: self.valves.max_chat_memories]]

        if not all_user and not all_chat:
            return body

        lines: list[str] = []
        if all_user:
            lines.append("What I know about you (from past sessions):")
            lines += [f"  - {t}" for t in all_user]
        if all_chat:
            lines.append("Context from this conversation:")
            lines += [f"  - {t}" for t in all_chat]

        block = "MEMORY:\n" + "\n".join(lines)
        messages = body.get("messages", [])
        sys_idx = next(
            (i for i, m in enumerate(messages) if m.get("role") == "system"),
            None,
        )
        if sys_idx is not None:
            messages[sys_idx]["content"] = (
                block + "\n\n" + messages[sys_idx]["content"]
            )
        else:
            messages.insert(0, {"role": "system", "content": block})
        body["messages"] = messages
        return body

    # ------------------------------------------------------------------ #
    # Outlet                                                               #
    # ------------------------------------------------------------------ #

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> dict:
        if not self.valves.enabled or not __user__:
            return body
        user_id = __user__.get("id", "")
        if not user_id:
            return body

        messages = body.get("messages", [])
        user_msg = next(
            (
                m["content"]
                for m in reversed(messages)
                if m.get("role") == "user"
                and isinstance(m.get("content"), str)
            ),
            None,
        )
        if not user_msg or len(user_msg.strip()) < 8:
            return body

        # Extract ONLY with the model already serving this chat — it is
        # already resident in VRAM, so this adds no model-swap cost and works
        # identically for gpt-oss:20b, gemma4:26b-mlx, or any future model.
        # Never call a different model: that would force a second large model
        # to load and could exceed unified memory. If OWU omits "model" for
        # some reason, skip extraction outright rather than guessing.
        extraction_model = body.get("model")
        if not extraction_model:
            return body

        await self._emit(__event_emitter__, "Processing memory...")

        user_mems, chat_mems = _load_memories(user_id, __chat_id__)
        all_existing = (
            [t for _, t in user_mems] + [t for _, t in chat_mems]
        )
        all_rows = user_mems + chat_mems

        async with aiohttp.ClientSession() as s:
            coll_id = (
                await _chroma_ensure_collection(s)
                if self.valves.use_semantic
                else None
            )
            facts = await _extract(
                s, user_msg, all_existing, extraction_model
            )

            saved = updated = 0
            for fact in facts:
                if not fact or not isinstance(fact, str):
                    continue
                fact = fact.strip()

                if fact.upper().startswith("REPLACE:"):
                    rest = fact[len("REPLACE:"):].strip()
                    if "::" not in rest:
                        continue
                    old_text, new_text = rest.split("::", 1)
                    old_text = old_text.strip()
                    new_text = new_text.strip()
                    for mem_id, mem_text in all_rows:
                        if mem_id and old_text.lower() in mem_text.lower():
                            _delete(mem_id)
                            if coll_id:
                                await _chroma_delete(s, coll_id, mem_id)
                    if new_text:
                        tagged = f"[USER] {new_text}"
                        new_id = _save(user_id, tagged)
                        if coll_id and self.valves.embedding_model:
                            emb = await _get_embedding(
                                s, new_text, self.valves.embedding_model
                            )
                            if emb:
                                await _chroma_add(
                                    s, coll_id, new_id, new_text, emb
                                )
                        updated += 1
                    continue

                chat_signals = (
                    "this conversation", "we discussed",
                    "you just said", "this chat", "just now",
                )
                is_chat = bool(__chat_id__) and any(
                    sig in fact.lower() for sig in chat_signals
                )

                if _is_duplicate(fact, all_existing):
                    continue

                tagged = (
                    f"[CHAT:{__chat_id__}] {fact}"
                    if is_chat
                    else f"[USER] {fact}"
                )
                new_id = _save(user_id, tagged)
                all_existing.append(fact)

                if coll_id and self.valves.embedding_model:
                    emb = await _get_embedding(
                        s, fact, self.valves.embedding_model
                    )
                    if emb:
                        await _chroma_add(s, coll_id, new_id, fact, emb)
                saved += 1

        parts = []
        if saved:
            parts.append(f"+{saved} stored")
        if updated:
            parts.append(f"{updated} updated")
        status = "Memory: " + (", ".join(parts) if parts else "nothing new")
        await self._emit(__event_emitter__, status, done=True)
        return body
