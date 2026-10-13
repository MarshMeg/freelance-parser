import logging
import re
from datetime import tzinfo

import httpx
from github import Auth, Github
from github.Repository import Repository

from config import config
from models import GitHubIssuePayload, Job, JobAnalysis
from utils import format_money

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
MARKER_RE = re.compile(r"<!-- job-id: (\S+) -->")

PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  %s(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
      }
    }
  }
}"""

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""

SET_STATUS = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}"""

SOURCE_TITLES = {
    "kwork": "Kwork", "freelance_ru": "Freelance.ru", "freelancer": "Freelancer",
    "upwork": "Upwork", "fiverr": "Fiverr",
}


def _clip(text: str, limit: int = 3000) -> str:
    text = text.strip()
    return text[:limit] + "…" if len(text) > limit else text


def build_payload(job: Job, analysis: JobAnalysis, tz: tzinfo | None = None) -> GitHubIssuePayload:
    """Собрать заголовок, тело и labels Issue по заказу и результату анализа"""
    source = SOURCE_TITLES.get(job.source, job.source)
    title = f"[{source}] {analysis.title_ru or job.title}"[:120]

    budget = format_money(job.budget, job.currency, "не указан")
    price = format_money(analysis.recommended_price, job.currency, "—")
    status = "Рекомендуется ✅" if analysis.is_recommended else "Не рекомендуется ❌"
    posted = f"{job.posted_at.astimezone(tz or config.TIMEZONE):%Y-%m-%d %H:%M %Z}" if job.posted_at else "—"
    tech = "\n".join(f"- {t}" for t in analysis.technologies) or "—"
    description = _clip(analysis.description_ru or job.description)
    if analysis.title_ru or analysis.description_ru:
        # перевод от модели может быть неточным — оригинал остаётся под рукой
        description += f"""

<details>
<summary>Оригинал</summary>

**{job.title}**

{_clip(job.description)}

</details>"""
    llm = ""
    if analysis.sonnet_confidence is not None and analysis.opus_confidence is not None:
        llm = f"""
## Решаемость через LLM
- Claude Sonnet 5.5: {analysis.sonnet_confidence:.0%}
- Claude Opus 5.5: {analysis.opus_confidence:.0%}

{analysis.llm_notes or ""}
"""
    # fence длиннее любой последовательности ` внутри текста отклика
    longest = max((len(m) for m in re.findall(r"`+", analysis.response_template)), default=0)
    fence = "`" * max(3, longest + 1)

    body = f"""<!-- job-id: {job.id} -->
**Статус анализа**: {status} (релевантность {analysis.relevance_score:.0%})

## Основная информация
- Платформа: {source}
- Бюджет заказчика: {budget}
- Сложность: {analysis.difficulty}
- Ссылка: [Открыть заказ]({job.url})
- Опубликован: {posted}

## Описание задачи
{description}

## Технологии
{tech}

## Рекомендуемая цена
{price}

{analysis.reasoning}
{llm}
## Черновик отклика
{fence}
{analysis.response_template}
{fence}
"""
    labels = [config.GITHUB_ISSUE_LABEL, job.source, f"difficulty:{analysis.difficulty}"]
    if analysis.is_recommended:
        labels.append("recommended")
    return GitHubIssuePayload(title=title, body=body, labels=labels)


class GitHubIntegration:
    """Создание Issues и добавление их в колонку Backlog GitHub Project (v2)"""

    def __init__(self, token: str | None = None, repo: str | None = None):
        self.token = token or config.GITHUB_TOKEN
        self.repo_name = repo or config.GITHUB_REPO
        self._gh = Github(auth=Auth.Token(self.token))
        self._repo: Repository | None = None
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {self.token}"}, timeout=30
        )
        self._project: dict | None = None
        self._known: set[str] | None = None

    @property
    def repo(self) -> Repository:
        if self._repo is None:
            self._repo = self._gh.get_repo(self.repo_name)
        return self._repo

    def close(self) -> None:
        self._http.close()
        self._gh.close()

    # --- дедупликация ---

    def known_job_ids(self) -> set[str]:
        """ID заказов, по которым уже есть Issue (включая закрытые)"""
        if self._known is None:
            self._known = set()
            for issue in self.repo.get_issues(state="all", labels=[config.GITHUB_ISSUE_LABEL]):
                m = MARKER_RE.search(issue.body or "")
                if m:
                    self._known.add(m.group(1))
        return self._known

    # --- GraphQL ---

    def _graphql(self, query: str, variables: dict) -> dict:
        resp = self._http.post(GRAPHQL_URL, json={"query": query, "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL: {data['errors']}")
        return data["data"]

    def _load_project(self) -> dict:
        """id проекта, id поля статуса и id его опции (по умолчанию Status / Backlog)"""
        if self._project:
            return self._project
        node = None
        for owner_type in ("user", "organization"):
            try:
                data = self._graphql(
                    PROJECT_QUERY % owner_type,
                    {"login": config.GITHUB_PROJECT_OWNER, "number": config.GITHUB_PROJECT_ID},
                )
            except RuntimeError:
                continue
            node = (data.get(owner_type) or {}).get("projectV2")
            if node:
                break
        if not node:
            raise RuntimeError(
                f"Проект {config.GITHUB_PROJECT_OWNER}/{config.GITHUB_PROJECT_ID} не найден "
                "(проверьте scope `project` у токена)"
            )
        status_field, status_option = config.GITHUB_PROJECT_STATUS_FIELD, config.GITHUB_PROJECT_STATUS_OPTION
        field = next(
            (f for f in node["fields"]["nodes"] if f and f.get("name") == status_field), None
        )
        option = next((o for o in (field or {}).get("options", []) if o["name"] == status_option), None)
        if not option:
            raise RuntimeError(f"В проекте нет поля «{status_field}» со значением «{status_option}»")
        self._project = {"id": node["id"], "field": field["id"], "option": option["id"]}
        return self._project

    def add_to_backlog(self, issue_node_id: str) -> None:
        project = self._load_project()
        item = self._graphql(ADD_ITEM, {"project": project["id"], "content": issue_node_id})
        item_id = item["addProjectV2ItemById"]["item"]["id"]
        self._graphql(SET_STATUS, {
            "project": project["id"], "item": item_id,
            "field": project["field"], "option": project["option"],
        })

    # --- публикация ---

    def create_issue(self, job: Job, analysis: JobAnalysis) -> int:
        """Создать Issue и положить в Backlog; возвращает номер Issue"""
        payload = build_payload(job, analysis)
        issue = self.repo.create_issue(
            title=payload.title, body=payload.body, labels=payload.labels
        )
        self.known_job_ids().add(job.id)
        try:
            self.add_to_backlog(issue.raw_data["node_id"])
        except Exception as e:
            logger.error("Issue #%d создан, но не добавлен в проект: %s", issue.number, e)
        return issue.number

    def publish(
        self, pairs: list[tuple[Job, JobAnalysis]], only_recommended: bool = True
    ) -> list[int]:
        """Создать Issues для новых заказов; уже известные и нерекомендованные пропускаются"""
        known = self.known_job_ids()
        created: list[int] = []
        for job, analysis in pairs:
            if only_recommended and not analysis.is_recommended:
                continue
            if job.id in known:
                logger.info("%s: Issue уже существует, пропуск", job.id)
                continue
            try:
                created.append(self.create_issue(job, analysis))
            except Exception as e:
                logger.error("Не удалось создать Issue для %s: %s", job.id, e)
        return created
