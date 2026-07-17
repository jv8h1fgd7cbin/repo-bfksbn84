from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_name: str = "pet_finder"
    admin_user_id: int = 0

    # Доступ к админ-панели: вход через Telegram (кука сессии); секрет для подписи куки
    admin_auth_enabled: bool = True
    admin_secret: str = ""

    ai_provider: str = "openai"  # openai | anthropic
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_model: str = "claude-3-5-haiku-latest"

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "pet_finder"
    postgres_user: str = "pet_finder"
    postgres_password: str = "change_me"

    redis_host: str = "redis"
    redis_port: int = 6379

    # Защита от 429 у ИИ-провайдера: троттлинг, backoff и обработка порциями
    ai_min_interval_seconds: float = 3.0  # минимальная пауза между запросами к ИИ
    ai_retry_max_attempts: int = 5  # попыток при 429/5xx (с ожиданием по Retry-After)
    ai_backoff_base_seconds: int = 30  # базовое ожидание при 429 без Retry-After
    ai_batch_size: int = 10  # сколько пользователей анализировать подряд
    ai_batch_pause_seconds: int = 60  # пауза между порциями анализа

    daily_new_users_limit: int = 100
    history_backfill_limit: int = 2000
    min_messages_for_category: int = 2
    context_messages_count: int = 10
    dialogs_refresh_seconds: int = 300
    # Чёрный список: chat_id или username через запятую — эти чаты не мониторятся
    excluded_chats: str = ""

    # Авто-поиск и авто-вступление в публичные группы
    discovery_enabled: bool = True
    discovery_keywords: str = (
        "собаки,собака,собаководы,щенки,щенок,питомник собак,кинолог,дрессировка собак,"
        "кошки,кошка,котята,котёнок,кошатники,котолюбы,котомания,"
        "домашние животные,питомцы,питомец,зоомагазин,зоотовары,корм для животных,"
        "ветеринар,ветклиника,ветеринария,груминг,передержка,"
        "приют животных,приют для собак,приют для кошек,волонтёры животные,пристройство животных,"
        "лабрадор,овчарка,хаски,корги,шпиц,такса,чихуахуа,йорк,мопс,бульдог,"
        "мейн-кун,британская кошка,шотландская кошка,сфинкс,бенгальская кошка,вислоухие"
    )
    discovery_interval_seconds: int = 1800  # как часто искать новые группы
    max_joins_per_day: int = 20  # консервативный лимит вступлений в сутки
    join_delay_min_seconds: int = 180  # минимальная пауза между вступлениями
    join_delay_max_seconds: int = 300  # максимальная пауза между вступлениями
    relevance_sample_size: int = 40  # сколько сообщений группы читать для ИИ-проверки релевантности

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

    @property
    def excluded_chat_set(self) -> set[str]:
        """Идентификаторы исключённых чатов (chat_id и username, в нижнем регистре)."""
        return {c.strip().lstrip("@").lower() for c in self.excluded_chats.split(",") if c.strip()}

    @property
    def discovery_keyword_list(self) -> list[str]:
        return [k.strip() for k in self.discovery_keywords.split(",") if k.strip()]


settings = Settings()
