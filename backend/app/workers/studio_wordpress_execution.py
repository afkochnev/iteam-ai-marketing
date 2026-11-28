from dataclasses import dataclass
from datetime import timedelta
from html import escape
from uuid import UUID

from app.integrations.studio_wordpress import WordPressArticle
from app.schemas.content_asset import ArticleVersionData

LEASE_DURATION = timedelta(minutes=2)
OPERATION_TIMEOUT = 60


@dataclass(frozen=True)
class Claim:
    operation_id: UUID
    lease_id: UUID
    reconciliation: bool


def render_article(slug: str, article: ArticleVersionData) -> WordPressArticle:
    html = "\n".join(
        f"<p>{escape(line, quote=False)}</p>" for line in article.body.splitlines() if line
    )
    return WordPressArticle(slug, article.title, html)
