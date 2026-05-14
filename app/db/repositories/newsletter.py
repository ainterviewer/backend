from sqlalchemy import select

from ainterviewer.utils import now

from ..tables import NewsletterSubscriptionTable
from .base import BaseRepository


class NewsletterRepository(BaseRepository):
    """Repository for newsletter subscriptions."""

    def subscribe(self, email: str) -> NewsletterSubscriptionTable:
        existing = self.session.execute(
            select(NewsletterSubscriptionTable).where(
                NewsletterSubscriptionTable.email == email
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.unsubscribed_at is not None:
                existing.unsubscribed_at = None
                self.session.commit()
                self.session.refresh(existing)
            return existing

        subscription = NewsletterSubscriptionTable(email=email)
        self.session.add(subscription)
        self.session.commit()
        self.session.refresh(subscription)
        return subscription

    def unsubscribe(self, email: str) -> bool:
        subscription = self.session.execute(
            select(NewsletterSubscriptionTable).where(
                NewsletterSubscriptionTable.email == email
            )
        ).scalar_one_or_none()

        if subscription is None or subscription.unsubscribed_at is not None:
            return False

        subscription.unsubscribed_at = now()
        self.session.commit()
        return True
