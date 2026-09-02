from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, computed_field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    PyprojectTomlConfigSettingsSource,
    TomlConfigSettingsSource,
)

from ainterviewer.settings import BaseSettingsConfigDict
from ainterviewer.types import DatabaseType, TimeDelta

from .types import Scope

os.environ.setdefault("ANY_LLM_UNIFIED_EXCEPTIONS", "1")


class SpecialRegistrationTokens(BaseModel):
    token: str
    scope: Scope


class AppSettings(BaseModel):
    api_host: str = "127.0.0.1"
    api_port: int = 8666

    app_host: str = "localhost"
    app_port: int = 5173

    web_host: str = "localhost"
    web_port: int = 5174

    jwt_interview_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=3)
    )
    jwt_invite_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=1)
    )
    # How long a manually issued interview resume link stays redeemable. The
    # whole point is reaching someone who has been away a while, so it is
    # deliberately longer than jwt_interview_token_expiration -- that one
    # bounds the session the link hands out, this one bounds the link itself.
    interview_resume_link_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=7)
    )

    jwt_auth_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(minutes=15)
    )
    jwt_refresh_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=1)
    )
    jwt_refresh_token_extended_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=3)
    )
    registration_requires_token: bool = True
    special_registration_tokens: list[SpecialRegistrationTokens] = Field(
        default_factory=list
    )

    email_verification_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=1)
    )
    login_code_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(minutes=10)
    )
    login_code_max_attempts: int = 5
    code_resend_cooldown_seconds: int = 30

    # Debounce for the last_active touch on /refresh. A client with the app
    # open refreshes every jwt_auth_token_expiration, so without this the
    # column would be rewritten on that cadence for no extra resolution.
    last_active_debounce: TimeDelta = Field(
        default_factory=lambda: TimeDelta(minutes=10)
    )

    @computed_field
    def api_endpoint(self) -> str:
        return f"{self.api_host}:{self.api_port}"

    @computed_field
    def app_endpoint(self) -> str:
        return f"{self.app_host}:{self.app_port}"

    @computed_field
    def web_endpoint(self) -> str:
        return f"{self.web_host}:{self.web_port}"


class DatabaseSettings(BaseModel):
    db: DatabaseType = DatabaseType.SQLITE
    db_path: str = "storage"

    # SQLite only, and per-connection: see app/db/pragmas.py. Exposed as a
    # setting so enforcement can be switched off with
    # `APP_DATABASE__ENFORCE_FOREIGN_KEYS=false` and a restart, without a
    # deploy, if it surfaces a violation in production.
    enforce_foreign_keys: bool = False

    db_url: str = "localhost"
    db_port: str = "5432"
    db_name: str = "ainterviewer"

    @computed_field
    def database_file(self) -> str | None:
        return "db.sqlite" if self.db == DatabaseType.SQLITE else None

    @computed_field
    @property
    def connection_string(self) -> str:
        if self.db == DatabaseType.SQLITE:
            connection_string = f"sqlite:///{self.db_path}/{self.database_file}"
        else:
            # if not self.db_username or not self.db_password:
            #     raise ValueError(
            #         "`db_username` and `db_password` must be set for PostgreSQL"
            #     )
            # connection_string = f"postgresql://{self.db_username}:{self.db_password.get_secret_value()}@{self.db_url}:{self.db_port}/{self.db_name}"
            pass

        return connection_string


class EmailAccount(BaseModel):
    email: str
    password: SecretStr


class EmailSettings(BaseModel):
    smtp_server: str
    smtp_port: int = 587
    smtp_use_ssl: bool = False
    sender: EmailAccount
    recipient: EmailAccount


class SpeechSettings(BaseModel):
    stt_model: str | None = None
    stt_endpoint: str | None = None
    sst_delay: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"

    tts_model: str | None = None
    tts_endpoint: str | None = None
    tts_voice: str = "alloy"


class EmbeddingSettings(BaseModel):
    """Text-embedding-inference server used to vectorise interview text.

    Disabled by default: with `enabled = false` nothing is embedded and no
    embedder is handed to the interview loop, so a missing or unreachable
    server changes nothing about how interviews run.
    """

    enabled: bool = False
    endpoint: str | None = None
    model: str = "microsoft/harrier-oss-v1-0.6b"
    dimension: int = 1024

    # The server's `max_client_batch_size`; sending more in one request is
    # rejected rather than split.
    batch_size: int = 32

    # Truncation guard, in characters. The server was launched with
    # `--max-batch-tokens 8192 --auto-truncate`, so anything longer is silently
    # cut off at the far end; truncating here instead means it is logged and the
    # stored `content_hash` matches the text actually embedded. ~3 chars/token
    # is conservative across the languages in use.
    max_input_chars: int = 24000

    # Generous on purpose: an interview-level chunk runs to the full 8k-token
    # input, and the reference deployment runs this model on CPU, where a batch
    # of those takes far longer than any interactive request would.
    timeout: float = 120.0

    # How long to wait on a server that is not answering at all. Split out from
    # `timeout` because the two measure different things: a dropped SYN -- a box
    # that is down, or firewalled -- never gets faster by waiting, while a live
    # server chewing through a batch legitimately needs the two minutes above.
    # Left at 120s for both, an unreachable host turns every status read into a
    # two-minute hang and the pages behind it into blank loading states.
    #
    # Doubles as the whole budget for `/health`, which is a liveness probe: a
    # server that cannot answer it in this long is unusable either way.
    connect_timeout: float = 5.0

    max_retries: int = 3

    # Consecutive failures before the client stops calling out and starts
    # failing fast, so a dead box degrades search rather than hanging requests.
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_seconds: float = 60.0


class ServiceSettings(BaseSettings):
    """Different extra services required to run the app"""

    email: EmailSettings | None = None
    speech: SpeechSettings = SpeechSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()

    model_config = BaseSettingsConfigDict(env_prefix="APP_SERVICE__")


class AppSecrets(BaseSettings):
    jwt_secret_key: SecretStr
    session_secret_key: SecretStr

    db_username: str | None = None
    db_password: SecretStr | None = None

    model_config = BaseSettingsConfigDict(env_prefix="APP_SECRET__")


class Settings(BaseSettings):
    debug: bool = False
    app_env: Literal["production", "staging", "development"] = "development"

    app: AppSettings = AppSettings()
    database: DatabaseSettings = DatabaseSettings()
    services: ServiceSettings

    # TODO:
    # - Should the secrets be a standalone class so they cant be read
    # through the config.toml file?
    # - Should all "secrets" be moved to that class?
    secrets: AppSecrets = AppSecrets()

    model_config = BaseSettingsConfigDict(
        toml_file="config.toml",
        pyproject_toml_table_header=("tool", "ainterviewer"),
    )

    @property
    def sveltekit_platform_public_addr(self) -> str:
        match self.app_env:
            case "development":
                return "http://localhost:5173"
            case "staging":
                return "https://app.staging.ainterviewer.dk"
            case "production":
                return "https://app.ainterviewer.dk"

    @property
    def sveltekit_website_public_addr(self) -> str:
        match self.app_env:
            case "development":
                return "http://localhost:5174"
            case "staging":
                return "https://staging.ainterviewer.dk"
            case "production":
                return "https://ainterviewer.dk"

    @property
    def sveltekit_platform_addr(self) -> str:
        match self.app_env:
            case "development":
                return "http://localhost:5173"
            case "staging":
                return "http://localhost:4001"
            case "production":
                return "http://localhost:3001"

    @property
    def sveltekit_website_addr(self) -> str:
        match self.app_env:
            case "development":
                return "http://localhost:5174"
            case "staging":
                return "http://localhost:4000"
            case "production":
                return "http://localhost:3000"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            PyprojectTomlConfigSettingsSource(settings_cls),
        )


# TODO: Read/write to/from database or config file to get persistent changes?
app_settings = Settings()

if __name__ == "__main__":
    # from ainterviewer.settings import settings as lib_settings

    # print(app_settings.secrets)
    # print(lib_settings)
    # print(app_settings.services)
    print(app_settings.app.special_registration_tokens)
