"""Weekly Report command domain

Generates weekly Jira reports via /dobby wkreport command.
"""
from src.command.weekly_report.handler import WeeklyReportHandler

__all__ = ["WeeklyReportHandler"]
