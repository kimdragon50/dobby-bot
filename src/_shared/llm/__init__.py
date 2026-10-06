"""LLM integration module"""
from src._shared.llm.service import LLMService, head_tail, safe_json_parse

__all__ = ["LLMService", "head_tail", "safe_json_parse"]
