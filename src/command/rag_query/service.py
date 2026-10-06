"""RAG Service using AWS Bedrock Knowledge Base"""
import re
from urllib.parse import unquote
import boto3
from botocore.config import Config as BotoConfig

from src._shared.config import Config
from src._shared.logger import logger
from src.command.rag_query.prompts import RAG_SYSTEM_PROMPT


class RAGService:
    """AWS Bedrock Knowledge Base를 사용한 RAG 서비스"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        retry_config = BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=60,
            read_timeout=60,
        )

        session = boto3.Session(
            aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            region_name=Config.AWS_REGION,
        )

        self.client = session.client(
            "bedrock-agent-runtime",
            config=retry_config,
        )

        self.knowledge_base_id = Config.KNOWLEDGE_BASE_ID
        self.model_arn = f"arn:aws:bedrock:{Config.AWS_REGION}::foundation-model/{Config.BEDROCK_MODEL_ID}"
        logger.info(f"RAG Service initialized: KB={self.knowledge_base_id}")

    def query(self, question: str) -> str:
        """Knowledge Base에 질문하고 응답을 반환 (하위 호환성)"""
        answer, sources = self.query_with_sources(question)
        if sources:
            answer += "\n\n*참고 문서:*\n" + "\n".join(
                f"• <{url}|{title}>" for title, url in sources
            )
        return answer

    def query_with_sources(self, question: str) -> tuple:
        """Knowledge Base에 질문하고 (응답, 소스목록) 튜플 반환"""
        try:
            # 1. 먼저 retrieve로 관련 문서 검색
            retrieve_response = self.client.retrieve(
                knowledgeBaseId=self.knowledge_base_id,
                retrievalQuery={"text": question},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {
                        "numberOfResults": 5,
                    }
                },
            )

            # 소스 추출
            results = retrieve_response.get("retrievalResults", [])
            logger.info(f"Retrieved {len(results)} results")
            sources = self._extract_sources_from_results(results)

            # 2. retrieve_and_generate로 답변 생성
            response = self.client.retrieve_and_generate(
                input={"text": question},
                retrieveAndGenerateConfiguration={
                    "type": "KNOWLEDGE_BASE",
                    "knowledgeBaseConfiguration": {
                        "knowledgeBaseId": self.knowledge_base_id,
                        "modelArn": self.model_arn,
                        "generationConfiguration": {
                            "inferenceConfig": {
                                "textInferenceConfig": {
                                    "temperature": 0.3,
                                    "maxTokens": 2048,
                                }
                            },
                            "promptTemplate": {
                                "textPromptTemplate": RAG_SYSTEM_PROMPT
                            },
                        },
                    },
                },
            )

            output = response.get("output", {})
            answer = output.get("text", "관련 문서를 찾지 못했습니다. 다른 키워드로 검색해보세요.")

            # 답변에서 언급된 문서 중 sources에 없는 것 찾아서 추가
            mentioned_docs = self._extract_mentioned_docs(answer)
            existing_titles = {title.lower() for title, _ in sources}

            for doc_name in mentioned_docs:
                if doc_name.lower() in existing_titles:
                    continue
                if any(doc_name.lower() in title.lower() for title, _ in sources):
                    continue

                doc_info = self._search_document(doc_name)
                if doc_info:
                    sources.append((f"📄 {doc_info['title']}", doc_info['url']))
                    logger.info(f"Added mentioned doc to sources: {doc_name}")

            return answer, sources

        except Exception as e:
            logger.error(f"RAG query failed: {e}")
            raise

    def _extract_sources_from_results(self, results: list) -> list:
        """retrieve API 결과에서 소스 추출"""
        sources = []
        folders = []
        seen_files = set()
        seen_folders = set()

        for result in results:
            location = result.get("location", {})
            loc_type = location.get("type", "")

            url = None
            title = None

            # SharePoint
            if loc_type == "SHARE_POINT" or "sharePointLocation" in location:
                sp_loc = location.get("sharePointLocation", {})
                url = sp_loc.get("url", "")
            # S3
            elif loc_type == "S3" or "s3Location" in location:
                s3_loc = location.get("s3Location", {})
                url = s3_loc.get("uri", "")
            # Web
            elif loc_type == "WEB" or "webLocation" in location:
                web_loc = location.get("webLocation", {})
                url = web_loc.get("url", "")
            # Confluence
            elif loc_type == "CONFLUENCE" or "confluenceLocation" in location:
                conf_loc = location.get("confluenceLocation", {})
                url = conf_loc.get("url", "")

            if url:
                decoded_url = unquote(url)
                title = decoded_url.split("/")[-1].split("?")[0]

                # 폴더 URL 추출
                folder_url = "/".join(url.split("/")[:-1])
                if folder_url and folder_url not in seen_folders:
                    seen_folders.add(folder_url)
                    folder_name = unquote(folder_url.split("/")[-1])
                    folders.append((f"📁 {folder_name}", folder_url))

                if url not in seen_files:
                    seen_files.add(url)
                    logger.info(f"Found source: {title} -> {url[:100]}")
                    sources.append((f"📄 {title}", url))

        return folders[:2] + sources[:5]

    def _extract_mentioned_docs(self, answer: str) -> list:
        """답변 텍스트에서 언급된 문서명 추출"""
        mentioned = []

        extensions = r'\.(?:xlsx|xls|docx|doc|pdf|pptx|ppt|hwp|txt|csv)'

        patterns = [
            rf'"([^"]+{extensions})"',
            rf"'([^']+{extensions})'",
            rf'([\w가-힣][\w가-힣_\-\.\(\)]*{extensions})',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, answer, re.IGNORECASE):
                filename = match.group(1).strip()
                if filename and len(filename) > 5:
                    mentioned.append(filename)

        seen = set()
        unique = []
        for doc in mentioned:
            doc_lower = doc.lower()
            if doc_lower not in seen:
                seen.add(doc_lower)
                unique.append(doc)

        if unique:
            logger.info(f"Extracted mentioned docs from answer: {unique}")
        return unique

    def _search_document(self, doc_name: str) -> dict | None:
        """Knowledge Base에서 문서 검색하여 URL 찾기"""
        try:
            logger.info(f"Searching for document: {doc_name}")
            response = self.client.retrieve(
                knowledgeBaseId=self.knowledge_base_id,
                retrievalQuery={"text": doc_name},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {
                        "numberOfResults": 10
                    }
                }
            )

            results = response.get("retrievalResults", [])
            candidates = []

            for result in results:
                location = result.get("location", {})
                loc_type = location.get("type", "")
                metadata = result.get("metadata", {})

                url = None
                source_type = "OTHER"

                if loc_type == "SHARE_POINT" or "sharePointLocation" in location:
                    url = location.get("sharePointLocation", {}).get("url", "")
                    source_type = "SHAREPOINT"
                elif loc_type == "CONFLUENCE" or "confluenceLocation" in location:
                    url = location.get("confluenceLocation", {}).get("url", "")
                    source_type = "CONFLUENCE"
                elif loc_type == "WEB" or "webLocation" in location:
                    url = location.get("webLocation", {}).get("url", "")
                    source_type = "WEB"
                elif loc_type == "S3" or "s3Location" in location:
                    url = location.get("s3Location", {}).get("uri", "")
                    source_type = "S3"

                if not url:
                    url = metadata.get("x-amz-bedrock-kb-source-uri", "")
                    if "sharepoint.com" in url:
                        source_type = "SHAREPOINT"
                    elif "atlassian.net" in url or "confluence" in url.lower():
                        source_type = "CONFLUENCE"

                if url:
                    decoded_url = unquote(url)
                    title = decoded_url.split("/")[-1].split("?")[0]
                    score = self._calc_similarity_score(doc_name, title, url)
                    if score > 0:
                        candidates.append((score, {
                            "title": title,
                            "url": url,
                            "type": source_type,
                        }))
                        logger.debug(f"Candidate: {title} (score={score})")

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                best = candidates[0]
                logger.info(f"Found matching doc: {doc_name} -> {best[1]['title']} (score={best[0]})")
                return best[1]

            logger.debug(f"No matching document found for: {doc_name}")
            return None

        except Exception as e:
            logger.error(f"Error searching document '{doc_name}': {e}")
            return None

    def _calc_similarity_score(self, search_term: str, title: str, url: str) -> int:
        """검색어와 문서의 유사도 점수 계산"""
        score = 0
        search_lower = search_term.lower()
        title_lower = title.lower()
        url_lower = unquote(unquote(url.lower()))

        search_base = re.sub(r'\.(xlsx|xlsm|xls|docx|doc|pdf|pptx|ppt|hwp|txt|csv)$', '', search_lower, flags=re.IGNORECASE)
        title_base = re.sub(r'\.(xlsx|xlsm|xls|docx|doc|pdf|pptx|ppt|hwp|txt|csv)$', '', title_lower, flags=re.IGNORECASE)

        if search_base == title_base:
            return 100

        if search_base in title_base:
            score += 80
        elif title_base in search_base:
            score += 70

        search_for_url = search_base.replace(" ", "_").replace("-", "_")
        if search_for_url in url_lower or search_base in url_lower:
            score += 50

        search_keywords = set(re.split(r'[_\-\s\.]+', search_base))
        title_keywords = set(re.split(r'[_\-\s\.]+', title_base))

        search_keywords = {k for k in search_keywords if not k.isdigit() and len(k) > 1}
        title_keywords = {k for k in title_keywords if not k.isdigit() and len(k) > 1}

        common = search_keywords & title_keywords
        if common:
            score += len(common) * 10
            long_common = {k for k in common if len(k) >= 4}
            score += len(long_common) * 5

        if score > 0 and len(common) == 0:
            score = 0

        return score

    def _extract_sources(self, citations: list) -> list:
        """인용 정보에서 소스 추출 (SharePoint, S3, Web 등 지원) - 레거시 호환"""
        sources = []
        folders = []
        seen_files = set()
        seen_folders = set()

        for citation in citations:
            for ref in citation.get("retrievedReferences", []):
                location = ref.get("location", {})

                url = None
                title = None

                # SharePoint 소스
                if "sharePointLocation" in location:
                    sp_loc = location["sharePointLocation"]
                    url = sp_loc.get("url", "")
                    if url:
                        decoded_url = unquote(url)
                        title = decoded_url.split("/")[-1].split("?")[0]

                        folder_url = "/".join(url.split("/")[:-1])
                        if folder_url and folder_url not in seen_folders:
                            seen_folders.add(folder_url)
                            folder_name = unquote(folder_url.split("/")[-1])
                            folders.append((f"📁 {folder_name}", folder_url))

                # Web 소스
                elif "webLocation" in location:
                    web_loc = location["webLocation"]
                    url = web_loc.get("url", "")
                    title = url.split("/")[-1] if url else "Web Document"

                # S3 소스
                elif "s3Location" in location:
                    s3_loc = location["s3Location"]
                    url = s3_loc.get("uri", "")
                    title = url.split("/")[-1] if url else "S3 Document"

                # Confluence 소스
                elif "confluenceLocation" in location:
                    conf_loc = location["confluenceLocation"]
                    url = conf_loc.get("url", "")
                    title = url.split("/")[-1] if url else "Confluence Document"

                # 기타 - metadata에서 sourceUri 확인
                if not url:
                    metadata = ref.get("metadata", {})
                    url = metadata.get("x-amz-bedrock-kb-source-uri", "")
                    title = url.split("/")[-1] if url else "Document"

                if url and url not in seen_files:
                    seen_files.add(url)
                    logger.info(f"Found source: {title} -> {url}")
                    sources.append((f"📄 {title}", url))

        return folders[:2] + sources[:5]
