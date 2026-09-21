from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "iTeam AI Marketing Department"
    app_env: str = "development"
    app_secret: str = "development-only-change-me"
    database_url: str = "postgresql+asyncpg://iteam:iteam@postgres:5432/iteam"
    redis_url: str = "redis://redis:6379/0"
    frontend_url: str = "http://localhost:3000"
    jwt_secret: str = "development-only-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    auth_cookie_name: str = "iteam_access_token"
    cookie_secure: bool = False
    admin_email: str = "admin@example.com"
    admin_password: str = "change-me-before-use"
    admin_full_name: str = "iTeam Administrator"
    openai_api_key: str | None = None
    openai_default_model: str | None = None
    openai_agents_disable_tracing: bool = True
    agent_max_turns: int = 8
    agent_run_timeout_seconds: int = 180
    agent_max_retries: int = 3

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
