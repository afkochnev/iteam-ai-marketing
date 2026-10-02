from datetime import date
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.models.campaign import CampaignStatus
from app.models.user import User, UserRole
from app.repositories.campaigns import CampaignRepository
from app.repositories.users import UserRepository
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.services.campaign_service import CampaignService


async def create_user(session: AsyncSession, role: UserRole = UserRole.ADMIN) -> User:
    user = await UserRepository(session).create(
        email=f"{role.value.lower()}-{uuid4()}@example.com",
        password_hash=hash_password("valid-password"),
        full_name=f"{role.value} User",
        role=role,
    )
    await session.commit()
    return user


async def login(client: AsyncClient, user: User) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "valid-password"},
    )
    assert response.status_code == 200


def campaign_payload(name: str = "AI Campaign") -> CampaignCreate:
    return CampaignCreate(
        name=name,
        goal="Получить заявки",
        product="AI-диагностика",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"name": "", "goal": "Goal"},
        {"name": "   ", "goal": "Goal"},
        {"name": "Name", "goal": ""},
        {"name": "Name", "goal": "   "},
        {
            "name": "Name",
            "goal": "Goal",
            "start_date": "2026-10-31",
            "end_date": "2026-10-01",
        },
    ],
)
def test_campaign_create_validation_rejects_invalid_values(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CampaignCreate.model_validate(values)


def test_campaign_schema_accepts_equal_dates_and_normalizes_optional_values() -> None:
    campaign = CampaignCreate(
        name=" Name ",
        goal=" Goal ",
        description="   ",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
    )
    assert campaign.name == "Name"
    assert campaign.goal == "Goal"
    assert campaign.description is None


async def test_campaign_repository_crud_filter_and_order(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    repository = CampaignRepository(db_session)
    first = await repository.create(campaign_payload("First").model_dump(), user.id)
    second = await repository.create(campaign_payload("Second").model_dump(), user.id)
    await db_session.commit()
    await db_session.refresh(first)
    await db_session.refresh(second)

    assert first.id is not None
    assert first.status is CampaignStatus.DRAFT
    assert first.created_by == user.id
    assert first.strategy is None
    assert first.created_at.tzinfo is not None
    assert first.description is None
    assert (await repository.get_by_id(first.id)).id == first.id  # type: ignore[union-attr]

    listed_once = await repository.list_campaigns()
    listed_twice = await repository.list_campaigns()
    assert [item.id for item in listed_once] == [item.id for item in listed_twice]
    assert {item.id for item in listed_once} == {first.id, second.id}

    await repository.update(first, {"desired_result": "40 заявок"})
    await repository.archive(second)
    await db_session.commit()
    assert first.desired_result == "40 заявок"
    assert [item.id for item in await repository.list_campaigns()] == [first.id]
    assert [item.id for item in await repository.list_campaigns(CampaignStatus.ARCHIVED)] == [
        second.id
    ]


async def test_campaign_service_rules(db_session: AsyncSession) -> None:
    creator = await create_user(db_session)
    service = CampaignService(db_session)
    campaign = await service.create_campaign(campaign_payload(), creator)
    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.created_by == creator.id

    archived = await service.archive_campaign(campaign.id)
    assert archived.status is CampaignStatus.ARCHIVED
    assert (await service.archive_campaign(campaign.id)).status is CampaignStatus.ARCHIVED
    with pytest.raises(AppError) as archived_error:
        await service.update_campaign(campaign.id, CampaignUpdate(goal="Changed"))
    assert archived_error.value.code == "CAMPAIGN_ARCHIVED"


async def test_update_validates_resulting_date_range(db_session: AsyncSession) -> None:
    creator = await create_user(db_session)
    campaign = await CampaignService(db_session).create_campaign(campaign_payload(), creator)
    with pytest.raises(AppError) as error:
        await CampaignService(db_session).update_campaign(
            campaign.id,
            CampaignUpdate(start_date=date(2026, 11, 1)),
        )
    assert error.value.code == "INVALID_CAMPAIGN_DATE_RANGE"


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
async def test_authenticated_user_can_create_and_edit_campaign(
    client: AsyncClient, db_session: AsyncSession, role: UserRole
) -> None:
    user = await create_user(db_session, role)
    await login(client, user)
    created = await client.post(
        "/api/v1/campaigns",
        json={"name": f"{role.value} Campaign", "goal": "30 заявок"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "DRAFT"
    assert body["created_by"] == str(user.id)
    assert body["strategy"] is None

    campaign_id = body["id"]
    protected_update = await client.patch(
        f"/api/v1/campaigns/{campaign_id}",
        json={"status": "ACTIVE", "strategy": {"fake": True}, "created_by": str(uuid4())},
    )
    assert protected_update.status_code == 422
    unchanged = (await client.get(f"/api/v1/campaigns/{campaign_id}")).json()
    assert unchanged["status"] == "DRAFT"
    assert unchanged["strategy"] is None
    assert unchanged["created_by"] == str(user.id)
    assert (await client.get("/api/v1/campaigns")).status_code == 200
    assert (await client.get(f"/api/v1/campaigns/{campaign_id}")).status_code == 200
    updated = await client.patch(
        f"/api/v1/campaigns/{campaign_id}", json={"name": "Updated campaign"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated campaign"
    strategic = await client.patch(
        f"/api/v1/campaigns/{campaign_id}", json={"desired_result": "40 заявок"}
    )
    assert strategic.status_code == 409
    assert strategic.json()["error"]["code"] == "CAMPAIGN_STRATEGIC_CHANGE_REQUIRES_PREVIEW"

    archived = await client.post(f"/api/v1/campaigns/{campaign_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"
    assert (
        await client.patch(f"/api/v1/campaigns/{campaign_id}", json={"goal": "No"})
    ).status_code == 409
    assert len((await client.get("/api/v1/campaigns")).json()) == 0
    assert len((await client.get("/api/v1/campaigns?status=ARCHIVED")).json()) == 1


async def test_campaign_api_auth_not_found_validation_and_injection(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await client.get("/api/v1/campaigns")).status_code == 401
    user = await create_user(db_session)
    await login(client, user)
    assert (await client.get(f"/api/v1/campaigns/{uuid4()}")).status_code == 404

    invalid_date = await client.post(
        "/api/v1/campaigns",
        json={
            "name": "Invalid",
            "goal": "Goal",
            "start_date": "2026-10-31",
            "end_date": "2026-10-01",
        },
    )
    assert invalid_date.status_code == 422

    injected = await client.post(
        "/api/v1/campaigns",
        json={
            "name": "Injected",
            "goal": "Goal",
            "status": "ACTIVE",
            "created_by": str(uuid4()),
            "strategy": {"fake": True},
        },
    )
    assert injected.status_code == 422
    assert injected.json()["error"]["code"] == "VALIDATION_ERROR"
