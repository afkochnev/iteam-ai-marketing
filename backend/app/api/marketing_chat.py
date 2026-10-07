from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.marketing_chat import MarketingConversation, MarketingMessage
from app.schemas.marketing_chat import (
    ConversationCreate,
    ConversationResponse,
    MessageResponse,
    MessageSend,
    TurnResponse,
)
from app.services.director_chat_service import DirectorChatService

router = APIRouter(tags=["marketing-director-chat"])


@router.post(
    "/campaigns/{campaign_id}/marketing-conversations",
    response_model=ConversationResponse,
    status_code=201,
)
async def create_conversation(
    campaign_id: UUID, payload: ConversationCreate, user: CurrentUser, session: SessionDependency
) -> MarketingConversation:
    return await DirectorChatService(session).create(campaign_id, user, payload.title)


@router.get(
    "/campaigns/{campaign_id}/marketing-conversations", response_model=list[ConversationResponse]
)
async def list_conversations(
    campaign_id: UUID, user: CurrentUser, session: SessionDependency
) -> list[MarketingConversation]:
    return await DirectorChatService(session).list_conversations(campaign_id, user)


@router.get("/marketing-conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID, user: CurrentUser, session: SessionDependency
) -> MarketingConversation:
    return await DirectorChatService(session).conversation(conversation_id, user)


@router.post(
    "/marketing-conversations/{conversation_id}/archive", response_model=ConversationResponse
)
async def archive_conversation(
    conversation_id: UUID, user: CurrentUser, session: SessionDependency
) -> MarketingConversation:
    return await DirectorChatService(session).archive(conversation_id, user)


@router.get(
    "/marketing-conversations/{conversation_id}/messages", response_model=list[MessageResponse]
)
async def list_messages(
    conversation_id: UUID, user: CurrentUser, session: SessionDependency
) -> list[MarketingMessage]:
    return await DirectorChatService(session).messages(conversation_id, user)


@router.post(
    "/marketing-conversations/{conversation_id}/messages",
    response_model=TurnResponse,
    status_code=202,
)
async def send_message(
    conversation_id: UUID, payload: MessageSend, user: CurrentUser, session: SessionDependency
) -> TurnResponse:
    return await DirectorChatService(session).send(
        conversation_id, user, payload.content, payload.client_message_id
    )


@router.post(
    "/marketing-conversations/{conversation_id}/messages/{message_id}/retry",
    response_model=MessageResponse,
    status_code=202,
)
async def retry_message(
    conversation_id: UUID, message_id: UUID, user: CurrentUser, session: SessionDependency
) -> MarketingMessage:
    return await DirectorChatService(session).retry(conversation_id, message_id, user)
