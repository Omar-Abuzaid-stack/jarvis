# ---------------------------------------------------------------------------
# JARVIS Expert Knowledge System
# Lightweight topic detection and knowledge injection for Mistral
# ---------------------------------------------------------------------------

import os
import re
from pathlib import Path
from typing import Optional

import logging

log = logging.getLogger("jarvis.knowledge")

# Knowledge base storage
_knowledge_store: dict[str, str] = {}
_knowledge_loaded = False

# Topic keywords mapping -> knowledge file base name (without JARVIS_KNOWLEDGE_X prefix)
TOPIC_KEYWORDS = {
    # VANTILITY (business, SaaS, marketing, automation)
    "vantility": "VANTILITY_BUSINESS",
    "vantility os": "VANTILITY_BUSINESS",
    "saas": "VANTILITY_BUSINESS",
    "subscription": "VANTILITY_BUSINESS",
    "pricing": "VANTILITY_BUSINESS",
    "mrr": "VANTILITY_BUSINESS",
    " ARR": "VANTILITY_BUSINESS",
    "churn": "VANTILITY_BUSINESS",
    "retention": "VANTILITY_BUSINESS",
    "growth": "VANTILITY_BUSINESS",
    "marketing agency": "VANTILITY_BUSINESS",

    # UAE Real Estate
    "real estate": "UAE_REAL_ESTATE",
    "property": "UAE_REAL_ESTATE",
    "dubai": "UAE_REAL_ESTATE",
    "uae property": "UAE_REAL_ESTATE",
    "apartment": "UAE_REAL_ESTATE",
    "villa": "UAE_REAL_ESTATE",
    "investment property": "UAE_REAL_ESTATE",
    "rental yield": "UAE_REAL_ESTATE",
    "off-plan": "UAE_REAL_ESTATE",
    "freehold": "UAE_REAL_ESTATE",

    # Social Media
    "social media": "SOCIAL_MEDIA",
    "instagram": "SOCIAL_MEDIA",
    "tiktok": "SOCIAL_MEDIA",
    "twitter": "SOCIAL_MEDIA",
    "x.com": "SOCIAL_MEDIA",
    "linkedin": "SOCIAL_MEDIA",
    "content": "SOCIAL_MEDIA",
    "viral": "SOCIAL_MEDIA",
    "engagement": "SOCIAL_MEDIA",
    "followers": "SOCIAL_MEDIA",

    # AI Tools
    "ai tool": "AI_TOOLS_MASTERY",
    "claude": "AI_TOOLS_MASTERY",
    "chatgpt": "AI_TOOLS_MASTERY",
    "gpt": "AI_TOOLS_MASTERY",
    "openai": "AI_TOOLS_MASTERY",
    "anthropic": "AI_TOOLS_MASTERY",
    "mistral": "AI_TOOLS_MASTERY",
    "llm": "AI_TOOLS_MASTERY",
    "language model": "AI_TOOLS_MASTERY",
    "prompt": "AI_TOOLS_MASTERY",
    "prompting": "AI_TOOLS_MASTERY",
    "cursor": "AI_TOOLS_MASTERY",
    "windsurf": "AI_TOOLS_MASTERY",
    "v0": "AI_TOOLS_MASTERY",
    "bolt.new": "AI_TOOLS_MASTERY",
    "lovable": "AI_TOOLS_MASTERY",
    "replit": "AI_TOOLS_MASTERY",

    # Technical Architecture
    "architecture": "TECHNICAL_ARCHITECTURE",
    "system design": "TECHNICAL_ARCHITECTURE",
    "api": "TECHNICAL_ARCHITECTURE",
    "database": "TECHNICAL_ARCHITECTURE",
    "frontend": "TECHNICAL_ARCHITECTURE",
    "backend": "TECHNICAL_ARCHITECTURE",
    "infrastructure": "TECHNICAL_ARCHITECTURE",
    "microservice": "TECHNICAL_ARCHITECTURE",
    "container": "TECHNICAL_ARCHITECTURE",
    "docker": "TECHNICAL_ARCHITECTURE",
    "kubernetes": "TECHNICAL_ARCHITECTURE",
    "aws": "TECHNICAL_ARCHITECTURE",
    "cloud": "TECHNICAL_ARCHITECTURE",

    # Automation Systems
    "automation": "AUTOMATION_SYSTEMS",
    "n8n": "AUTOMATION_SYSTEMS",
    "make.com": "AUTOMATION_SYSTEMS",
    "zapier": "AUTOMATION_SYSTEMS",
    "workflow": "AUTOMATION_SYSTEMS",
    "integration": "AUTOMATION_SYSTEMS",
    "webhook": "AUTOMATION_SYSTEMS",
    "api integration": "AUTOMATION_SYSTEMS",
    "mcp": "AUTOMATION_SYSTEMS",
    "model context protocol": "AUTOMATION_SYSTEMS",

    # Content Production
    "content": "CONTENT_PRODUCTION",
    "video": "CONTENT_PRODUCTION",
    "youtube": "CONTENT_PRODUCTION",
    "podcast": "CONTENT_PRODUCTION",
    "blog": "CONTENT_PRODUCTION",
    "seo": "CONTENT_PRODUCTION",
    "copywriting": "CONTENT_PRODUCTION",
    "script": "CONTENT_PRODUCTION",

    # Business Strategy
    "business": "BUSINESS_STRATEGY",
    "strategy": "BUSINESS_STRATEGY",
    "startup": "BUSINESS_STRATEGY",
    "founder": "BUSINESS_STRATEGY",
    "funding": "BUSINESS_STRATEGY",
    "pitch": "BUSINESS_STRATEGY",
    "revenue": "BUSINESS_STRATEGY",
    "profit": "BUSINESS_STRATEGY",
    "roi": "BUSINESS_STRATEGY",

    # Expert Prompting
    "prompt engineer": "EXPERT_PROMPTING",
    "prompting": "EXPERT_PROMPTING",
    "chain of thought": "EXPERT_PROMPTING",
    "cot": "EXPERT_PROMPTING",
    "few-shot": "EXPERT_PROMPTING",
    "roleplay": "EXPERT_PROMPTING",
    "system prompt": "EXPERT_PROMPTING",
}


def _get_knowledge_dir() -> Path:
    """Get the knowledge files directory."""
    return Path.home() / "Desktop"


def _derive_base_name(filename: str) -> str:
    if filename.startswith("JARVIS_KNOWLEDGE_"):
        return filename.replace("JARVIS_KNOWLEDGE_", "").replace(".txt", "").split("_", 1)[1]
    return Path(filename).stem


def _clean_excerpt(text: str, limit: int = 900) -> str:
    compact = re.sub(r"\s+", " ", (text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rsplit(" ", 1)[0] + "..."


def load_knowledge() -> None:
    """Load all JARVIS_KNOWLEDGE files into memory."""
    global _knowledge_store, _knowledge_loaded

    if _knowledge_loaded:
        return

    knowledge_dir = _get_knowledge_dir()

    knowledge_files = [path.name for path in sorted(knowledge_dir.glob("JARVIS_KNOWLEDGE_*.txt"))]

    loaded_count = 0
    for filename in knowledge_files:
        filepath = knowledge_dir / filename
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8")
                base_name = _derive_base_name(filename)
                _knowledge_store[base_name] = content
                loaded_count += 1
                log.debug(f"Loaded knowledge: {base_name}")
            except Exception as e:
                log.warning(f"Failed to load {filename}: {e}")
        else:
            log.debug(f"Knowledge file not found: {filename}")

    _knowledge_loaded = True
    log.info(f"Loaded {loaded_count} knowledge files")


def get_matching_knowledge(message: str) -> Optional[str]:
    """
    Detect topics in user message and return relevant knowledge.

    Args:
        message: The user's message text

    Returns:
        Formatted knowledge context or None if no match
    """
    if not _knowledge_loaded:
        load_knowledge()

    message_lower = message.lower()

    # Find matching topics
    matched_topics = []
    for keyword, knowledge_key in TOPIC_KEYWORDS.items():
        if keyword in message_lower:
            if knowledge_key not in matched_topics:
                matched_topics.append(knowledge_key)

    if not matched_topics:
        return None

    # Build knowledge context from matched topics
    context_parts = []
    for topic in matched_topics:
        if topic in _knowledge_store:
            content = _knowledge_store[topic]
            # Take first 800 chars of relevant knowledge (avoid too much context)
            snippet = content[:800]
            context_parts.append(f"=== {topic.replace('_', ' ').title()} ===\n{snippet}\n")

    if context_parts:
        return "\n\n".join(context_parts)

    return None


def inject_knowledge_context(message: str, base_prompt: str) -> str:
    """
    Inject relevant knowledge into the system prompt.

    Args:
        message: The user's current message
        base_prompt: The base system prompt

    Returns:
        Enhanced prompt with knowledge context if relevant topics found
    """
    knowledge = get_matching_knowledge(message)

    if not knowledge:
        return base_prompt

    if "YOUR CAPABILITIES" in base_prompt:
        insertion = base_prompt.find("YOUR CAPABILITIES")
        knowledge_block = (
            "SUPPLEMENTAL DOMAIN CONTEXT:\n"
            "Use the following only as optional background when the request clearly overlaps these domains. "
            "Do not answer as if limited to these files, and do not force this context into unrelated conversations.\n\n"
            f"{knowledge}"
        )
        return base_prompt[:insertion] + f"\n\n{knowledge_block}\n\n" + base_prompt[insertion:]

    return base_prompt + "\n\nSUPPLEMENTAL DOMAIN CONTEXT:\n" + knowledge


def get_knowledge_summary() -> dict:
    """Get summary of loaded knowledge for debugging."""
    return {
        "loaded": _knowledge_loaded,
        "topics_count": len(_knowledge_store),
        "available_topics": list(_knowledge_store.keys()),
    }
