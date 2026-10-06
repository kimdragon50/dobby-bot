# Dobby - 업무 자동화 Slack 봇

> **EN:** A Slack Bolt bot (Socket Mode) that automates DevOps team chores: LLM-generated Jira tickets from Slack threads, Grafana alert root-cause analysis (kubectl / Prometheus / Loki / CloudWatch + AWS Bedrock), RAG Q&A on a Bedrock Knowledge Base, daily scrum and weekly report automation, with Langfuse feedback tracing.

> **Note**
> This repository is a personal portfolio version of an internal tool I designed and built. Company-specific configuration, internal infrastructure details and business modules have been removed.

Dobby는 DevOps 팀의 반복 업무를 Slack 안에서 끝내기 위해 만든 봇입니다.
스레드에 이모지 하나를 달면 대화 내용을 LLM이 정리해 Jira 티켓을 만들고, Grafana 알림 메시지에 이모지를 달면 해당 Pod의 상태·로그·메트릭을 수집해 원인 분석 리포트를 스레드에 남깁니다. 그 밖에 사내 문서 RAG 질의, 데일리 스크럼, 주간 리포트, 일정 브리핑 같은 팀 루틴을 자동화합니다.

설계와 구현은 혼자 진행했고, 개발 과정에서 Claude Code / Codex 같은 AI 코딩 도구를 적극적으로 활용했습니다 (AI-assisted, "vibe coding").

---

## 기능

### 슬래시 명령어 (`/dobby <subcommand>`)

| 명령 | 설명 |
| --- | --- |
| `jira` | Jira 티켓 생성 모달. 스레드 내용을 LLM이 분석해 제목·설명·체크리스트 초안을 채우고, 프로젝트/에픽/담당자를 골라 생성 (메시지 shortcut으로도 실행) |
| `wkreport` | 주간 리포트. Jira 그룹을 선택하면 기간 내 업데이트된 티켓을 Initiative > Epic 단위로 묶어 LLM이 경영진용/팀용 보고서를 작성하고, 분류 결과를 티켓 라벨로 기록 |
| `ask` / `askpv` | Bedrock Knowledge Base 기반 RAG 질의 (`askpv`는 본인에게만 보이는 응답). 답변과 함께 출처 문서 링크 첨부 |
| `scrum` | 데일리 스크럼. `on/off/list`로 자동 게시 채널 관리(DynamoDB), `now/test`로 즉시 게시. 작성 모달 → 스레드 정리 → 버튼 한 번으로 LLM 종합, 담당자별 Jira 티켓 현황(현재/직전 스프린트) 첨부 |
| `briefing` | Google Calendar 일정 브리핑 등록. 서비스 계정으로 캘린더를 읽어 LLM이 요약한 브리핑을 DM으로 발송 |
| `food` / `bob` | 점심 메뉴 추천. 위치·분위기·예산 입력 + 오늘 날씨(wttr.in)를 반영해 LLM이 추천, 네이버 검색/지도 링크 첨부 |

### 이모지 리액션

| 이모지 | 설명 |
| --- | --- |
| `:op_cloud:` 등 (설정 가능) | 스레드 → LLM 분석 → Jira 태스크 + 서브태스크 생성. 리액션별로 프로젝트/상위 이슈를 `config.yaml`에서 매핑. 스레드에 `#KPI` 키워드가 있으면 KPI 이니셔티브 하위 에픽 중 가장 적합한 것을 LLM이 선택 |
| `:op_sre_complete:` | 스레드에 연결된 Jira 티켓을 찾아 Done으로 전환 |
| `:op_analyze:` | Grafana 알림 메시지 원인 분석 (아래 [알림 원인 분석 흐름](#알림-원인-분석-흐름-op_analyze) 참고) |
| 👍 / 👎 | 봇 답변 피드백을 Langfuse score로 기록. 👎이면 사유를 되물어 코멘트로 함께 저장 |

### 멘션 / DM

- `@dobby 질문` 또는 DM → **Bedrock AgentCore Runtime**의 supervisor 에이전트로 전달하고, SSE 스트림 응답을 Slack mrkdwn으로 변환해 답글로 게시
- 세션 ID를 `slack-{channel}-{thread_ts}`로 고정해 같은 스레드에서는 대화 문맥이 유지되고, Langfuse 트레이스와 피드백 score가 같은 세션으로 묶임

### 스케줄러

- **데일리 스크럼**: APScheduler cron (기본 평일 09:00 KST, env로 변경)
- **일정 브리핑**: 등록 사용자별 일일 브리핑 DM
- 여러 Pod로 띄워도 중복 실행되지 않도록 **DynamoDB 조건부 쓰기 기반 분산 락**(TTL lease)으로 리더 선출. 락 저장소 장애 시에는 알림 누락보다 중복이 낫다고 보고 fail-open

---

## 아키텍처

```
                       ┌──────────────────────────┐
  Slack Workspace ───▶ │  Socket Mode (WebSocket) │
                       └────────────┬─────────────┘
                                    ▼
                       ┌──────────────────────────┐
                       │  Slack Bolt App (app.py) │   /health (readiness)
                       └────────────┬─────────────┘
                                    ▼
                       ┌──────────────────────────┐
                       │       SlackRouter        │  /dobby <sub> · reaction_added
                       │  (dedup · channel filter)│  · app_mention / DM
                       └──┬─────────┬─────────┬───┘
            ┌─────────────┘         │         └──────────────┐
            ▼                       ▼                        ▼
   command/ handlers        emoji/ handlers           mention/ · feedback/
   jira · wkreport · ask    jira_ticket · complete    AgentCore supervisor
   scrum · briefing · food  alert_analyze             Langfuse score
            │                       │                        │
  ┌─────────┼───────────┬───────────┼──────────────┬─────────┘
  ▼         ▼           ▼           ▼              ▼
 Jira   Bedrock LLM   RAG        Observability     Langfuse
 (REST) (LangChain    (Bedrock   collectors        (tracing /
         ChatBedrock)  Knowledge  ├ kubectl (k8s)   feedback)
                       Base)      ├ Prometheus
                                  ├ Loki
                                  └ CloudWatch
                                        │
                                        ▼
                                 Confluence report

 State: DynamoDB (scrum 채널 · 브리핑 사용자 · 피드백 대기 · 스케줄러 분산 락)
```

### 설계 포인트

- **단일 라우터**: 슬래시 명령은 `/dobby` 하나로 받아 서브커맨드로 분기하고, `reaction_added`도 하나의 핸들러에서 이모지 이름으로 분기합니다. 기능 추가 = 핸들러 작성 + `register_command`/`register_reaction` 한 줄.
- **Slack 3초 제한 대응**: 모든 무거운 작업(LLM, 외부 API)은 `ack()` 후 백그라운드 스레드에서 처리하고, "분석 중..." 메시지를 먼저 올린 뒤 결과로 교체합니다.
- **이벤트 중복 제거**: Socket Mode 재전송에 대비해 `reaction:channel:event_ts` 키로 60초 TTL dedup.
- **LLM 호출 안정성**: Bedrock throttling 등에 지수 백오프 재시도, 긴 입력은 head/tail 보존 방식으로 자르기, LLM이 깨진 JSON을 반환하면 코드블록 추출 → 중괄호 블록 추출 → 제어문자 이스케이프 순으로 복구.
- **환각 방지 장치**: Jira 티켓 생성 시 LLM이 고른 에픽 키가 실제 후보 목록에 있을 때만 parent로 사용, 프롬프트에 URL/티켓 키를 지어내지 말라는 규칙 명시.
- **자격증명 분리**: Bedrock은 정적 키, DynamoDB는 Pod의 IRSA role로 접근. boto3 자격증명 체인에서 정적 키가 IRSA를 가리는 문제를 호출 구간에만 env를 잠시 비우는 헬퍼(`run_with_irsa`)로 해결.
- **관측성은 선택 사항**: Langfuse·Prometheus·Loki·Confluence 등은 env가 비어 있으면 해당 기능만 조용히 건너뛰고 봇은 정상 동작합니다.

---

## 알림 원인 분석 흐름 (`:op_analyze:`)

Grafana 알림이 Slack에 올라오면, 담당자가 이모지 하나로 1차 진단을 받을 수 있게 만든 기능입니다.

```
:op_analyze: 리액션
   │
   ├─ 1. 원본 메시지 조회 (봇이 채널에 없으면 자동 join 후 재시도)
   │     Grafana 메시지는 attachments/blocks에 내용이 있어 텍스트를 별도 추출,
   │     replies API가 attachments를 누락하면 history API로 재조회
   │
   ├─ 2. Grafana 알림 파싱 → AlertInstance(cluster, namespace, pod, alertname, summary...)
   │     메시지당 최대 3개 인스턴스만 분석 (지연 시간 제어)
   │
   ├─ 3. 스레드에 "분석 중..." 메시지 게시
   │
   ├─ 4. 인스턴스별 진단 데이터 수집
   │     ├ kubectl   : describe pod + pod events(최근 15개) + logs(이전 컨테이너 우선, tail 100)
   │     │             kubeconfig에서 cluster 이름과 일치하는 context 선택, 없으면 in-cluster
   │     ├ Loki      : kubectl 로그가 비면(Pod가 이미 사라진 경우) 최근 30분 로그 조회
   │     ├ Prometheus: CPU / 메모리(working set·limit) / 재시작 횟수 / OOMKilled 여부 (최근 30분)
   │     │             cluster 라벨로 먼저 조회하고 결과가 없으면 라벨 없이 재조회
   │     └ CloudWatch: Prometheus 데이터가 없을 때 Container Insights 메트릭으로 대체
   │
   ├─ 5. Bedrock LLM 진단 → SEVERITY / CAUSE / ACTIONS 고정 포맷으로 출력
   │
   ├─ 6. Confluence에 상세 리포트 페이지 생성 (진단 결과 + 원시 수집 데이터)
   │
   └─ 7. "분석 중..." 메시지를 결과 블록으로 교체
         심각도 이모지 · 원인 · 권장 조치 · [Confluence 링크] · [Jira 티켓 생성] 버튼
              │
              └─ 버튼 클릭 시 진단 내용과 리포트 링크를 담아 Jira 티켓 생성
```

각 수집기는 실패해도 예외를 던지지 않고 빈 결과를 돌려주기 때문에, 일부 데이터 소스가 없어도 가능한 정보만으로 진단을 계속합니다.

---

## 기술 스택

| 영역 | 사용 기술 |
| --- | --- |
| Language / Runtime | Python 3.11, Docker (multi-stage, non-root) |
| Slack | slack-bolt (Socket Mode), Block Kit 모달 / 버튼 / shortcut |
| LLM | AWS Bedrock (Claude), LangChain `ChatBedrock` |
| Agent | Bedrock AgentCore Runtime (supervisor 에이전트 호출, SSE 스트리밍) |
| RAG | Bedrock Knowledge Base (`retrieve`, `retrieve_and_generate`) |
| 이슈 관리 | Jira REST API (`jira` 라이브러리 + Agile API), Confluence REST API |
| 관측 데이터 | Kubernetes Python client, Prometheus HTTP API, Loki HTTP API, CloudWatch |
| LLM Observability | Langfuse (사용자 피드백 score) |
| 상태 / 스케줄 | DynamoDB (상태 저장 · 분산 락), APScheduler |
| 기타 | Google Calendar API, Naver Search API, wttr.in, loguru, pytest |

---

## 로컬 실행

### 1. 사전 준비

- Python 3.11+
- Socket Mode가 켜진 Slack App (Bot Token `xoxb-`, App Token `xapp-`)
  - 슬래시 명령 `/dobby`, 이벤트 `reaction_added` · `app_mention` · `message.im`, 메시지 shortcut `create_jira_ticket`
- Bedrock 모델 접근 권한이 있는 AWS 자격증명
- (선택) Jira / Confluence API 토큰, Bedrock Knowledge Base, Langfuse, Prometheus / Loki 엔드포인트, DynamoDB 테이블

### 2. 설치 및 실행

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 값 채우기
python app.py
```

Docker:

```bash
docker build -t dobby .
docker run --env-file .env dobby
```

### 3. 주요 환경 변수

전체 목록과 설명은 [`.env.example`](.env.example)에 있습니다.

| 변수 | 필수 | 설명 |
| --- | --- | --- |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` | ✅ | Slack 앱 자격증명 |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BEDROCK_MODEL_ID` | ✅ | Bedrock LLM |
| `JIRA_SERVER_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` | Jira 기능 사용 시 | `https://your-domain.atlassian.net` |
| `KNOWLEDGE_BASE_ID` | | 설정 시 `ask`/`askpv` 활성화 |
| `AGENTCORE_SUPERVISOR_ARN` | | 설정 시 멘션/DM 응답 활성화 |
| `PROMETHEUS_URL`, `LOKI_URL`, `KUBECONFIG_PATH` | | 알림 분석 수집기 엔드포인트 |
| `CONFLUENCE_SRE_SPACE_KEY`, `CONFLUENCE_ALERT_PARENT_PAGE_ID` | | 알림 분석 리포트 저장 위치 |
| `OPS_JIRA_PROJECT_KEY` | | 알림 티켓 / 완료 처리 대상 프로젝트 (기본 `SRE`) |
| `JIRA_GROUP_PREFIX` | | 주간 리포트에 노출할 Jira 그룹 접두사 (기본 `team`) |
| `SCRUM_*` | | 스크럼 스케줄 · 티켓 현황 JQL |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | | 피드백 기록 (3개 모두 있어야 활성화) |

이모지 → Jira 프로젝트 매핑, 허용 채널, KPI 이니셔티브는 `src/emoji/jira_ticket/config.yaml`에서 설정합니다.

### 4. 테스트

```bash
pytest -q
```

---

## 프로젝트 구조

```
.
├── app.py                         # 엔트리포인트: 서비스 초기화, 핸들러 등록, Socket Mode 시작
├── Dockerfile
├── requirements.txt
├── .env.example
├── src/
│   ├── _shared/                   # 공용 모듈
│   │   ├── config.py              # env 기반 설정
│   │   ├── router.py              # /dobby 서브커맨드 · 리액션 라우팅, 도움말
│   │   ├── jira/service.py        # Jira / Agile API 래퍼
│   │   ├── llm/service.py         # Bedrock(ChatBedrock) 호출, 재시도, JSON 복구
│   │   ├── langfuse_client.py     # Langfuse score 클라이언트 (no-op fallback)
│   │   ├── dynamo.py              # IRSA 자격증명 DynamoDB 헬퍼
│   │   ├── lock.py                # DynamoDB 분산 락 (스케줄러 리더 선출)
│   │   ├── health.py              # /health HTTP 서버
│   │   └── logger.py
│   ├── command/                   # 슬래시 명령
│   │   ├── jira_ticket/           # 티켓 생성 모달
│   │   ├── weekly_report/         # 주간 리포트
│   │   ├── rag_query/             # Knowledge Base 질의
│   │   ├── daily_scrum/           # 스크럼 알림 · 모달 · 종합 · 스프린트 티켓 현황
│   │   ├── calendar_briefing/     # 일정 브리핑
│   │   └── food_recommendation/   # 메뉴 추천
│   ├── emoji/                     # 이모지 리액션
│   │   ├── jira_ticket/           # 스레드 → Jira (config.yaml)
│   │   ├── complete_ticket/       # 연결 티켓 완료 처리
│   │   └── alert_analyze/         # Grafana 알림 원인 분석
│   │       ├── parsers/           #   Grafana 메시지 파서
│   │       ├── collectors/        #   kubectl · Prometheus · Loki · CloudWatch
│   │       └── reporters/         #   Confluence 리포트
│   ├── mention/                   # @dobby 멘션 / DM → AgentCore
│   └── feedback/                  # 👍/👎 → Langfuse score, 👎 사유 수집
└── tests/
```

---

## License

[MIT](LICENSE)
