from pathlib import Path
from typing import Literal

from pydantic import UUID4, BaseModel, Field, field_validator

MediaType = Literal["images", "audio", "videos", "qr_codes"]


class BaseStorage(BaseModel):
    """Base storage class with common media path operations."""

    base_path: Path

    @field_validator("base_path")
    @classmethod
    def validate_base_path(cls, v: Path) -> Path:
        """Ensure base_path is absolute and create if it doesn't exist."""
        path = v.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _get_media_path(self, entity_id: UUID4, media_type: MediaType) -> Path:
        """Generic method to construct media paths."""
        path = self.base_path / str(entity_id) / media_type
        path.mkdir(parents=True, exist_ok=True)
        return path

    def image_path(self, entity_id: UUID4) -> Path:
        """Get the images directory for an entity."""
        return self._get_media_path(entity_id, "images")

    def audio_path(self, entity_id: UUID4) -> Path:
        """Get the audio directory for an entity."""
        return self._get_media_path(entity_id, "audio")

    def video_path(self, entity_id: UUID4) -> Path:
        """Get the videos directory for an entity."""
        return self._get_media_path(entity_id, "videos")

    def get_all_media_paths(self, entity_id: UUID4) -> dict[str, Path]:
        """Get all media paths for an entity."""
        return {
            "images": self.image_path(entity_id),
            "audio": self.audio_path(entity_id),
            "videos": self.video_path(entity_id),
        }

    def entity_path(self, entity_id: UUID4) -> Path:
        """Get the root directory for an entity."""
        path = self.base_path / str(entity_id)
        path.mkdir(parents=True, exist_ok=True)
        return path


class ProjectStorage(BaseStorage):
    """Storage manager for project-related media."""

    base_path: Path = Field(default=Path("storage/projects/"))

    def qr_code_path(self, project_id: UUID4) -> Path:
        """Get the QR codes directory for a project."""
        return self._get_media_path(project_id, "qr_codes")

    def get_all_media_paths(self, entity_id: UUID4) -> dict[str, Path]:
        """Get all media paths for a project, including QR codes."""
        paths = super().get_all_media_paths(entity_id)
        paths["qr_codes"] = self.qr_code_path(entity_id)
        return paths


class InterviewStorage(BaseStorage):
    """Storage manager for interview-related media."""

    base_path: Path = Field(default=Path("storage/interviews/"))


class ExperimentStorage(BaseModel):
    base_path: Path

    @field_validator("base_path")
    @classmethod
    def validate_base_path(cls, v: Path) -> Path:
        """Ensure base_path is absolute and create if it doesn't exist."""
        path = v.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def qr_code_path(self, entity_id: UUID4) -> Path:
        path = self.base_path / str(entity_id) / "qr_codes"
        path.mkdir(parents=True, exist_ok=True)
        return path
