"""
JARVIS Memory - Supabase Cloud implementation with Infinity Pruning.
Replaces local SQLite with Supabase for persistent, cloud-hosted context.
Includes "Infinity Memory" logic: removes 1 day of memories every 2 weeks.
"""

import json
import logging
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from supabase import create_client, Client
from model_router import MODEL_ROUTER

log = logging.getLogger("jarvis.memory")

# Load credentials from environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PRUNE_INTERVAL = int(os.getenv("JARVIS_PRUNE_INTERVAL_DAYS", "14"))

_client: Client = None

def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            return None
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client

# ---------------------------------------------------------------------------
# Infinity Memory Maintenance
# ---------------------------------------------------------------------------

def maintenance():
    """Runs pruning logic to maintain 'Infinity Memory'."""
    client = get_client()
    if not client: return

    try:
        # 1. Check last maintenance date from a special system record
        res = client.table("jarvis_memory").select("*").eq("category", "system_status").eq("content", "last_prune").execute()
        
        now = datetime.utcnow()
        should_prune = False
        
        if not res.data:
            # First time setup
            client.table("jarvis_memory").insert({
                "category": "system_status",
                "content": "last_prune",
                "metadata": {"date": now.isoformat()}
            }).execute()
        else:
            last_prune = datetime.fromisoformat(res.data[0]["metadata"]["date"])
            if (now - last_prune).days >= PRUNE_INTERVAL:
                should_prune = True
        
        if should_prune:
            log.info("Infinity Memory: 2 weeks passed. Pruning one day of old memories...")
            
            # Find the oldest day
            oldest_res = client.table("jarvis_memory").select("created_at").order("created_at", desc=False).limit(1).execute()
            
            if oldest_res.data:
                oldest_stamp = oldest_res.data[0]["created_at"]
                # Delete everything within that first 24-hour window
                cutoff = (datetime.fromisoformat(oldest_stamp.replace('Z', '+00:00')) + timedelta(days=1)).isoformat()
                client.table("jarvis_memory").delete().lt("created_at", cutoff).neq("category", "system_status").execute()
                
                # Update last prune date
                client.table("jarvis_memory").update({"metadata": {"date": now.isoformat()}}).eq("content", "last_prune").execute()
                log.info("Infinity Memory: Oldest day pruned successfully.")

    except Exception as e:
        log.error(f"Maintenance failed: {e}")

# ---------------------------------------------------------------------------
# Core Memory Functions
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
        client.table("jarvis_memory").insert(data).execute()
        return 1
    except Exception as e:
        log.error(f"Failed to store cloud memory: {e}")
        return 0

def recall(query: str, limit: int = 5) -> list[dict]:
    client = get_client()
    if not client: return []
    try:
        res = client.table("jarvis_memory").select("*").ilike("content", f"%{query}%").limit(limit).execute()
        return [{"id": r["id"], "content": r["content"], "type": r["category"]} for r in res.data]
    except Exception:
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
    return get_recent_memories(limit)

def create_task(title: str, description: str = "", priority: str = "medium",
                due_date: str = "", due_time: str = "", project: str = "",
                tags: list[str] = None) -> int:
    client = get_client()
    if not client: return 0
    try:
        data = {
            "title": title,
            "description": description,
            "priority": priority,
            "status": "open",
            "due_date": due_date if due_date else None
        }
        res = client.table("jarvis_tasks").insert(data).execute()
        return res.data[0]["id"]
    except Exception as e:
        log.error(f"Failed to create cloud task: {e}")
        return 0

def get_open_tasks(project: str = None) -> list[dict]:
    client = get_client()
    if not client: return []
    try:
        res = client.table("jarvis_tasks").select("*").eq("status", "open").limit(20).execute()
        return res.data
    except Exception:
        return []

def complete_task(task_id: int):
    client = get_client()
    if client:
        client.table("jarvis_tasks").update({"status": "done"}).eq("id", task_id).execute()

def build_memory_context(user_message: str) -> str:
    parts = []
    relevant = recall(user_message, limit=3)
    if relevant:
        mem_lines = [f"  - {m['content']}" for m in relevant]
        parts.append("RELEVANT MEMORIES:\n" + "\n".join(mem_lines))
    return "\n\n".join(parts) if parts else ""

async def extract_memories(user_text: str, jarvis_response: str, gemini_client) -> list[str]:
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

def init_db():
    maintenance()

# Stubs for compatibility
def get_tasks_for_date(d): return []
def search_tasks(q, l=10): return []
def create_note(c, t="", to="", tags=None): return 0
def search_notes(q, l=10): return []
def get_notes_by_topic(t): return []
def format_tasks_for_voice(t): return f"You have {len(t)} tasks synced to the cloud, sir."
def format_plan_for_voice(t, e): return "Your infinity memory loop is currently stable."
