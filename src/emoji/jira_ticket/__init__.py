"""Jira Ticket Emoji domain - Reaction-based Jira ticket creation

This domain handles automatic Jira ticket creation triggered by emoji reactions.
When a configured emoji is added to a message, the thread content is analyzed by LLM
and a Jira ticket is created in the mapped project.
"""
from src.emoji.jira_ticket.handler import ReactionHandler, ReactionConfig

__all__ = ["ReactionHandler", "ReactionConfig"]
