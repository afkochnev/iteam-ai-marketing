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
    smm_agent_max_turns: int = 3
    agent_run_timeout_seconds: int = 180
    agent_provider_request_timeout_seconds: int = 60
    smm_final_provider_timeout_seconds: int = 120
    agent_provider_max_retries: int = 0
    agent_max_retries: int = 3
    agent_output_repair_attempts: int = 2
    agent_retry_backoff_seconds: int = 5
    task_stuck_after_seconds: int = 600
    rate_limit_login_per_minute: int = 10
    rate_limit_ai_actions_per_minute: int = 30
    rate_limit_uploads_per_minute: int = 10
    openai_vector_store_id: str | None = None
    knowledge_max_upload_mb: int = 25
    max_upload_size_mb: int = 25
    knowledge_index_timeout_seconds: int = 300
    knowledge_index_poll_interval_seconds: float = 2.0
    task_dispatch_interval_seconds: int = 10
    allowed_hosts: str = "localhost,127.0.0.1"
    cors_allowed_origins: str | None = None
    log_level: str = "INFO"
    app_version: str = "0.1.0"
    build_sha: str | None = None
    telegram_publishing_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_target_chat_id: str | None = None
    telegram_request_timeout_seconds: int = 30
    publication_max_retries: int = 3
    publication_publishing_stale_seconds: int = 600
    vk_publishing_enabled: bool = False
    vk_access_token: str | None = None
    vk_owner_id: int | None = None
    vk_api_version: str = "5.199"
    vk_request_timeout_seconds: int = 30
    metrics_sync_enabled: bool = False
    metrics_sync_interval_seconds: int = 3600
    metrics_lookback_days: int = 30

    @property
    def allowed_host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        value = self.cors_allowed_origins or self.frontend_url
        return [item.strip() for item in value.split(",") if item.strip()]

    def validate_production(self) -> None:
        numeric_errors: list[str] = []
        positive_values = {
            "ACCESS_TOKEN_EXPIRE_MINUTES": self.access_token_expire_minutes,
            "AGENT_RUN_TIMEOUT_SECONDS": self.agent_run_timeout_seconds,
            "AGENT_PROVIDER_REQUEST_TIMEOUT_SECONDS": self.agent_provider_request_timeout_seconds,
            "SMM_FINAL_PROVIDER_TIMEOUT_SECONDS": self.smm_final_provider_timeout_seconds,
            "AGENT_MAX_TURNS": self.agent_max_turns,
            "SMM_AGENT_MAX_TURNS": self.smm_agent_max_turns,
            "AGENT_OUTPUT_REPAIR_ATTEMPTS": self.agent_output_repair_attempts,
            "AGENT_RETRY_BACKOFF_SECONDS": self.agent_retry_backoff_seconds,
            "TASK_STUCK_AFTER_SECONDS": self.task_stuck_after_seconds,
            "TASK_DISPATCH_INTERVAL_SECONDS": self.task_dispatch_interval_seconds,
            "TELEGRAM_REQUEST_TIMEOUT_SECONDS": self.telegram_request_timeout_seconds,
            "VK_REQUEST_TIMEOUT_SECONDS": self.vk_request_timeout_seconds,
            "METRICS_SYNC_INTERVAL_SECONDS": self.metrics_sync_interval_seconds,
            "METRICS_LOOKBACK_DAYS": self.metrics_lookback_days,
            "PUBLICATION_PUBLISHING_STALE_SECONDS": self.publication_publishing_stale_seconds,
        }
        numeric_errors.extend(name for name, value in positive_values.items() if value <= 0)
        if self.publication_max_retries < 0 or self.publication_max_retries > 10:
            numeric_errors.append("PUBLICATION_MAX_RETRIES")
        if self.agent_max_retries < 0 or self.agent_max_retries > 10:
            numeric_errors.append("AGENT_MAX_RETRIES")
        if numeric_errors:
            raise RuntimeError(
                "Configuration contains invalid numeric values: "
                + ", ".join(sorted(set(numeric_errors)))
            )
        if self.app_env.lower() not in {"production", "prod"}:
            return
        placeholders = {
            "APP_SECRET": self.app_secret,
            "JWT_SECRET": self.jwt_secret,
            "ADMIN_PASSWORD": self.admin_password,
        }
        errors = [
            name
            for name, value in placeholders.items()
            if len(value) < 32 or "change-me" in value.lower() or "replace-with" in value.lower()
        ]
        required = {
            "DATABASE_URL": self.database_url,
            "REDIS_URL": self.redis_url,
            "OPENAI_API_KEY": self.openai_api_key,
            "OPENAI_DEFAULT_MODEL": self.openai_default_model,
            "FRONTEND_URL": self.frontend_url,
        }
        errors.extend(name for name, value in required.items() if not value)
        if self.telegram_publishing_enabled:
            if not self.telegram_bot_token:
                errors.append("TELEGRAM_BOT_TOKEN")
            if not self.telegram_target_chat_id:
                errors.append("TELEGRAM_TARGET_CHAT_ID")
        if self.vk_publishing_enabled:
            if not self.vk_access_token:
                errors.append("VK_ACCESS_TOKEN")
            if self.vk_owner_id is None:
                errors.append("VK_OWNER_ID")
        if not self.cookie_secure:
            errors.append("COOKIE_SECURE")
        if not self.allowed_hosts or self.allowed_hosts == "localhost,127.0.0.1":
            errors.append("ALLOWED_HOSTS")
        if errors:
            raise RuntimeError(
                "Production configuration is invalid; set required secure values: "
                + ", ".join(sorted(set(errors)))
            )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
