from __future__ import annotations

import os
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    PyprojectTomlConfigSettingsSource,
    TomlConfigSettingsSource,
)

from ainterviewer.settings import BaseSettingsConfigDict
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import DatabaseType, TimeDelta

os.environ.setdefault("ANY_LLM_UNIFIED_EXCEPTIONS", "1")


class DemoLimits(BaseModel):
    """Synthetic-interview budgets for `Scope.DEMO` accounts, counted per user.

    Every synthetic interview is a full interview against the shared inference
    server, so a demo account gets a small budget of them.

    These are the defaults. The three counts can be overridden per user (the
    `user.demo_*_limit` columns, NULL meaning "use the default"), set by an
    admin or snapshotted from the invitation the user registered with; see
    `UserRepository.get_demo_limits`. `active_run_max_age` is an operational
    safeguard rather than a budget, so it stays global.
    """

    # Interviews in flight at once, across all of the user's runs.
    max_concurrent_synthetic_interviews: int = 10
    # Runs older than this no longer count as in flight, so a run orphaned in
    # RUNNING by a backend restart cannot lock a demo user out for good.
    active_run_max_age: TimeDelta = Field(default_factory=lambda: TimeDelta(hours=2))

    # Interviews started, whatever the runs' outcome: a failed run still spent
    # inference before it failed. "Daily" is a rolling 24 hours rather than a
    # calendar day, so it has no timezone to get wrong.
    daily_synthetic_interviews: int = 50
    lifetime_synthetic_interviews: int = 100


class AppSettings(BaseModel):
    api_host: str = "127.0.0.1"
    api_port: int = 8666

    app_host: str = "localhost"
    app_port: int = 5173

    web_host: str = "localhost"
    web_port: int = 5174

    # Where people reach the platform and the website from outside. Every link
    # the backend hands out -- invitations, participant links, verification
    # and password-reset emails -- is built on public_url, and both are the
    # allowed CORS origins. The defaults are the local dev servers, so a
    # deployed environment must set public_url; see
    # Settings.require_deployment_settings.
    public_url: str = "http://localhost:5173"
    website_url: str = "http://localhost:5174"

    # Who runs this instance. Participants are told to write here with
    # questions or corrections about their interview, so on a self-hosted
    # instance it must be its operator and not the hosted platform's address;
    # like public_url, a deployed environment must set it.
    contact_email: str = "contact@example.org"
    docs_url: str = "https://docs.ainterviewer.dk"

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
    # How long after a refresh token is spent it may be spent again without
    # counting as reuse. One browser routinely refreshes several times at
    # once -- parallel server renders, a client call, a second tab -- all
    # holding the same token, and only the first can win the rotation.
    refresh_token_reuse_grace: TimeDelta = Field(
        default_factory=lambda: TimeDelta(seconds=30)
    )
    registration_requires_token: bool = True

    email_verification_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(days=1)
    )
    login_code_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(minutes=10)
    )
    login_code_max_attempts: int = 5
    password_reset_token_expiration: TimeDelta = Field(
        default_factory=lambda: TimeDelta(hours=1)
    )
    code_resend_cooldown_seconds: int = 30

    demo_limits: DemoLimits = Field(default_factory=DemoLimits)

    # Debounce for recording activity (user_activity.last_seen and
    # user.last_active) from authenticated requests: a user is written at most
    # once per window per process, plus once on the first request of a day.
    last_active_debounce: TimeDelta = Field(
        default_factory=lambda: TimeDelta(minutes=10)
    )

    @field_validator("public_url", "website_url", "docs_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

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

    # SQLite only, and per-connection: see app/db/pragmas.py. On by default,
    # so a database cannot quietly fill with orphans again; exposed as a
    # setting so enforcement can be switched off with
    # `DATABASE__ENFORCE_FOREIGN_KEYS=false` and a restart, without a
    # deploy, if it surfaces a violation in production.
    enforce_foreign_keys: bool = True

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

    # Embed chunks as interviews run. Off, interviews emit nothing and the
    # server is only called for search and for a backfill someone asked for --
    # the dashboard's or the CLI's -- which trades freshness for inference
    # cost. Nothing is lost: the backfill re-derives every chunk from the
    # stored messages. Has no effect while `enabled` is false.
    live: bool = True

    endpoint: str | None = None
    model: str = "microsoft/harrier-oss-v1-0.6b"
    dimension: int = 1024

    # The server's `max_client_batch_size`; sending more in one request is
    # rejected rather than split.
    batch_size: int = 32

    # Budget for one request, applied on top of `batch_size` and measured as
    # `count x longest input` in characters: the server pads every input in a
    # batch out to the longest one, so that product is what a request costs,
    # not the sum. 16000 is the measured throughput optimum on the reference
    # box (see ../embedding/bench.py): it lands exactly on the best batch size
    # for message- and QA-length chunks, and puts interview-length chunks in a
    # request of their own, where they belong.
    max_batch_chars: int = 16000

    # Truncation guard, in characters. The server was launched with
    # `--max-batch-tokens 8192 --auto-truncate`, so anything longer is silently
    # cut off at the far end; truncating here instead means it is logged and the
    # stored `content_hash` matches the text actually embedded. ~3 chars/token
    # is conservative across the languages in use.
    max_input_chars: int = 24000

    # Generous on purpose: an interview-level chunk runs to the full 8k-token
    # input, and the reference deployment runs this model on CPU, where a batch
    # of those takes far longer than any interactive request would. Kept above
    # the proxy's own `request_timeout` so the server that knows why a request
    # is slow is the one that gets to answer: timing out first here just turns
    # its 502 into an empty ReadTimeout and abandons work still in flight.
    timeout: float = 360.0

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

    # Cap on how long a `Retry-After` from the load balancer is honoured. A
    # cold instance is worth waiting out; an unbounded wait would let one
    # header hold a request open far past any caller's patience.
    max_retry_after: float = 30.0

    # Consecutive failures before the client stops calling out and starts
    # failing fast, so a dead box degrades search rather than hanging requests.
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_seconds: float = 60.0

    @property
    def base_url(self) -> str:
        """Where embedding requests go.

        Defaults to the shared EC2 load balancer. The proxy serves the
        embedding pool behind the same address as the LLM pool, so there is
        normally no second host to configure -- set `endpoint` only to reach a
        server directly, bypassing the balancer.
        """
        return self.endpoint or lib_settings.llm.llm_endpoint


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
    # `default_factory`, not `ServiceSettings()`: the eager form is built at
    # class-definition time and reads `APP_SERVICE__*` on its own, so a partial
    # environment -- a .env carrying only the email passwords, with the rest of
    # the section in config.toml -- fails validation before the toml source is
    # ever consulted. Deferred, it runs only when config.toml supplies no
    # `services` at all, which is the case in CI and in a fresh clone.
    services: ServiceSettings = Field(default_factory=ServiceSettings)

    # TODO:
    # - Should the secrets be a standalone class so they cant be read
    # through the config.toml file?
    # - Should all "secrets" be moved to that class?
    secrets: AppSecrets = AppSecrets()

    model_config = BaseSettingsConfigDict(
        toml_file="config.toml",
        pyproject_toml_table_header=("tool", "ainterviewer"),
    )

    @model_validator(mode="after")
    def require_deployment_settings(self) -> Settings:
        # Falling back to a dev default in a deployed environment would mail
        # out links to localhost and point participants at a placeholder
        # address, so refuse to start instead.
        if self.app_env != "development":
            missing = [
                f"app.{name}"
                for name in ("public_url", "contact_email")
                if name not in self.app.model_fields_set
            ]
            if missing:
                raise ValueError(
                    f"{' and '.join(missing)} must be set "
                    f"when app_env is {self.app_env!r}"
                )
        return self

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
    print(app_settings.app)
