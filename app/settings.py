from __future__ import annotations

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


class EmailSettings(BaseModel):
    smtp_server: str
    smtp_port: int = 587
    smtp_use_ssl: bool = False
    sender: EmailAccount
    recipient: EmailAccount


class EmailAccount(BaseModel):
    email: str
    password: SecretStr


class ServiceSettings(BaseSettings):
    """Different extra services required to run the app"""

    email: EmailSettings | None = None

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
    services: ServiceSettings = ServiceSettings()  # ty: ignore[missing-argument]

    # TODO:
    # - Should the secrets be a standalone class so they cant be read
    # through the config.toml file?
    # - Should all "secrets" be moved to that class?
    secrets: AppSecrets = AppSecrets()  # ty: ignore[missing-argument]

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
app_settings = Settings()  # ty: ignore[missing-argument]

if __name__ == "__main__":
    from ainterviewer.settings import settings as lib_settings

    # print(app_settings.secrets)
    print(lib_settings)
    pass
