from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from core.timeutil import utcnow


class PlaybackProgress(Base):
    __tablename__ = "playback_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "media_id", name="uq_playback_progress_user_media"),
        Index("idx_playback_progress_user_media", "user_id", "media_id"),
    )

    id               : Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id          : Mapped[int]            = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id         : Mapped[int]            = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    progress_percent : Mapped[float]          = mapped_column(Float, default=0.0, nullable=False)
    progress_seconds : Mapped[int]            = mapped_column(Integer, default=0, nullable=False)
    updated_at       : Mapped[datetime]       = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    # Continue Watching rank when it differs from updated_at: Stremio bumps a
    # library series when a new episode is released. Kept apart from
    # updated_at, which playback push sends back as Stremio's lastWatched.
    sort_at          : Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user  : Mapped["User"]  = relationship()
    media : Mapped["Media"] = relationship()
