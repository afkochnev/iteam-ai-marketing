from uuid import UUID

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    CurrentSnapshotUser,
    CurrentUser,
    ReadSnapshotSession,
    SessionDependency,
)
from app.services.studio_website_sandbox import (
    WebsiteDraft,
    WebsitePage,
    WebsiteSandbox,
    WebsiteStatus,
)

router = APIRouter(tags=["studio-website-sandbox"])
ARTICLE_PATH = "/campaigns/{campaign_id}/studio/versions/{version_id}/website-sandbox"


@router.get(ARTICLE_PATH, response_model=WebsiteStatus)
async def get_website_status(
    campaign_id: UUID,
    version_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    response: Response,
) -> WebsiteStatus:
    response.headers["Cache-Control"] = "private, no-store"
    return await WebsiteSandbox(session).status(user.id, campaign_id, version_id)


@router.post(ARTICLE_PATH + "/draft", response_model=WebsitePage)
async def create_website_draft(
    campaign_id: UUID,
    version_id: UUID,
    metadata: WebsiteDraft,
    user: CurrentUser,
    session: SessionDependency,
) -> WebsitePage:
    draft = await WebsiteSandbox(session).create_draft(user.id, campaign_id, version_id, metadata)
    await session.commit()
    return draft


@router.post(ARTICLE_PATH + "/{page_id}/publish", response_model=WebsitePage)
async def publish_website_page(
    campaign_id: UUID,
    version_id: UUID,
    page_id: UUID,
    user: CurrentUser,
    session: SessionDependency,
) -> WebsitePage:
    page = await WebsiteSandbox(session).publish(user.id, campaign_id, version_id, page_id)
    await session.commit()
    return page


@router.get(ARTICLE_PATH + "/{page_id}/preview", response_class=HTMLResponse)
async def preview_website_draft(
    campaign_id: UUID,
    version_id: UUID,
    page_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
) -> HTMLResponse:
    return _page_response(
        await WebsiteSandbox(session).preview(user.id, campaign_id, version_id, page_id)
    )


@router.get("/website-sandbox/{page_id}", response_class=HTMLResponse)
async def get_published_website_page(page_id: UUID, session: ReadSnapshotSession) -> HTMLResponse:
    return _page_response(await WebsiteSandbox(session).public_page(page_id))


def _page_response(html: str) -> HTMLResponse:
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": (
                "default-src 'none'; img-src data:; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
