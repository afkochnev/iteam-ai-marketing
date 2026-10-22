from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


api_path = Path("backend/app/api/content.py")
api = api_path.read_text()
api = replace_once(
    api,
    '''    status: ContentStatus | None = None,
    author_agent_id: UUID | None = None,
) -> list[ContentListItem]:
''',
    '''    status: ContentStatus | None = None,
    author_agent_id: UUID | None = None,
    include_archived: bool = False,
) -> list[ContentListItem]:
''',
    "content list include_archived parameter",
)
api = replace_once(
    api,
    '''        status=status,
        author_agent_id=author_agent_id,
    )
''',
    '''        status=status,
        author_agent_id=author_agent_id,
        include_archived=include_archived,
    )
''',
    "content list include_archived forwarding",
)
api_path.write_text(api)

repo_path = Path("backend/app/repositories/content.py")
repo = repo_path.read_text()
repo = replace_once(
    repo,
    '''    async def list(self, **filters: object) -> list[ContentItem]:
        stmt = (
''',
    '''    async def list(self, **filters: object) -> list[ContentItem]:
        include_archived = bool(filters.pop("include_archived", False))
        stmt = (
''',
    "repository include_archived flag",
)
repo = replace_once(
    repo,
    '''        if filters.get("status") is None:
            stmt = stmt.where(ContentItem.status != ContentStatus.ARCHIVED)
''',
    '''        if filters.get("status") is None and not include_archived:
            stmt = stmt.where(ContentItem.status != ContentStatus.ARCHIVED)
''',
    "repository archived filter",
)
repo_path.write_text(repo)

page_path = Path("frontend/app/publications/page.tsx")
page = page_path.read_text()
page = replace_once(
    page,
    '          contentApi.list({ campaign_id: campaign.id, content_type: "SOCIAL_POST" }),',
    '          contentApi.list({ campaign_id: campaign.id, content_type: "SOCIAL_POST", include_archived: "true" }),',
    "publication workspace archived content read",
)
page_path.write_text(page)

backend_test_path = Path("backend/app/tests/test_content_read_models.py")
backend_test = backend_test_path.read_text()
backend_test += '''

async def test_content_list_can_include_archived_items_for_operational_reads(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = User(
        email=f"archived-read-{uuid4()}@example.com",
        password_hash=hash_password("valid-password"),
        full_name="Archived Read User",
        role=UserRole.ADMIN,
    )
    agent = Agent(
        name="Writer",
        slug=f"archived-writer-{uuid4().hex[:8]}",
        role="content_writer",
        system_prompt="Write content.",
        status=AgentStatus.ACTIVE,
        autonomy_level=1,
        settings={},
    )
    db_session.add_all([user, agent])
    await db_session.flush()
    campaign = Campaign(name="Archived read campaign", goal="Operational history", created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    task = Task(
        campaign_id=campaign.id,
        task_type=TaskType.CREATE_SOCIAL_POSTS,
        title="Archived post task",
        assigned_agent_id=agent.id,
        priority=TaskPriority.NORMAL,
        status=TaskStatus.COMPLETED,
        input_data={},
        output_data={},
        requires_approval=False,
        retry_count=0,
    )
    db_session.add(task)
    await db_session.flush()
    post = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.SOCIAL_POST,
        title="Архивный пост",
        status=ContentStatus.ARCHIVED,
        author_agent_id=agent.id,
        metadata_={},
    )
    db_session.add(post)
    await db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "valid-password"},
    )
    assert login.status_code == 200

    default = await client.get(
        "/api/v1/content",
        params={"campaign_id": str(campaign.id), "content_type": "SOCIAL_POST"},
    )
    assert default.status_code == 200
    assert all(row["id"] != str(post.id) for row in default.json())

    operational = await client.get(
        "/api/v1/content",
        params={
            "campaign_id": str(campaign.id),
            "content_type": "SOCIAL_POST",
            "include_archived": "true",
        },
    )
    assert operational.status_code == 200
    archived = next(row for row in operational.json() if row["id"] == str(post.id))
    assert archived["title"] == "Архивный пост"
    assert archived["status"] == "ARCHIVED"
'''
backend_test_path.write_text(backend_test)

frontend_test_path = Path("frontend/tests/publications.test.tsx")
frontend_test = frontend_test_path.read_text()
frontend_test = frontend_test.replace(
    'expect(mocks.content).toHaveBeenLastCalledWith({ campaign_id: "campaign-1", content_type: "SOCIAL_POST" });',
    'expect(mocks.content).toHaveBeenLastCalledWith({ campaign_id: "campaign-1", content_type: "SOCIAL_POST", include_archived: "true" });',
)
frontend_test_path.write_text(frontend_test)
