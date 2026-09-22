import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.security import hash_password
from app.models.agent import Agent, AgentRole, AgentSlug, AgentStatus, AgentTool
from app.models.knowledge import (
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
)
from app.models.user import UserRole
from app.repositories.agents import AgentRepository
from app.repositories.users import UserRepository

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class AgentSeed:
    name: str
    slug: AgentSlug
    role: AgentRole
    description: str
    autonomy_level: int
    tools: tuple[str, ...]


AGENT_SEEDS = (
    AgentSeed(
        "Marketing Director",
        AgentSlug.MARKETING_DIRECTOR,
        AgentRole.MARKETING_DIRECTOR,
        "Планирует маркетинговые кампании и координирует специализированных агентов.",
        3,
        ("read_campaign", "create_internal_task", "read_content", "read_knowledge_pack"),
    ),
    AgentSeed(
        "Knowledge Keeper",
        AgentSlug.KNOWLEDGE_KEEPER,
        AgentRole.KNOWLEDGE_KEEPER,
        "Находит и систематизирует внутренние знания iTeam с сохранением источников.",
        2,
        ("search_knowledge", "get_knowledge_item_metadata"),
    ),
    AgentSeed(
        "Writer",
        AgentSlug.WRITER,
        AgentRole.CONTENT_WRITER,
        "Создаёт экспертные материалы на основе проверенной базы знаний iTeam.",
        2,
        ("read_campaign", "read_knowledge_pack", "save_content", "create_content_revision"),
    ),
    AgentSeed(
        "SMM Manager",
        AgentSlug.SMM_MANAGER,
        AgentRole.SMM_MANAGER,
        "Адаптирует экспертный контент для социальных сетей без автоматической публикации.",
        2,
        (
            "read_campaign",
            "read_content_version",
            "read_content",
            "save_content",
            "create_content_revision",
        ),
    ),
)


async def seed_admin() -> bool:
    async with async_session_factory() as session:
        repository = UserRepository(session)
        if await repository.get_by_email(settings.admin_email):
            print(f"Admin {settings.admin_email.strip().lower()} already exists.")
            return False
        await repository.create(
            email=settings.admin_email,
            password_hash=hash_password(settings.admin_password),
            full_name=settings.admin_full_name,
            role=UserRole.ADMIN,
        )
        await session.commit()
        print(f"Admin {settings.admin_email.strip().lower()} created.")
        return True


async def seed_agents() -> tuple[int, int]:
    agents_created = 0
    tools_created = 0
    async with async_session_factory() as session:
        repository = AgentRepository(session)
        for definition in AGENT_SEEDS:
            agent = await repository.get_by_slug(definition.slug, with_tools=True)
            if agent is None:
                prompt = (PROMPTS_DIR / f"{definition.slug}.md").read_text(encoding="utf-8")
                agent = Agent(
                    name=definition.name,
                    slug=definition.slug,
                    role=definition.role,
                    description=definition.description,
                    system_prompt=prompt,
                    model=None,
                    status=AgentStatus.ACTIVE,
                    autonomy_level=definition.autonomy_level,
                    settings={},
                )
                session.add(agent)
                await session.flush()
                agents_created += 1
                existing_tools: set[str] = set()
            else:
                existing_tools = {tool.tool_name for tool in agent.tools}
            for tool_name in definition.tools:
                if tool_name not in existing_tools:
                    session.add(AgentTool(agent_id=agent.id, tool_name=tool_name))
                    tools_created += 1
        await session.commit()
    print(f"Agents created: {agents_created}; tools created: {tools_created}.")
    return agents_created, tools_created


async def seed_knowledge_sources() -> bool:
    async with async_session_factory() as session:
        from sqlalchemy import select

        existing = await session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.source_type == KnowledgeSourceType.FILE_UPLOAD
            )
        )
        if existing:
            print("Knowledge source 'Ручные загрузки' already exists.")
            return False
        session.add(
            KnowledgeSource(
                name="Ручные загрузки",
                source_type=KnowledgeSourceType.FILE_UPLOAD,
                status=KnowledgeSourceStatus.ACTIVE,
                metadata_={},
            )
        )
        await session.commit()
        print("Knowledge source 'Ручные загрузки' created.")
        return True


async def seed_all() -> None:
    await seed_admin()
    await seed_agents()
    await seed_knowledge_sources()


if __name__ == "__main__":
    asyncio.run(seed_all())
