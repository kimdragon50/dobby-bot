"""Unified Jira Service for Dobby Bot

Used by:
- emoji_jira_ticket: simple ticket creation
- command_jira_ticket: advanced ticket creation with modal support
- command_weekly_report: ticket querying and group management
"""
from jira import JIRA, JIRAError
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta, timezone
import requests

from src._shared.config import Config
from src._shared.logger import logger


KST = timezone(timedelta(hours=9))


class JiraService:
    """Unified Jira service for all bot features"""

    _instance: Optional["JiraService"] = None
    _client: Optional[JIRA] = None
    _field_mapping: Optional[Dict] = None
    _session: Optional[requests.Session] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._client is None:
            self._initialize_client()

    def _initialize_client(self):
        """Initialize Jira client and HTTP session"""
        if not Config.JIRA_ENABLED:
            logger.info("Jira is disabled")
            return

        try:
            Config.validate_jira()
            self._client = JIRA(
                server=Config.JIRA_SERVER_URL,
                basic_auth=(Config.JIRA_USERNAME, Config.JIRA_API_TOKEN),
                options={"server": Config.JIRA_SERVER_URL, "verify": True, "rest_api_version": "3"},
            )

            # Initialize HTTP session for connection pooling
            self._session = requests.Session()
            self._session.auth = (Config.JIRA_USERNAME, Config.JIRA_API_TOKEN)
            self._session.headers.update({
                "Accept": "application/json",
                "Content-Type": "application/json"
            })

            logger.info(f"Jira client initialized for {Config.JIRA_SERVER_URL}")
        except Exception as e:
            logger.error(f"Failed to initialize Jira client: {e}")
            raise

    @property
    def session(self) -> requests.Session:
        """Get HTTP session for Jira API calls"""
        if self._session is None:
            raise RuntimeError("Jira session not initialized")
        return self._session

    @property
    def client(self) -> JIRA:
        """Get Jira client instance"""
        if self._client is None:
            raise RuntimeError("Jira client not initialized")
        return self._client

    # ============================================
    # Basic Operations (from portkey)
    # ============================================

    # Label namespaces this service owns. The correct values are assigned from config
    # (initiative/team/role/source/trigger) or derived from priority, so LLM-generated
    # labels in these namespaces are dropped — otherwise the LLM invents entries like
    # "initiative:<이니셔티브 제목 전체>" that pollute the team's grouping labels.
    MANAGED_LABEL_PREFIXES = ("initiative:", "team:", "role:", "source:", "trigger:", "priority:")

    @classmethod
    def _sanitize_llm_labels(cls, raw_labels: List[str]) -> List[str]:
        """Normalize LLM-generated labels and drop any in a managed namespace"""
        labels = []
        for raw in raw_labels or []:
            # Jira labels cannot contain spaces
            label = (raw or "").strip().replace(" ", "-")
            if not label:
                continue
            if label.lower().startswith(cls.MANAGED_LABEL_PREFIXES):
                logger.info(f"Dropped LLM label in managed namespace: {label}")
                continue
            labels.append(label)
        return labels

    async def create_ticket_from_llm(
        self, payload: Dict[str, Any], project_key: str, requester_email: Optional[str] = None,
        parent_key: Optional[str] = None, issue_type: str = "Story",
        requester_name: Optional[str] = None, assignee_account_id: Optional[str] = None,
        story_points: Optional[int] = 1, requester_account_id: Optional[str] = None,
        required_labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create Jira ticket from LLM-processed payload.
        Used by Portkey handler (reaction -> Jira).

        Args:
            payload: LLM-generated Jira payload
            project_key: Jira project key
            requester_email: Optional email to find and mention requester in Jira
            parent_key: Optional parent issue key (e.g., initiative or epic)
            issue_type: Issue type name (default: "Story")
            requester_name: Optional Slack display name to use as fallback for requester
            assignee_account_id: Optional Jira accountId to set as assignee
            required_labels: Labels always applied (defaults to the op initiative set)
        """
        summary = (payload.get("summary") or "").strip()[:255]
        description = payload.get("description") or "No description provided."
        labels = self._sanitize_llm_labels(payload.get("labels"))
        priority = payload.get("priority")
        checklist = payload.get("checklist") or []
        epic_key = payload.get("epic_key")  # LLM-selected epic

        # Add required labels
        if required_labels is None:
            required_labels = ["initiative:op", "role:devops"]
        for req_label in required_labels:
            if req_label not in labels:
                labels.append(req_label)

        # Convert priority to label format
        priority_map = {
            "Highest": "priority:critical",
            "High": "priority:high",
            "Medium": "priority:medium",
            "Low": "priority:low",
        }
        if priority and priority in priority_map:
            priority_label = priority_map[priority]
            if priority_label not in labels:
                labels.append(priority_label)

        # Determine actual parent: LLM-selected epic > parent_key (epic or initiative)
        actual_parent_key = epic_key or parent_key

        # Inject requester as Jira mention in description
        # Priority: pre-resolved requester_account_id > email lookup
        _req_account_id = requester_account_id
        _req_display_name = requester_name or ""
        if not _req_account_id and requester_email:
            jira_user = self.get_user_by_email(requester_email)
            if jira_user and jira_user.get("accountId"):
                _req_account_id = jira_user["accountId"]
                _req_display_name = jira_user.get("displayName") or requester_name or ""
        if _req_account_id and _req_display_name:
            description = self._inject_requester_mention(description, _req_account_id, _req_display_name)
            logger.info(f"Injected requester mention: {_req_display_name} ({_req_account_id})")
        elif requester_name:
            logger.info(f"Using Slack name without mention: {requester_name}")

        # Add checklist to description
        if checklist:
            checklist_md = "\n\n----\nh2. (예상)작업 내용\n\n"
            for idx, item in enumerate(checklist, 1):
                title = item.get("title", "Untitled")
                steps = item.get("steps") or []
                detail = item.get("detail")
                # Work item title as h3 header
                checklist_md += f"h3. 작업{idx}. {title}\n"
                # If steps are provided, use numbered list
                if steps:
                    for step_idx, step in enumerate(steps, 1):
                        checklist_md += f"{step_idx}. {step}\n"
                # Fallback: if detail is provided (legacy format)
                elif detail:
                    lines = [l.strip() for l in detail.split("\n") if l.strip()]
                    if len(lines) > 1:
                        for step_idx, line in enumerate(lines, 1):
                            checklist_md += f"{step_idx}. {line}\n"
                    else:
                        checklist_md += f"1. {detail}\n"
                checklist_md += "\n"
            description += checklist_md

        # Convert description to Atlassian Document Format (ADF) for API v3
        description_adf = self._convert_to_adf(description[:32000])

        default_due_date = (datetime.now(KST) + timedelta(weeks=2)).strftime("%Y-%m-%d")

        fields = {
            "project": {"key": project_key},
            "summary": summary,
            "description": description_adf,
            "issuetype": {"name": issue_type},
            "labels": labels,
            "duedate": default_due_date,
        }

        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}

        # Set parent issue (epic or initiative) if available
        if actual_parent_key:
            fields["parent"] = {"key": actual_parent_key}
            logger.info(f"Setting parent: {actual_parent_key} (epic_key={epic_key}, parent_key={parent_key})")

        if priority:
            try:
                fields["priority"] = {"name": priority}
            except Exception:
                logger.warning("Priority field is not available in this Jira project.")

        # Apply story points via custom field mapping
        # Fallback to common Jira field IDs if mapping lookup fails
        STORY_POINTS_FALLBACK_FIELDS = ["customfield_10016", "customfield_10028"]
        story_points_fields = {}
        if story_points is not None:
            field_mapping = self.get_custom_field_mapping()
            sp_field = field_mapping.get("story_points") or STORY_POINTS_FALLBACK_FIELDS[0]
            story_points_fields[sp_field] = story_points
            logger.info(f"Setting story points: {story_points} (field={sp_field})")

        try:
            logger.info(f"Creating Jira ticket: project={project_key}, summary={summary}")
            issue = self.client.create_issue(fields={**fields, **story_points_fields})
            key = issue.key
            url = f"{Config.JIRA_SERVER_URL}/browse/{key}"
            logger.info(f"Jira ticket created: {key}")
            return {"key": key, "url": url, "summary": summary}

        except JIRAError as e:
            msg = getattr(e, "text", str(e))
            is_required_error = "필수" in msg or "required" in msg.lower()
            is_field_rejected = story_points_fields and ("cannot be set" in msg or "customfield_" in msg)
            is_invalid_parent = "상위 업무" in msg or "parent" in msg.lower() and "유효" in msg

            # Retry without story points if the field is unwritable (not required)
            if is_field_rejected and not is_required_error:
                logger.warning(f"Story points field rejected (not required), retrying without it: {msg}")
                try:
                    issue = self.client.create_issue(fields=fields)
                    key = issue.key
                    url = f"{Config.JIRA_SERVER_URL}/browse/{key}"
                    logger.info(f"Jira ticket created (without story points): {key}")
                    return {"key": key, "url": url, "summary": summary}
                except JIRAError as e2:
                    msg = getattr(e2, "text", str(e2))
                    is_invalid_parent = "상위 업무" in msg or "parent" in msg.lower() and "유효" in msg

            # Retry without parent if the initiative/issue type hierarchy is not allowed
            if is_invalid_parent and actual_parent_key:
                logger.warning(f"Invalid parent '{actual_parent_key}', retrying without parent: {msg}")
                fields_no_parent = {k: v for k, v in fields.items() if k != "parent"}
                try:
                    issue = self.client.create_issue(fields={**fields_no_parent, **story_points_fields})
                    key = issue.key
                    url = f"{Config.JIRA_SERVER_URL}/browse/{key}"
                    logger.info(f"Jira ticket created (without parent): {key}")
                    return {"key": key, "url": url, "summary": summary}
                except JIRAError as e3:
                    msg = getattr(e3, "text", str(e3))

            logger.error(f"Jira API error: {msg}")
            raise RuntimeError(f"Jira API 오류: {msg}")

    async def create_subtask(
        self,
        parent_key: str,
        project_key: str,
        summary: str,
        description: str = "",
        story_points: Optional[int] = None,
        assignee_account_id: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a subtask under a parent issue.

        Tries issue type names in order: Subtask → Sub-task → 하위 작업
        Falls back to Task with parent set if no subtask type is found.
        """
        description_adf = self._convert_to_adf(description) if description else {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": []}]}

        # Subtask base fields — duedate excluded (not on subtask screen in most Jira configs)
        base_fields = {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description_adf,
            "parent": {"key": parent_key},
        }
        if labels:
            base_fields["labels"] = [l.replace(" ", "-") for l in labels]
        if assignee_account_id:
            base_fields["assignee"] = {"accountId": assignee_account_id}

        STORY_POINTS_FALLBACK = "customfield_10016"
        sp_field = None
        if story_points is not None:
            field_mapping = self.get_custom_field_mapping()
            sp_field = field_mapping.get("story_points") or STORY_POINTS_FALLBACK

        candidate_types = ["Subtask", "Sub-task", "하위 작업"]
        last_error = None
        _DUEDATE_ERRORS = ("기한", "due date", "duedate")

        for issue_type_name in candidate_types:
            created_key = None
            created_url = None
            used_sp = False

            # Attempt 1: with SP (if available)
            if sp_field and story_points is not None:
                try:
                    fields = {**base_fields, "issuetype": {"name": issue_type_name}, sp_field: story_points}
                    issue = self.client.create_issue(fields=fields)
                    created_key = issue.key
                    created_url = f"{Config.JIRA_SERVER_URL}/browse/{created_key}"
                    used_sp = True
                except JIRAError as e:
                    msg = getattr(e, "text", str(e))
                    if "issuetype" in msg.lower() or "issue type" in msg.lower() or "이슈 유형" in msg or "유효하지 않은" in msg:
                        last_error = msg
                        continue  # try next issue type
                    # SP or other optional field rejected — fall through to attempt 2
                    logger.warning(f"Subtask with SP failed ({issue_type_name}), retrying without: {msg}")
                    last_error = msg

            # Attempt 2: without SP
            if not created_key:
                try:
                    fields = {**base_fields, "issuetype": {"name": issue_type_name}}
                    issue = self.client.create_issue(fields=fields)
                    created_key = issue.key
                    created_url = f"{Config.JIRA_SERVER_URL}/browse/{created_key}"
                except JIRAError as e:
                    msg = getattr(e, "text", str(e))
                    last_error = msg
                    if "issuetype" in msg.lower() or "issue type" in msg.lower() or "이슈 유형" in msg or "유효하지 않은" in msg:
                        continue  # try next issue type
                    # Attempt 3: duedate required — retry with default duedate (7 days)
                    if any(err in msg.lower() for err in _DUEDATE_ERRORS):
                        duedate = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
                        logger.warning(f"Subtask duedate required, retrying with {duedate}")
                        try:
                            fields["duedate"] = duedate
                            issue = self.client.create_issue(fields=fields)
                            created_key = issue.key
                            created_url = f"{Config.JIRA_SERVER_URL}/browse/{created_key}"
                        except JIRAError as e3:
                            last_error = getattr(e3, "text", str(e3))
                    if not created_key:
                        continue  # try next issue type name

            if created_key:
                # Try setting SP via update if not set during creation
                if not used_sp and sp_field and story_points is not None:
                    try:
                        self.client.issue(created_key).update(fields={sp_field: story_points})
                        logger.info(f"Subtask created: {created_key} (type={issue_type_name}, SP={story_points} via update)")
                    except Exception as sp_e:
                        logger.warning(f"Subtask {created_key} SP update skipped: {sp_e}")
                        logger.info(f"Subtask created: {created_key} (type={issue_type_name}, no SP)")
                else:
                    logger.info(f"Subtask created: {created_key} (type={issue_type_name}, SP={story_points})")
                return {"key": created_key, "url": created_url, "summary": summary}

        raise RuntimeError(f"서브태스크 생성 실패: {last_error}")

    # ============================================
    # Project & Metadata (from slack-to-jira-bot)
    # ============================================

    def get_all_projects(self) -> List[Dict]:
        """Get all accessible projects from Jira"""
        try:
            jira_projects = self.client.projects()
            projects = [
                {
                    "key": project.key,
                    "name": project.name,
                    "id": project.id,
                    "description": getattr(project, "description", ""),
                }
                for project in jira_projects
            ]
            projects.sort(key=lambda x: x["key"])
            return projects
        except Exception as e:
            logger.error(f"Error fetching projects: {e}")
            return []

    def get_epics(self, project_key: str) -> List[Dict]:
        """Get all epics in the project"""
        try:
            jql = f'project = {project_key} AND (issuetype = Epic OR issuetype = 에픽) ORDER BY created DESC'
            epics = self.client.search_issues(jql, maxResults=100)
            return [
                {
                    "id": epic.id,
                    "key": epic.key,
                    "summary": epic.fields.summary,
                    "name": getattr(epic.fields, "customfield_10011", epic.fields.summary),
                }
                for epic in epics
            ]
        except Exception as e:
            logger.error(f"Error fetching epics: {e}")
            return []

    def get_initiatives(self, project_key: str) -> List[Dict]:
        """Get all initiatives in the project"""
        try:
            jql = f'project = {project_key} AND (issuetype = Initiative OR issuetype = 이니셔티브) ORDER BY created DESC'
            initiatives = self.client.search_issues(jql, maxResults=100)
            return [
                {
                    "id": initiative.id,
                    "key": initiative.key,
                    "summary": initiative.fields.summary,
                }
                for initiative in initiatives
            ]
        except Exception as e:
            logger.error(f"Error fetching initiatives: {e}")
            return []

    def get_epics_under_parent(self, parent_key: str, project_key: Optional[str] = None) -> List[Dict]:
        """Get all epics under a specific parent (initiative)

        Args:
            parent_key: Parent issue key (e.g., PROJ-1)
            project_key: Restrict results to this project (see get_epics_under_parents)

        Returns:
            List of epic dicts with key, summary, parent_key
        """
        return self.get_epics_under_parents([parent_key], project_key)

    def get_epics_under_parents(
        self, parent_keys: List[str], project_key: Optional[str] = None
    ) -> List[Dict]:
        """Get all epics under the given parents (initiatives)

        Args:
            parent_keys: Parent issue keys (e.g., the KPI initiatives)
            project_key: Restrict results to this project. An initiative can hold epics
                from other projects (e.g. an OPS-* epic under a PROJ-* initiative), and
                those must not become the parent of a ticket created in project_key.

        Returns:
            List of epic dicts with key, summary, parent_key
        """
        if not parent_keys:
            return []

        try:
            keys = ", ".join(parent_keys)
            project_filter = f" AND project = {project_key}" if project_key else ""
            jql = (
                f'parent in ({keys}){project_filter} '
                f'AND (issuetype = Epic OR issuetype = 에픽) ORDER BY key ASC'
            )
            epics = self.client.search_issues(jql, maxResults=100)
            result = []
            for epic in epics:
                parent = getattr(epic.fields, "parent", None)
                result.append({
                    "key": epic.key,
                    "summary": epic.fields.summary,
                    "parent_key": getattr(parent, "key", None) if parent else None,
                })
            return result
        except Exception as e:
            logger.error(f"Error fetching epics under parents {parent_keys}: {e}")
            return []

    def get_all_labels(self) -> List[str]:
        """Get all global labels from Jira"""
        try:
            url = f"{Config.JIRA_SERVER_URL}/rest/api/3/label"
            timeout = Config.get_http_timeout()

            all_labels = []
            start_at = 0
            max_results = 1000

            while True:
                params = {"startAt": start_at, "maxResults": max_results}
                response = self.session.get(url, params=params, timeout=timeout)

                if response.status_code != 200:
                    break

                data = response.json()
                values = data.get("values", [])

                if not values:
                    break

                for label_obj in values:
                    if isinstance(label_obj, str):
                        label = label_obj
                    elif isinstance(label_obj, dict):
                        label = label_obj.get("label", "")
                    else:
                        continue
                    if label:
                        all_labels.append(label)

                if len(values) < max_results:
                    break

                start_at += max_results
                if start_at >= 10000:
                    break

            valid_labels = [
                label for label in all_labels
                if label and not all(c in ["-", "_"] for c in label)
            ]
            return sorted(valid_labels, key=lambda x: x.lower())

        except Exception as e:
            logger.error(f"Error fetching labels: {e}")
            return []

    def get_assignable_users(self, project_key: str) -> List[Dict]:
        """Get all users that can be assigned to issues in the project"""
        try:
            users = self.client.search_assignable_users_for_projects("", project_key, maxResults=100)
            return [
                {
                    "account_id": user.accountId,
                    "display_name": user.displayName,
                    "email": getattr(user, "emailAddress", ""),
                    "active": user.active,
                }
                for user in users
            ]
        except Exception as e:
            logger.error(f"Error fetching users: {e}")
            return []

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """Find Jira user by email address

        Returns:
            Dict with accountId and displayName, or None if not found
        """
        if not email:
            return None

        try:
            url = f"{Config.JIRA_SERVER_URL}/rest/api/3/user/search"
            timeout = Config.get_http_timeout()
            params = {"query": email}

            response = self.session.get(url, params=params, timeout=timeout)
            if response.status_code != 200:
                logger.warning(f"Failed to search user by email: {response.status_code}")
                return None

            users = response.json()
            # Find exact email match
            for user in users:
                if user.get("emailAddress", "").lower() == email.lower():
                    return {
                        "accountId": user.get("accountId"),
                        "displayName": user.get("displayName"),
                        "emailAddress": user.get("emailAddress"),
                    }

            # If no exact match, return first result if available
            if users:
                return {
                    "accountId": users[0].get("accountId"),
                    "displayName": users[0].get("displayName"),
                    "emailAddress": users[0].get("emailAddress"),
                }

            return None
        except Exception as e:
            logger.error(f"Error searching user by email: {e}")
            return None

    def get_sprints(self, project_key: str) -> List[Dict]:
        """Get all sprints for the project"""
        try:
            url = f"{Config.JIRA_SERVER_URL}/rest/agile/1.0/board"
            timeout = Config.get_http_timeout()

            params = {"projectKeyOrId": project_key}
            response = self.session.get(url, params=params, timeout=timeout)

            if response.status_code != 200:
                return []

            boards = response.json().get("values", [])
            if not boards:
                return []

            board_id = boards[0]["id"]
            sprint_url = f"{Config.JIRA_SERVER_URL}/rest/agile/1.0/board/{board_id}/sprint"
            sprint_response = self.session.get(sprint_url, timeout=timeout)

            if sprint_response.status_code != 200:
                return []

            sprints = sprint_response.json().get("values", [])
            sprint_list = [
                {"id": sprint.get("id"), "name": sprint.get("name"), "state": sprint.get("state")}
                for sprint in sprints
            ]

            state_order = {"active": 0, "future": 1, "closed": 2}
            sprint_list.sort(key=lambda s: (state_order.get(s["state"], 3), s["name"]))
            return sprint_list

        except Exception as e:
            logger.error(f"Error fetching sprints: {e}")
            return []

    def move_issue_to_sprint(self, issue_key: str, sprint_id: int) -> bool:
        """Move an issue to a sprint using Agile API

        Args:
            issue_key: The issue key (e.g., 'PROJ-123')
            sprint_id: The sprint ID

        Returns:
            True if successful, False otherwise
        """
        try:
            url = f"{Config.JIRA_SERVER_URL}/rest/agile/1.0/sprint/{sprint_id}/issue"
            timeout = Config.get_http_timeout()

            payload = {"issues": [issue_key]}
            response = self.session.post(url, json=payload, timeout=timeout)

            if response.status_code in [200, 204]:
                logger.info(f"Issue {issue_key} moved to sprint {sprint_id}")
                return True
            else:
                logger.warning(f"Failed to move issue to sprint: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error moving issue to sprint: {e}")
            return False

    def search_issues_by_slack_thread(self, channel_id: str, root_ts: str, project_key: str = "SRE") -> List[Dict]:
        """Search Jira for issues whose description contains the Slack thread URL"""
        try:
            ts_no_dot = root_ts.replace('.', '')
            search_term = f"archives/{channel_id}/p{ts_no_dot}"
            jql = f'project = {project_key} AND description ~ "{search_term}" ORDER BY created DESC'
            logger.info(f"[Jira] Searching issues by thread: {jql}")
            issues = self.client.search_issues(jql, maxResults=20)
            return [
                {
                    "key": i.key,
                    "summary": i.fields.summary,
                    "status": i.fields.status.name if i.fields.status else "",
                    "url": f"{Config.JIRA_SERVER_URL}/browse/{i.key}",
                }
                for i in issues
            ]
        except Exception as e:
            logger.error(f"[Jira] Failed to search issues by Slack thread: {e}")
            return []

    # Jira Cloud 는 한 페이지에 최대 100건만 준다. 그 이상은 nextPageToken 으로 이어 받는다.
    _JQL_PAGE_SIZE = 100
    # customfield_10020 = "스프린트" (greenhopper gh-sprint). 이슈는 이월되면서
    # 스프린트를 여러 개 갖는다 — 가장 최근 것만 추리는 건 호출부 몫.
    _SPRINT_FIELD = "customfield_10020"
    _JQL_FIELDS = (
        "summary,status,assignee,issuetype,parent,duedate,priority," + _SPRINT_FIELD
    )

    def _iter_jql_pages(self, jql: str, max_results: int, fields: str = None, expand: str = None):
        """JQL 결과를 끝까지(또는 max_results 까지) 이어 받는다.

        구 `search_issues` 는 Jira Cloud 에서 폐기됐다. 페이지당 100건만 주면서
        전체 건수(total)를 주지 않아, 라이브러리가 그 자리에 페이지 크기를
        채워 넣는다 — 249건짜리 JQL 이 "100건"으로 보이던 원인.
        maxResults 를 100 넘게 줘도 조용히 잘리므로, 100 초과를 기대하는
        조회는 모두 이 헬퍼를 거쳐야 한다.

        Returns:
            (issues, leftover_token) — leftover_token 이 있으면 max_results 에서
            끊긴 것이므로 더 남아 있다는 뜻.
        """
        issues, token = [], None
        while len(issues) < max_results:
            want = min(self._JQL_PAGE_SIZE, max_results - len(issues))
            kwargs = {
                "maxResults": want,
                "fields": fields or self._JQL_FIELDS,
                "json_result": False,
            }
            if expand:
                kwargs["expand"] = expand
            if token:
                kwargs["nextPageToken"] = token
            page = self.client.enhanced_search_issues(jql, **kwargs)
            issues.extend(page)
            token = getattr(page, "nextPageToken", None)
            if not token or not len(page):
                break
        return issues, token

    def _sprints_of(self, fields) -> List[Dict]:
        """이슈에 붙은 스프린트를 평평한 dict 로 뽑는다.

        이월된 이슈는 여러 스프린트에 속한다 (닫힌 것 + 현재 것).
        """
        out = []
        for s in (getattr(fields, self._SPRINT_FIELD, None) or []):
            sid = getattr(s, "id", None)
            if sid is None:
                continue
            out.append({
                "id": int(sid),
                "name": getattr(s, "name", "") or "",
                "state": (getattr(s, "state", "") or "").lower(),
                "start": (getattr(s, "startDate", "") or "")[:10],
                "end": (getattr(s, "endDate", "") or "")[:10],
            })
        return out

    def search_issues_by_jql(self, jql: str, max_results: int = 300):
        """임의의 JQL 로 이슈를 조회한다 (담당자별 현황 공유 등).

        데일리 스크럼 티켓 현황처럼 운영자가 JQL 을 직접 정하는 용도.
        담당자/상태/상위 이슈까지 함께 반환해 그룹핑에 쓸 수 있게 한다.

        Returns:
            (issues, total) — total 은 JQL 전체 건수. max_results 에서 끊긴
            경우 가져온 수보다 크다. 이 값이 없으면 "전체 100건" 처럼 잘린
            수치를 전체인 양 표시하게 된다.
        """
        try:
            logger.info(f"[Jira] JQL search: {jql[:160]}")
            raw, leftover_token = self._iter_jql_pages(jql, max_results)
            result = []
            for i in raw:
                f = i.fields
                assignee = getattr(f, "assignee", None)
                parent = getattr(f, "parent", None)
                result.append({
                    "key": i.key,
                    "summary": getattr(f, "summary", "") or "",
                    "status": f.status.name if getattr(f, "status", None) else "",
                    "status_category": (
                        getattr(f.status.statusCategory, "key", "")
                        if getattr(f, "status", None)
                        and getattr(f.status, "statusCategory", None)
                        else ""
                    ),
                    "issue_type": f.issuetype.name if getattr(f, "issuetype", None) else "",
                    # 미할당 티켓도 드러나야 하므로 빈 값을 그대로 둔다
                    "assignee": getattr(assignee, "displayName", "") if assignee else "",
                    "assignee_email": getattr(assignee, "emailAddress", "") if assignee else "",
                    "parent_key": getattr(parent, "key", "") if parent else "",
                    "parent_summary": (
                        getattr(parent.fields, "summary", "") if parent and getattr(parent, "fields", None) else ""
                    ),
                    "due_date": getattr(f, "duedate", "") or "",
                    "sprints": self._sprints_of(f),
                    "url": f"{Config.JIRA_SERVER_URL}/browse/{i.key}",
                })
            # 상한에 걸려 남은 페이지가 있을 때만 전체 건수를 따로 묻는다
            total = len(result)
            if leftover_token:
                try:
                    total = int(self.client.approximate_issue_count(jql))
                except Exception as e:
                    logger.warning(f"[Jira] 전체 건수 조회 실패, 가져온 수로 대체: {e}")
            logger.info(f"[Jira] JQL search result: {len(result)} issues (total={total})")
            return result, total
        except Exception as e:
            logger.error(f"[Jira] JQL 조회 실패: {e}")
            return [], 0

    def get_subtasks(self, issue_key: str) -> List[Dict]:
        """Get subtasks of a Jira issue"""
        try:
            issue = self.client.issue(issue_key, fields="subtasks,summary,status")
            result = []
            for sub in (issue.fields.subtasks or []):
                result.append({
                    "key": sub.key,
                    "summary": sub.fields.summary,
                    "status": sub.fields.status.name if sub.fields.status else "",
                    "url": f"{Config.JIRA_SERVER_URL}/browse/{sub.key}",
                })
            return result
        except Exception as e:
            logger.error(f"[Jira] Failed to get subtasks for {issue_key}: {e}")
            return []

    def transition_issue_to_done(self, issue_key: str) -> bool:
        """Transition a Jira issue to Done/완료 status"""
        try:
            transitions = self.client.transitions(issue_key)
            done_keywords = ["완료", "done", "closed", "close", "resolve", "resolved"]
            for t in transitions:
                if any(kw in t["name"].lower() for kw in done_keywords):
                    self.client.transition_issue(issue_key, t["id"])
                    logger.info(f"[Jira] {issue_key} transitioned to '{t['name']}' (id={t['id']})")
                    return True
            logger.warning(f"[Jira] No done transition found for {issue_key}. Available: {[t['name'] for t in transitions]}")
            return False
        except Exception as e:
            logger.error(f"[Jira] Failed to transition {issue_key} to done: {e}")
            return False

    def get_issue_types(self, project_key: str) -> List[Dict]:
        """Get available issue types for the project"""
        try:
            meta = self.client.createmeta(projectKeys=project_key, expand="projects.issuetypes")

            type_list = []
            seen_names = set()

            if meta and "projects" in meta and len(meta["projects"]) > 0:
                project_meta = meta["projects"][0]
                issue_types = project_meta.get("issuetypes", [])

                for issue_type in issue_types:
                    name = issue_type.get("name", "")
                    is_epic = "Epic" in name or "에픽" in name
                    is_subtask = issue_type.get("subtask", False) or "Subtask" in name or "하위" in name

                    if name in seen_names:
                        continue

                    if not is_epic and not is_subtask:
                        type_list.append({
                            "id": issue_type.get("id", ""),
                            "name": name,
                            "description": issue_type.get("description", ""),
                        })
                        seen_names.add(name)

            return type_list
        except Exception as e:
            logger.error(f"Error fetching issue types: {e}")
            return []

    def get_custom_field_mapping(self) -> Dict:
        """Get custom field IDs for epic link, start date, story points, and sprint"""
        if self._field_mapping is not None:
            return self._field_mapping

        try:
            fields = self.client.fields()
            field_mapping = {}

            # Priority pass: Look for "story point estimate" or Korean equivalent
            STORY_POINTS_KEYWORDS = [
                "story point estimate", "point estimate",
                "story points", "스토리 포인트",
            ]
            for field in fields:
                name_lower = field["name"].lower()
                field_id = field["id"]

                if any(kw in name_lower for kw in STORY_POINTS_KEYWORDS):
                    if "story_points" not in field_mapping:
                        field_mapping["story_points"] = field_id
                        break

            # Second pass: Find other fields
            for field in fields:
                name_lower = field["name"].lower()
                field_id = field["id"]

                if any(keyword in name_lower for keyword in ["epic link", "parent", "epic", "에픽"]):
                    if "epic_link" not in field_mapping:
                        field_mapping["epic_link"] = field_id
                elif "start date" in name_lower or "시작" in name_lower:
                    if "start_date" not in field_mapping:
                        field_mapping["start_date"] = field_id
                elif "sprint" in name_lower or "스프린트" in name_lower:
                    if "sprint" not in field_mapping:
                        field_mapping["sprint"] = field_id
                elif "story_points" not in field_mapping and (
                    "story point" in name_lower or "스토리" in name_lower
                ):
                    field_mapping["story_points"] = field_id

            self._field_mapping = field_mapping
            return field_mapping
        except Exception as e:
            logger.error(f"Error fetching custom fields: {e}")
            self._field_mapping = {}
            return {}

    def _inject_requester_mention(self, description: str, account_id: str, display_name: str) -> str:
        """Inject Jira mention marker into the 요청자 section of description

        Uses a special marker format that will be converted to ADF mention in _convert_to_adf:
        {{mention:accountId:displayName}}
        """
        import re

        # Pattern to find 요청자 section and replace its content
        # Matches "h2. 요청자" followed by any content until next "h2." or "----" or end
        # Uses non-greedy match to capture content between sections
        pattern = r"(h2\.\s*요청자\s*\n)(.*?)(?=\nh2\.|\n----|\Z)"

        def replace_requester(match):
            header = match.group(1)
            # Replace content with mention marker
            return f"{header}* {{{{mention:{account_id}:{display_name}}}}}"

        result = re.sub(pattern, replace_requester, description, flags=re.MULTILINE | re.DOTALL)

        # Check if replacement was made
        if result == description:
            logger.warning(f"Could not find 요청자 section to inject mention for {display_name}")

        return result

    def _is_valid_url_for_description(self, url: str) -> bool:
        """Validate URL for inclusion in Jira description

        Filters out:
        1. Malformed Slack URLs (timestamp too short)
        2. Jira URLs (likely hallucinated by LLM)

        Valid Slack URLs have format:
        https://slack.com/archives/{channel_id}/p{timestamp}
        https://{workspace}.slack.com/archives/{channel_id}/p{timestamp}

        Timestamp should be 13-16 digits (Unix timestamp in microseconds without dot)
        """
        import re

        # Filter out Jira URLs (likely hallucinated by LLM)
        jira_pattern = r"https://[\w-]+\.atlassian\.net/browse/[A-Z]+-\d+"
        if re.match(jira_pattern, url):
            logger.warning(f"Filtering out Jira URL (likely hallucinated): {url}")
            return False

        # Check if it's a Slack archives URL
        slack_pattern = r"https://(?:[\w-]+\.)?slack\.com/archives/[A-Z0-9]+/p(\d+)"
        match = re.match(slack_pattern, url)

        if match:
            timestamp = match.group(1)
            # Valid Slack timestamps are 13-16 digits (microseconds)
            if len(timestamp) < 10:
                logger.warning(f"Invalid Slack URL (malformed timestamp): {url}")
                return False

        return True

    def _parse_inline_content(self, text: str) -> List[Dict]:
        """Parse inline content including mentions and URLs

        Converts:
        - {{mention:accountId:displayName}} markers to ADF mention nodes
        - URLs (http/https) to ADF link nodes (with validation for Slack URLs)
        """
        import re

        mention_pattern = r"\{\{mention:([^:]+):([^}]*)\}\}"
        bold_pattern = r"\*\*(.+?)\*\*"
        url_pattern = r"(https?://[^\s<>\[\]]+)"

        matches = []

        for match in re.finditer(mention_pattern, text):
            matches.append({
                "start": match.start(),
                "end": match.end(),
                "type": "mention",
                "account_id": match.group(1),
                "display_name": match.group(2)
            })

        for match in re.finditer(bold_pattern, text, re.DOTALL):
            is_inside = any(m["start"] <= match.start() < m["end"] for m in matches)
            if not is_inside:
                matches.append({
                    "start": match.start(),
                    "end": match.end(),
                    "type": "bold",
                    "text": match.group(1)
                })

        for match in re.finditer(url_pattern, text):
            url = match.group(1)
            is_inside_mention = any(
                m["start"] <= match.start() < m["end"]
                for m in matches if m["type"] == "mention"
            )
            # Skip if inside mention or if it's an invalid/hallucinated URL
            if is_inside_mention:
                continue
            if not self._is_valid_url_for_description(url):
                continue
            matches.append({
                "start": match.start(),
                "end": match.end(),
                "type": "url",
                "url": url
            })

        matches.sort(key=lambda x: x["start"])

        content = []
        last_end = 0

        for match in matches:
            if match["start"] > last_end:
                before_text = text[last_end:match["start"]]
                if before_text:
                    content.append({"type": "text", "text": before_text})

            if match["type"] == "mention":
                content.append({
                    "type": "mention",
                    "attrs": {
                        "id": match["account_id"],
                        "text": f"@{match['display_name']}",
                        "accessLevel": ""
                    }
                })
            elif match["type"] == "bold":
                content.append({
                    "type": "text",
                    "text": match["text"],
                    "marks": [{"type": "strong"}]
                })
            elif match["type"] == "url":
                content.append({
                    "type": "text",
                    "text": match["url"],
                    "marks": [{"type": "link", "attrs": {"href": match["url"]}}]
                })

            last_end = match["end"]

        if last_end < len(text):
            remaining = text[last_end:]
            if remaining:
                content.append({"type": "text", "text": remaining})

        if not content and text:
            content.append({"type": "text", "text": text})

        return content

    def _convert_to_adf(self, text: str) -> Dict:
        """Convert Markdown/Jira Wiki Markup to Atlassian Document Format (ADF)

        Supports:
        - Jira Wiki Markup: h1., h2., h3., h4., h5., h6. (headings), ---- (horizontal rule)
        - Markdown: ##, ###, etc. (headings)
        - Both: * (bullet list items), # (numbered list items)
        - Markdown: 1., 2., etc. (numbered list items)
        - Mentions: {{mention:accountId:displayName}} markers
        - URLs: converted to hyperlinks
        - Jira code blocks: {code:lang}...{code}, {noformat}...{noformat}
        """
        import re

        # Pre-process: clean up nested asterisks (e.g., "* * text" -> "* text")
        text = re.sub(r"^\*\s+\*\s+", "* ", text, flags=re.MULTILINE)
        # Pre-process: strip orphan leading ** (no closing pair on the same line, e.g. "** SomeName")
        text = re.sub(r"^\*\*\s+(?![^\n]*\*\*)", "", text, flags=re.MULTILINE)

        # Pre-process: extract {code:lang}...{code} and {noformat}...{noformat} blocks
        # Replace them with placeholders to preserve multi-line content during line-by-line parsing
        _code_blocks = []

        def _store_code_block(lang, content):
            placeholder = f"__CODEBLOCK_{len(_code_blocks)}__"
            _code_blocks.append({"lang": lang.strip(), "content": content})
            return placeholder

        # {code:lang}...{code} and {code}...{code}
        text = re.sub(
            r'\{code(?::([^}]*))?\}(.*?)\{code\}',
            lambda m: _store_code_block(m.group(1) or "", m.group(2)),
            text, flags=re.DOTALL | re.IGNORECASE
        )
        # {noformat}...{noformat}
        text = re.sub(
            r'\{noformat\}(.*?)\{noformat\}',
            lambda m: _store_code_block("", m.group(1)),
            text, flags=re.DOTALL | re.IGNORECASE
        )

        lines = text.split("\n")
        content = []
        current_bullet_items = []
        current_numbered_items = []

        def is_bullet_line(s):
            return s.startswith("*") and not s.startswith("**")

        def is_numbered_line(s):
            return (s.startswith("#") and not s.startswith("##")) or re.match(r"^\d+\.\s+", s)

        for line in lines:
            stripped = line.strip()

            # Flush bullet list if current line is not a bullet
            if current_bullet_items and not is_bullet_line(stripped):
                content.append({"type": "bulletList", "content": current_bullet_items})
                current_bullet_items = []

            # Flush numbered list if current line is not a numbered item
            if current_numbered_items and not is_numbered_line(stripped):
                content.append({"type": "orderedList", "content": current_numbered_items})
                current_numbered_items = []

            if not stripped:
                continue

            # Code block placeholder → ADF codeBlock node
            _cb_match = re.match(r'^__CODEBLOCK_(\d+)__$', stripped)
            if _cb_match:
                cb_idx = int(_cb_match.group(1))
                cb = _code_blocks[cb_idx]
                code_node = {
                    "type": "codeBlock",
                    "attrs": {"language": cb["lang"]} if cb["lang"] else {},
                    "content": [{"type": "text", "text": cb["content"].strip("\n")}],
                }
                content.append(code_node)
                continue

            # Jira Wiki Markup: horizontal rule (----)
            if stripped == "----":
                content.append({"type": "rule"})
            # Jira Wiki Markup: h1. ~ h6. headings
            elif re.match(r"^h([1-6])\.\s*", stripped):
                match = re.match(r"^h([1-6])\.\s*(.*)", stripped)
                level = int(match.group(1))
                heading_text = match.group(2).strip()
                content.append({
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": [{"type": "text", "text": heading_text}] if heading_text else [],
                })
            # Markdown: ## headings
            elif stripped.startswith("##"):
                heading_text = stripped.lstrip("#").strip()
                content.append({
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": heading_text}],
                })
            # Numbered list items (Jira Wiki Markup: #)
            elif stripped.startswith("#") and not stripped.startswith("##"):
                numbered_text = stripped[1:].strip()
                inline_content = self._parse_inline_content(numbered_text)
                current_numbered_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": inline_content}],
                })
            # Numbered list items (Markdown: 1., 2., etc.)
            elif re.match(r"^\d+\.\s+", stripped):
                numbered_text = re.sub(r"^\d+\.\s+", "", stripped)
                inline_content = self._parse_inline_content(numbered_text)
                current_numbered_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": inline_content}],
                })
            # Bullet list items
            elif is_bullet_line(stripped):
                bullet_text = stripped[1:].strip()
                inline_content = self._parse_inline_content(bullet_text)
                current_bullet_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": inline_content}],
                })
            # Regular paragraph
            else:
                # Parse inline content for mentions
                inline_content = self._parse_inline_content(stripped)
                content.append({"type": "paragraph", "content": inline_content})

        if current_bullet_items:
            content.append({"type": "bulletList", "content": current_bullet_items})

        if current_numbered_items:
            content.append({"type": "orderedList", "content": current_numbered_items})

        if not content:
            content = [{"type": "paragraph", "content": []}]

        return {"type": "doc", "version": 1, "content": content}

    def _extract_text_from_adf(self, adf) -> str:
        """Extract plain text from Atlassian Document Format (ADF)"""
        try:
            # Handle PropertyHolder or dict-like objects
            if hasattr(adf, 'content'):
                content = adf.content
            elif isinstance(adf, dict):
                content = adf.get("content", [])
            else:
                return str(adf)

            texts = []
            for node in content:
                texts.append(self._extract_text_from_node(node))
            return "\n".join(filter(None, texts))
        except Exception:
            return ""

    def _extract_text_from_node(self, node) -> str:
        """Recursively extract text from ADF node"""
        try:
            # Handle PropertyHolder objects
            if hasattr(node, 'type'):
                node_type = node.type
                node_content = getattr(node, 'content', []) or []
                node_text = getattr(node, 'text', None)
            elif isinstance(node, dict):
                node_type = node.get("type", "")
                node_content = node.get("content", []) or []
                node_text = node.get("text")
            else:
                return ""

            if node_type == "text" and node_text:
                return node_text

            if node_content:
                texts = [self._extract_text_from_node(child) for child in node_content]
                return " ".join(filter(None, texts))

            return ""
        except Exception:
            return ""

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Story",
        epic_key: Optional[str] = None,
        labels: Optional[List[str]] = None,
        assignee_id: Optional[str] = None,
        due_date: Optional[str] = None,
        start_date: Optional[str] = None,
        story_points: Optional[int] = None,
        sprint_id: Optional[int] = None,
    ) -> Dict:
        """Create a new Jira issue with full options (for Modal handler)

        Note: Initiative is used only for UI filtering. Task/Story parent is always Epic.
        """
        field_mapping = self.get_custom_field_mapping()
        description_adf = self._convert_to_adf(description)

        issue_dict = {
            "project": {"key": project_key},
            "summary": summary,
            "description": description_adf,
            "issuetype": {"name": issue_type},
        }

        if labels:
            issue_dict["labels"] = [label.replace(" ", "-") for label in labels]

        if assignee_id:
            issue_dict["assignee"] = {"accountId": assignee_id}

        if due_date:
            issue_dict["duedate"] = due_date

        parent_fields = {}
        start_date_fields = {}
        story_points_fields = {}

        # Set Epic as parent for Task/Story/Bug
        # Use 'parent' field for Next-gen (Team-managed) projects
        # Note: customfield epic_link is for Classic projects only, not used here
        if epic_key:
            logger.info(f"[JiraService] Setting epic {epic_key} as parent using 'parent' field")
            parent_fields["parent"] = {"key": epic_key}

        if start_date and "start_date" in field_mapping:
            start_date_fields[field_mapping["start_date"]] = start_date

        if story_points and "story_points" in field_mapping:
            story_points_fields[field_mapping["story_points"]] = story_points

        def _finalize_result(result: Dict, issue_key: str) -> Dict:
            """Apply sprint assignment after issue creation"""
            if sprint_id:
                sprint_success = self.move_issue_to_sprint(issue_key, sprint_id)
                if not sprint_success:
                    result["warning"] = result.get("warning", "") + " Sprint could not be set."
            return result

        try:
            full_issue_dict = {**issue_dict, **parent_fields, **start_date_fields, **story_points_fields}
            new_issue = self.client.create_issue(fields=full_issue_dict)

            result = {
                "success": True,
                "key": new_issue.key,
                "id": new_issue.id,
                "url": f"{Config.JIRA_SERVER_URL}/browse/{new_issue.key}",
            }
            return _finalize_result(result, new_issue.key)
        except Exception as e:
            error_str = str(e)
            if "cannot be set" in error_str and "customfield_" in error_str:
                try:
                    retry_dict = {**issue_dict, **parent_fields, **start_date_fields}
                    new_issue = self.client.create_issue(fields=retry_dict)
                    result = {
                        "success": True,
                        "key": new_issue.key,
                        "id": new_issue.id,
                        "url": f"{Config.JIRA_SERVER_URL}/browse/{new_issue.key}",
                        "warning": "Story points field could not be set.",
                    }
                    return _finalize_result(result, new_issue.key)
                except Exception as e2:
                    try:
                        new_issue = self.client.create_issue(fields=issue_dict)
                        result = {
                            "success": True,
                            "key": new_issue.key,
                            "id": new_issue.id,
                            "url": f"{Config.JIRA_SERVER_URL}/browse/{new_issue.key}",
                            "warning": "Custom fields could not be set.",
                        }
                        return _finalize_result(result, new_issue.key)
                    except Exception as e3:
                        logger.error(f"Error creating issue: {e3}")
                        return {"success": False, "error": str(e3)}
            else:
                logger.error(f"Error creating issue: {e}")
                return {"success": False, "error": str(e)}

    # ============================================
    # Group & Report Operations (from weekly-report-bot)
    # ============================================

    def get_groups(self, prefix: str = "") -> List[Dict]:
        """Get Jira groups with optional prefix filter"""
        try:
            url = f"{Config.JIRA_SERVER_URL}/rest/api/3/groups/picker"
            timeout = Config.get_http_timeout()
            params = {"query": prefix} if prefix else {}

            response = self.session.get(url, params=params, timeout=timeout)
            if response.status_code != 200:
                logger.error(f"Failed to fetch groups: {response.status_code}")
                return []

            groups = response.json().get("groups", [])
            return [{"name": g.get("name", ""), "groupId": g.get("groupId", "")} for g in groups]
        except Exception as e:
            logger.error(f"Error fetching groups: {e}")
            return []

    def get_group_members(self, group_name: str) -> List[Dict]:
        """Get members of a Jira group"""
        try:
            url = f"{Config.JIRA_SERVER_URL}/rest/api/3/group/member"
            timeout = Config.get_http_timeout()
            params = {"groupname": group_name, "maxResults": 200}

            response = self.session.get(url, params=params, timeout=timeout)
            if response.status_code != 200:
                logger.error(f"Failed to fetch group members: {response.status_code}")
                return []

            members = response.json().get("values", [])
            return [
                {
                    "accountId": m.get("accountId", ""),
                    "displayName": m.get("displayName", ""),
                    "emailAddress": m.get("emailAddress", ""),
                    "identifier": m.get("emailAddress") or m.get("accountId", ""),
                }
                for m in members
            ]
        except Exception as e:
            logger.error(f"Error fetching group members: {e}")
            return []

    def _get_epic_name(self, epic_key: str) -> Optional[str]:
        """Get epic name/summary by key"""
        try:
            epic = self.client.issue(epic_key, fields="summary")
            return epic.fields.summary
        except Exception:
            return None

    def get_weekly_tickets(
        self, users: List[str], start_date: datetime, end_date: datetime
    ) -> List[Dict]:
        """Get tickets assigned to users within date range (excludes completed tickets)"""
        try:
            user_jql = " OR ".join([f'assignee = "{user}"' for user in users])
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")

            # Exclude completed/done status tickets
            jql = f'({user_jql}) AND (created >= "{start_str}" OR updated >= "{start_str}") AND (created <= "{end_str}" OR updated <= "{end_str}") AND status NOT IN (Done, 완료, Closed, Resolved) ORDER BY updated DESC'

            # 100건 넘는 주도 있으므로 페이지를 이어 받는다 (구 API 는 조용히 100건에서 잘렸다)
            # expand="changelog" 를 쓰고 있었으나 읽는 곳이 없어 응답만 키웠다 — 제거
            issues, _ = self._iter_jql_pages(jql, 200, fields="*all")

            tickets = []
            for issue in issues:
                epic_key = None
                epic_name = None
                parent_key = None
                is_subtask = False

                # Check if this is a subtask
                if hasattr(issue.fields, "issuetype") and issue.fields.issuetype:
                    is_subtask = getattr(issue.fields.issuetype, "subtask", False)

                initiative_key = None
                initiative_name = None

                # Try to get Epic and Initiative from parent hierarchy
                if hasattr(issue.fields, "parent") and issue.fields.parent:
                    parent = issue.fields.parent
                    parent_key = parent.key
                    parent_type = getattr(parent.fields.issuetype, "name", "") if hasattr(parent.fields, "issuetype") else ""

                    if parent_type in ["Epic", "에픽"]:
                        # Direct parent is Epic
                        epic_key = parent.key
                        epic_name = parent.fields.summary
                        # Check if Epic's parent is Initiative
                        if hasattr(parent.fields, "parent") and parent.fields.parent:
                            epic_parent = parent.fields.parent
                            epic_parent_type = getattr(epic_parent.fields.issuetype, "name", "") if hasattr(epic_parent.fields, "issuetype") else ""
                            if epic_parent_type in ["Initiative", "이니셔티브"]:
                                initiative_key = epic_parent.key
                                initiative_name = epic_parent.fields.summary
                    elif parent_type not in ["Initiative", "이니셔티브"]:
                        # Parent is Story/Task - try to get Epic from parent's parent
                        if hasattr(parent.fields, "parent") and parent.fields.parent:
                            grandparent = parent.fields.parent
                            grandparent_type = getattr(grandparent.fields.issuetype, "name", "") if hasattr(grandparent.fields, "issuetype") else ""
                            if grandparent_type in ["Epic", "에픽"]:
                                epic_key = grandparent.key
                                epic_name = grandparent.fields.summary
                                # Check if Epic's parent is Initiative
                                if hasattr(grandparent.fields, "parent") and grandparent.fields.parent:
                                    epic_parent = grandparent.fields.parent
                                    epic_parent_type = getattr(epic_parent.fields.issuetype, "name", "") if hasattr(epic_parent.fields, "issuetype") else ""
                                    if epic_parent_type in ["Initiative", "이니셔티브"]:
                                        initiative_key = epic_parent.key
                                        initiative_name = epic_parent.fields.summary

                # Try custom field for classic projects (Epic Link)
                if not epic_key:
                    epic_link = getattr(issue.fields, "customfield_10014", None)
                    if epic_link:
                        epic_key = epic_link
                        # Try to fetch epic name
                        if not epic_name:
                            epic_name = self._get_epic_name(epic_link)

                # Handle description - may be ADF object or string
                desc = issue.fields.description
                if desc is None:
                    description_text = ""
                elif isinstance(desc, str):
                    description_text = desc
                else:
                    # ADF format - extract text from content
                    description_text = self._extract_text_from_adf(desc)

                # Get resolution date for completed subtasks
                resolution_date = None
                if hasattr(issue.fields, "resolutiondate") and issue.fields.resolutiondate:
                    resolution_date = issue.fields.resolutiondate[:10]  # YYYY-MM-DD

                tickets.append({
                    "key": issue.key,
                    "summary": issue.fields.summary,
                    "status": issue.fields.status.name if issue.fields.status else "",
                    "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "",
                    "description": description_text,
                    "priority": issue.fields.priority.name if issue.fields.priority else "",
                    "labels": issue.fields.labels or [],
                    "epic_key": epic_key,
                    "epic_name": epic_name,
                    "initiative_key": initiative_key,
                    "initiative_name": initiative_name,
                    "created": str(issue.fields.created),
                    "updated": str(issue.fields.updated),
                    "is_subtask": is_subtask,
                    "parent_key": parent_key,
                    "resolution_date": resolution_date,
                    "duedate": getattr(issue.fields, "duedate", None),
                })

            # Attach subtasks to their parent tickets
            tickets = self._attach_subtasks_to_parents(tickets)

            return tickets
        except Exception as e:
            logger.error(f"Error fetching weekly tickets: {e}")
            return []

    def _attach_subtasks_to_parents(self, tickets: List[Dict]) -> List[Dict]:
        """Attach subtasks to their parent tickets and filter out standalone subtasks"""
        # Create a map of ticket key -> ticket
        ticket_map = {t["key"]: t for t in tickets}

        # Separate subtasks and parent tickets
        subtasks = [t for t in tickets if t["is_subtask"]]
        parent_tickets = [t for t in tickets if not t["is_subtask"]]

        # Initialize subtasks list for each parent
        for ticket in parent_tickets:
            ticket["subtasks"] = []

        # Find orphan subtasks (subtasks whose parents are not in the result set)
        orphan_parent_keys = set()
        for subtask in subtasks:
            parent_key = subtask.get("parent_key")
            if parent_key and parent_key not in ticket_map:
                orphan_parent_keys.add(parent_key)

        # Fetch parent tickets for orphan subtasks
        if orphan_parent_keys:
            orphan_parents = self._fetch_parent_tickets(list(orphan_parent_keys))
            for parent in orphan_parents:
                parent["subtasks"] = []
                ticket_map[parent["key"]] = parent
                parent_tickets.append(parent)

        # Attach subtasks to their parents
        for subtask in subtasks:
            parent_key = subtask.get("parent_key")
            if parent_key and parent_key in ticket_map:
                parent = ticket_map[parent_key]
                if "subtasks" not in parent:
                    parent["subtasks"] = []
                parent["subtasks"].append({
                    "key": subtask["key"],
                    "summary": subtask["summary"],
                    "status": subtask["status"],
                    "resolution_date": subtask.get("resolution_date"),
                    "description": subtask.get("description", ""),
                })

        return parent_tickets

    def _fetch_parent_tickets(self, parent_keys: List[str]) -> List[Dict]:
        """Fetch parent ticket information for orphan subtasks"""
        if not parent_keys:
            return []

        try:
            # Build JQL to fetch parent tickets
            keys_jql = ", ".join([f'"{key}"' for key in parent_keys])
            jql = f"key in ({keys_jql})"

            issues = self.client.search_issues(jql, maxResults=100)

            parents = []
            for issue in issues:
                epic_key = None
                epic_name = None
                initiative_key = None
                initiative_name = None

                # Try to get Epic and Initiative from parent hierarchy
                if hasattr(issue.fields, "parent") and issue.fields.parent:
                    parent = issue.fields.parent
                    parent_type = getattr(parent.fields.issuetype, "name", "") if hasattr(parent.fields, "issuetype") else ""

                    if parent_type in ["Epic", "에픽"]:
                        epic_key = parent.key
                        epic_name = parent.fields.summary
                        # Check if Epic's parent is Initiative
                        if hasattr(parent.fields, "parent") and parent.fields.parent:
                            epic_parent = parent.fields.parent
                            epic_parent_type = getattr(epic_parent.fields.issuetype, "name", "") if hasattr(epic_parent.fields, "issuetype") else ""
                            if epic_parent_type in ["Initiative", "이니셔티브"]:
                                initiative_key = epic_parent.key
                                initiative_name = epic_parent.fields.summary
                    elif parent_type not in ["Initiative", "이니셔티브"]:
                        # Try grandparent
                        if hasattr(parent.fields, "parent") and parent.fields.parent:
                            grandparent = parent.fields.parent
                            grandparent_type = getattr(grandparent.fields.issuetype, "name", "") if hasattr(grandparent.fields, "issuetype") else ""
                            if grandparent_type in ["Epic", "에픽"]:
                                epic_key = grandparent.key
                                epic_name = grandparent.fields.summary
                                # Check if Epic's parent is Initiative
                                if hasattr(grandparent.fields, "parent") and grandparent.fields.parent:
                                    epic_parent = grandparent.fields.parent
                                    epic_parent_type = getattr(epic_parent.fields.issuetype, "name", "") if hasattr(epic_parent.fields, "issuetype") else ""
                                    if epic_parent_type in ["Initiative", "이니셔티브"]:
                                        initiative_key = epic_parent.key
                                        initiative_name = epic_parent.fields.summary

                # Try custom field for classic projects
                if not epic_key:
                    epic_link = getattr(issue.fields, "customfield_10014", None)
                    if epic_link:
                        epic_key = epic_link
                        if not epic_name:
                            epic_name = self._get_epic_name(epic_link)

                # Handle description
                desc = issue.fields.description
                if desc is None:
                    description_text = ""
                elif isinstance(desc, str):
                    description_text = desc
                else:
                    description_text = self._extract_text_from_adf(desc)

                parents.append({
                    "key": issue.key,
                    "summary": issue.fields.summary,
                    "status": issue.fields.status.name if issue.fields.status else "",
                    "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "",
                    "description": description_text,
                    "priority": issue.fields.priority.name if issue.fields.priority else "",
                    "labels": issue.fields.labels or [],
                    "epic_key": epic_key,
                    "epic_name": epic_name,
                    "initiative_key": initiative_key,
                    "initiative_name": initiative_name,
                    "created": str(issue.fields.created),
                    "updated": str(issue.fields.updated),
                    "is_subtask": False,
                    "parent_key": None,
                    "resolution_date": None,
                })

            return parents
        except Exception as e:
            logger.error(f"Error fetching parent tickets: {e}")
            return []

    def group_tickets_by_epic(self, tickets: List[Dict]) -> Dict[str, List[Dict]]:
        """Group tickets by epic"""
        grouped = {}
        for ticket in tickets:
            epic_name = ticket.get("epic_name") or ticket.get("epic_key") or "기타 (Epic 없음)"
            if epic_name not in grouped:
                grouped[epic_name] = []
            grouped[epic_name].append(ticket)
        return grouped

    def group_tickets_by_initiative_and_epic(self, tickets: List[Dict]) -> Dict[str, Dict[str, List[Dict]]]:
        """Group tickets by initiative, then by epic within each initiative

        Returns:
            Dict structure: {initiative_name: {epic_name: [tickets]}}
            Tickets without initiative are grouped under "기타 (Initiative 없음)"
            Tickets without epic are grouped under "기타 (Epic 없음)"
        """
        grouped = {}
        for ticket in tickets:
            initiative_name = ticket.get("initiative_name") or ticket.get("initiative_key") or "기타 (Initiative 없음)"
            epic_name = ticket.get("epic_name") or ticket.get("epic_key") or "기타 (Epic 없음)"

            if initiative_name not in grouped:
                grouped[initiative_name] = {}
            if epic_name not in grouped[initiative_name]:
                grouped[initiative_name][epic_name] = []
            grouped[initiative_name][epic_name].append(ticket)

        return grouped

    def add_labels_to_tickets(self, label_mapping: Dict[str, List[str]]) -> Dict[str, Any]:
        """Add labels to multiple tickets based on classification

        Args:
            label_mapping: Dict mapping label names to list of ticket keys
                e.g., {"report:lead": ["KAN-123"], "report:team": ["KAN-456"]}

        Returns:
            Dict with success/failure counts and details
        """
        results = {"success": 0, "failed": 0, "details": []}

        for label, ticket_keys in label_mapping.items():
            for ticket_key in ticket_keys:
                try:
                    issue = self.client.issue(ticket_key)
                    current_labels = issue.fields.labels or []

                    # Skip if label already exists
                    if label in current_labels:
                        logger.debug(f"Label '{label}' already exists on {ticket_key}")
                        results["success"] += 1
                        continue

                    # Add new label
                    new_labels = current_labels + [label]
                    issue.update(fields={"labels": new_labels})

                    logger.info(f"Added label '{label}' to {ticket_key}")
                    results["success"] += 1
                    results["details"].append({"key": ticket_key, "label": label, "status": "added"})

                except Exception as e:
                    logger.error(f"Failed to add label '{label}' to {ticket_key}: {e}")
                    results["failed"] += 1
                    results["details"].append({"key": ticket_key, "label": label, "status": "failed", "error": str(e)})

        return results
