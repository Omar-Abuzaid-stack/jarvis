"""
JARVIS Memory - Supabase Cloud implementation.
Replaces local SQLite with Supabase for persistent, cloud-hosted context.
"""

import json
import logging
import time
import os
from datetime import datetime
from pathlib import Path
from supabase import create_client, Client
from model_router import MODEL_ROUTER

log = logging.getLogger("jarvis.memory")

# Load credentials from environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_client: Client = None

def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            # Fallback for headless/env missing
            return None
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client

def init_db():
    """Table creation is done via SQL Editor in Supabase UI."""
    log.info("Cloud Memory (Supabase) integration active")

# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

def remember(content: str, mem_type: str = "fact", source: str = "", importance: int = 5) -> int:
    client = get_client()
    if not client: return 0
    
    try:
        data = {
            "content": content,
            "category": mem_type,
            "metadata": {"source": source, "importance": importance}
        }
        res = client.table("jarvis_memory").insert(data).execute()
        log.info(f"Stored cloud memory: {content[:60]}")
        return 1
    except Exception as e:
        log.error(f"Failed to store cloud memory: {e}")
        return 0

def recall(query: str, limit: int = 5) -> list[dict]:
    """Search memories. Fallback to basic search if vectors aren't setup."""
    client = get_client()
    if not client: return []
    try:
        # Basic text search as fallback to FTS
        res = client.table("jarvis_memory").select("*").ilike("content", f"%{query}%").limit(limit).execute()
        return [{"id": r["id"], "content": r["content"], "type": r["category"]} for r in res.data]
    except Exception as e:
        log.error(f"Recall failed: {e}")
        return []

def get_recent_memories(limit: int = 10) -> list[dict]:
    client = get_client()
    if not client: return []
    try:
        res = client.table("jarvis_memory").select("*").order("created_at", desc=True).limit(limit).execute()
        return [{"id": r["id"], "content": r["content"], "type": r["category"]} for r in res.data]
    except Exception:
        return []

def get_important_memories(limit: int = 10) -> list[dict]:
    # Placeholder: fetch by importance stored in metadata if needed
    return get_recent_memories(limit)

# ---------------------------------------------------------------------------
# Tasks (Mapping to memory for now or separate table)
# ---------------------------------------------------------------------------

def create_task(title: str, description: str = "", priority: str = "medium",
                due_date: str = "", due_time: str = "", project: str = "",
                tags: list[str] = None) -> int:
    return remember(f"TASK: {title} - {description} (Priority: {priority}, Due: {due_date})", mem_type="task")

def get_open_tasks(project: str = None) -> list[dict]:
    client = get_client()
    if not client: return []
    try:
        res = client.table("jarvis_memory").select("*").eq("category", "task").limit(50).execute()
        tasks = []
        for r in res.data:
            tasks.append({"title": r["content"], "priority": "medium", "id": r["id"]})
        return tasks
    except Exception:
        return []

def complete_task(task_id: int):
    client = get_client()
    if client:
        client.table("jarvis_memory").delete().eq("id", task_id).execute()

# ---------------------------------------------------------------------------
# Context Builder
# ---------------------------------------------------------------------------

def build_memory_context(user_message: str) -> str:
    parts = []
    
    # Simple recall
    relevant = recall(user_message, limit=3)
    if relevant:
        mem_lines = [f"  - {m['content']}" for m in relevant]
        parts.append("RELEVANT MEMORIES:\n" + "\n".join(mem_lines))
        
    return "\n\n".join(parts) if parts else ""

async def extract_memories(user_text: str, jarvis_response: str, gemini_client) -> list[str]:
    # Logic remains similar to local but uses cloud remember
    if not gemini_client or len(user_text) < 15:
        return []
    try:
        response = await MODEL_ROUTER.complete(
            client=gemini_client,
            max_tokens=200,
            task_type="memory",
            purpose="memory extraction",
            messages=[
                {"role": "system", "content": "Extract concrete facts. Return JSON array [{\"content\": \"...\"}]"},
                {"role": "user", "content": f"User: {user_text}\nJARVIS: {jarvis_response}"},
            ],
        )
        items = json.loads(response.choices[0].message.content.strip())
        stored = []
        for item in items:
            remember(item["content"])
            stored.append(item["content"])
        return stored
    except Exception:
        return []

def init_db(): pass
def get_tasks_for_date(d): return []
def search_tasks(q, l=10): return []
def create_note(c, t="", to="", tags=None): return 0
def search_notes(q, l=10): return []
def get_notes_by_topic(t): return []
def format_tasks_for_voice(t): return f"I have {len(t)} items saved in the cloud, sir."
def format_plan_for_voice(t, e): return "Your cloud synchronization is active, sir."
