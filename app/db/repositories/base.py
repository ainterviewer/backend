from sqlalchemy.orm import Session


class BaseRepository:
    """Base class for all repositories providing shared session access."""

    def __init__(self, session: Session):
        self.session = session
