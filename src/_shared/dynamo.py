"""공용 DynamoDB 헬퍼 — IRSA 자격증명으로 실행

Bedrock용 정적 AWS 키(AWS_ACCESS_KEY_ID 등)가 환경에 있으면 boto3 자격증명
체인에서 IRSA web-identity 토큰보다 우선한다. DynamoDB는 pod의 IRSA
ServiceAccount role로 접근해야 하므로, 호출 동안만 해당 env를 제거했다가
복원한다.

boto3는 세션 생성 시점이 아니라 API 호출 시점에 자격증명을 resolve하므로
env 복원 전에 실제 API 호출이 끝나야 한다 — 반드시 run_with_irsa 안에서
호출을 완료할 것.
"""
import os

import boto3

from src._shared.config import Config

# IRSA를 가리는 정적 Bedrock 자격증명 env 키
_BEDROCK_ENV_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def run_with_irsa(table_name: str, fn):
    """fn(table)을 IRSA 자격증명으로 실행하고 env를 복원한다.

    Usage:
        run_with_irsa("my-table", lambda t: t.put_item(Item={...}))
    """
    saved = {k: os.environ.pop(k) for k in _BEDROCK_ENV_KEYS if k in os.environ}
    try:
        session = boto3.Session(region_name=Config.AWS_REGION)
        table = session.resource("dynamodb").Table(table_name)
        return fn(table)
    finally:
        os.environ.update(saved)
