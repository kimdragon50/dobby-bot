"""Dobby Bot - Domain-driven Architecture

Domains:
- _shared/: Common modules (config, logger, jira, llm)
- jira_ticket/: Jira ticket creation (modal + reaction)
- weekly_report/: Weekly report generation
"""
from src._shared.config import Config
from src._shared.logger import logger
from src._shared.jira import JiraService
from src._shared.llm import LLMService

__all__ = ['Config', 'logger', 'JiraService', 'LLMService']
