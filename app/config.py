from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_name: str = "pet_finder"
    admin_user_id: int = 0

    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "pet_finder"
    postgres_user: str = "pet_finder"
    postgres_password: str = "change_me"

    redis_host: str = "redis"
    redis_port: int = 6379

    daily_new_users_limit: int = 100
    history_backfill_limit: int = 2000
    min_messages_for_category: int = 2
    context_messages_count: int = 10
    dialogs_refresh_seconds: int = 300

    admin_panel_host: str = "0.0.0.0"
    admin_panel_port: int = 8080

    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


settings = Settings()
