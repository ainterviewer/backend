from datetime import datetime

from pydantic import BaseModel


class GitHashes(BaseModel):
    core_lib: str
    backend: str
    frontend: str


class PlatformManifest(BaseModel):
    platform_version: str
    core_lib: str
    backend: str
    frontend: str
    build_time: datetime
    git: GitHashes
