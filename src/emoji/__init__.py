"""Emoji domain - Reaction triggered handlers

Handles emoji reaction events:
- :op_cloud:, :op_hub:, :op_creative: → Jira ticket creation
- :op_sre_complete: → mark linked Jira tickets as done
- :op_analyze: → Grafana alert root-cause analysis

Note: Reaction routing is handled by SlackRouter in src/_shared/router.py
"""
