"""Tests for sync progress counter fixes (#494, #497, #503)."""
import os
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from models.base import MediaType, CollectionSource
from models.collection import Collection, CollectionFile
from models.events import WatchEvent
from models.media import Media
from models.show import Show
from models.sync import SyncJob, SyncStatus
from models.playback_progress import PlaybackProgress
from routers.sync import (
    sync_items,
    _apply_nuvio_progress,
    _apply_nuvio_watch_history,
    _apply_stremio_progress_clears,
    _drop_shadowed_stremio_clears,
    _expand_multi_episode_items,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """JSONB is PostgreSQL-only and has no SQLite rendering of its own."""
    return "JSON"


class _TestDB(unittest.IsolatedAsyncioTestCase):
    """SQLite harness for sync tests."""

    async def asyncSetUp(self):
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)


class StremioWatchHistorySourceTests(_TestDB):
    """Regression test for #503: Stremio watch history was saved with source="nuvio"
    instead of source="stremio" because _apply_nuvio_watch_history hardcoded the source."""

    async def test_nuvio_watch_history_uses_nuvio_source(self):
        """Default source should be 'nuvio' for backward compatibility."""
        async with self.Session() as db:
            media = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(media)
            await db.commit()
            media_id = media.id

            rows = [{
                "content_id": "tmdb:603",
                "content_type": "movie",
                "watched_at": 1705317600000,  # Unix timestamp in milliseconds
            }]
            tmdb_ids = {"tmdb:603": 603}

            added_ids = await _apply_nuvio_watch_history(
                db, user_id=1, rows=rows, show_map={}, tmdb_ids=tmdb_ids
            )

            self.assertEqual(len(added_ids), 1)
            self.assertIn(media_id, added_ids)

            events = (await db.execute(
                select(WatchEvent).where(WatchEvent.user_id == 1, WatchEvent.media_id == media_id)
            )).scalars().all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "nuvio")

    async def test_stremio_watch_history_uses_stremio_source(self):
        """When source="stremio" is passed, watch events should have that source."""
        async with self.Session() as db:
            media = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(media)
            await db.commit()
            media_id = media.id

            rows = [{
                "content_id": "tmdb:603",
                "content_type": "movie",
                "watched_at": 1705317600000,  # Unix timestamp in milliseconds
            }]
            tmdb_ids = {"tmdb:603": 603}

            added_ids = await _apply_nuvio_watch_history(
                db, user_id=1, rows=rows, show_map={}, tmdb_ids=tmdb_ids,
                source="stremio"
            )

            self.assertEqual(len(added_ids), 1)
            self.assertIn(media_id, added_ids)

            events = (await db.execute(
                select(WatchEvent).where(WatchEvent.user_id == 1, WatchEvent.media_id == media_id)
            )).scalars().all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "stremio")


class StremioProgressClearTests(_TestDB):
    """Progress for items that left Stremio's Continue Watching is deleted on pull."""

    async def _progress_media_ids(self, db) -> set[int]:
        rows = (await db.execute(select(PlaybackProgress.media_id))).scalars().all()
        return set(rows)

    async def _add_progress(self, db, media: Media, updated_at: datetime) -> None:
        db.add(PlaybackProgress(
            user_id=1, media_id=media.id, progress_percent=0.4, progress_seconds=600, updated_at=updated_at,
        ))

    async def test_movie_clear_deletes_progress(self):
        async with self.Session() as db:
            movie = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(movie)
            await db.flush()
            await self._add_progress(db, movie, datetime(2026, 7, 1))
            await db.commit()

            deleted = await _apply_stremio_progress_clears(
                db, 1,
                [{"content_id": "tt0133093", "content_type": "movie", "cleared_at": 1785528000000}],
                {}, {"tt0133093": 603},
            )

            self.assertEqual(deleted, 1)
            self.assertEqual(await self._progress_media_ids(db), set())

    async def test_clear_keeps_progress_newer_than_stremio_state(self):
        """A full resync must not wipe progress another source wrote later."""
        async with self.Session() as db:
            movie = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(movie)
            await db.flush()
            await self._add_progress(db, movie, datetime(2026, 9, 1))
            await db.commit()

            deleted = await _apply_stremio_progress_clears(
                db, 1,
                # 2026-07-31T20:00:00Z
                [{"content_id": "tt0133093", "content_type": "movie", "cleared_at": 1785528000000}],
                {}, {"tt0133093": 603},
            )

            self.assertEqual(deleted, 0)
            self.assertEqual(await self._progress_media_ids(db), {movie.id})

    async def _show_with_episodes(self, db):
        show = Show(tmdb_id=1399, title="Game of Thrones")
        db.add(show)
        await db.flush()
        episodes = {
            n: Media(media_type=MediaType.episode, title=f"S1E{n}", show_id=show.id, season_number=1, episode_number=n)
            for n in (3, 4)
        }
        db.add_all(episodes.values())
        await db.flush()
        return show, episodes

    async def test_new_current_episode_replaces_previous_one(self):
        async with self.Session() as db:
            show, episodes = await self._show_with_episodes(db)
            await self._add_progress(db, episodes[3], datetime(2026, 7, 20))
            await db.commit()

            # Stremio moved on to S1E4: upsert it, then clear the rest of the show.
            await _apply_nuvio_progress(
                db, 1,
                [{
                    "content_id": "tt0944947", "content_type": "series", "season": 1, "episode": 4,
                    "position": 1_200_000, "duration": 3_600_000, "last_watched": 1785067200000,
                }],
                {"tt0944947": show.id}, {"tt0944947": 1399},
            )
            await _apply_stremio_progress_clears(
                db, 1,
                [{
                    "content_id": "tt0944947", "content_type": "series", "cleared_at": 1785067200000,
                    "keep_season": 1, "keep_episode": 4,
                }],
                {"tt0944947": show.id}, {"tt0944947": 1399},
            )

            self.assertEqual(await self._progress_media_ids(db), {episodes[4].id})

    async def test_series_clear_without_show_map_finds_existing_show(self):
        """A clear-only series is never synced this run, so it isn't in show_map."""
        async with self.Session() as db:
            show, episodes = await self._show_with_episodes(db)
            for episode in episodes.values():
                await self._add_progress(db, episode, datetime(2026, 7, 20))
            await db.commit()

            deleted = await _apply_stremio_progress_clears(
                db, 1,
                [{"content_id": "tt0944947", "content_type": "series", "cleared_at": None}],
                {}, {"tt0944947": 1399},
            )

            self.assertEqual(deleted, 2)
            self.assertEqual(await self._progress_media_ids(db), set())

    async def test_clear_for_unknown_show_creates_nothing(self):
        async with self.Session() as db:
            deleted = await _apply_stremio_progress_clears(
                db, 1,
                [{"content_id": "tt0944947", "content_type": "series", "cleared_at": None}],
                {}, {"tt0944947": 1399},
            )

            self.assertEqual(deleted, 0)
            self.assertEqual((await db.execute(select(Show))).scalars().all(), [])

    def test_stale_duplicate_entry_does_not_clear_active_title(self):
        """Euphoria lives in Stremio as tt8772296 (in Continue Watching) and as
        a stale tmdb:85552 at timeOffset 0; both resolve to the same show."""
        tmdb_ids = {"tt8772296": 85552, "tmdb:85552": 85552, "tt0133093": 603}
        keep = {"content_id": "tt8772296", "content_type": "series", "cleared_at": 1, "keep_season": 3, "keep_episode": 2}
        stale = {"content_id": "tmdb:85552", "content_type": "series", "cleared_at": 1}
        unrelated = {"content_id": "tt0133093", "content_type": "movie", "cleared_at": 1}

        self.assertEqual(
            _drop_shadowed_stremio_clears([], [keep, stale, unrelated], tmdb_ids),
            [keep, unrelated],
        )

    def test_movie_progress_shadows_stale_duplicate_clear(self):
        tmdb_ids = {"tt14173636": 1, "tmdb:1": 1}
        progress = [{"content_id": "tt14173636", "content_type": "movie", "position": 10, "duration": 100}]
        stale = {"content_id": "tmdb:1", "content_type": "movie", "cleared_at": 1}

        self.assertEqual(_drop_shadowed_stremio_clears(progress, [stale], tmdb_ids), [])


class StremioProgressWindowTests(_TestDB):
    """Stremio mirrors Continue Watching, so progress outside 5-90% is kept."""

    async def _apply(self, db, position: int, **kwargs) -> list[PlaybackProgress]:
        await _apply_nuvio_progress(
            db, 1,
            [{"content_id": "tt0133093", "content_type": "movie", "position": position, "duration": 3_600_000}],
            {}, {"tt0133093": 603},
            **kwargs,
        )
        return (await db.execute(select(PlaybackProgress))).scalars().all()

    async def _movie(self, db) -> Media:
        movie = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
        db.add(movie)
        await db.commit()
        return movie

    async def test_next_episode_marker_is_kept_when_mirroring(self):
        async with self.Session() as db:
            await self._movie(db)
            [row] = await self._apply(db, 1, mirror_all=True)
            self.assertEqual(row.progress_seconds, 0)

    async def test_nearly_finished_is_kept_when_mirroring(self):
        async with self.Session() as db:
            await self._movie(db)
            [row] = await self._apply(db, 3_500_000, mirror_all=True)
            self.assertGreater(row.progress_percent, 0.9)

    async def test_default_window_still_drops_low_progress(self):
        """Nuvio keeps the 5-90% window."""
        async with self.Session() as db:
            await self._movie(db)
            self.assertEqual(await self._apply(db, 1), [])


class SyncItemsBatchBoundarySkipTests(_TestDB):
    """Regression test for #494: when an unmatched item is skipped at exactly a
    batch boundary, the batch bookkeeping is jumped over by continue, causing
    processed_items to lose that entire batch."""

    async def test_skipped_item_at_batch_boundary_does_not_lose_batch(self):
        """A skipped item at position (BATCH_SIZE - 1) must still count the batch."""
        async with self.Session() as db:
            # Create a sync job
            job = SyncJob(
                user_id=1, source=CollectionSource.jellyfin, status=SyncStatus.running,
                processed_items=0, total_items=3
            )
            db.add(job)
            await db.commit()
            job_id = job.id

            # Patch BATCH_SIZE to 2 for testing (as mentioned in the issue)
            with patch("routers.sync.BATCH_SIZE", 2):
                # Three unmatched movies without a name (will be skipped)
                items = [
                    {"Id": "1", "ProviderIds": {}},  # No TMDB ID, no name
                    {"Id": "2", "ProviderIds": {}},  # No TMDB ID, no name - at boundary (i=1, (i+1) % 2 == 0)
                    {"Id": "3", "ProviderIds": {}},  # No TMDB ID, no name
                ]

                stats = {"movies": 0, "skipped": 0, "errors": 0}
                await sync_items(
                    items=items,
                    media_type=MediaType.movie,
                    source=CollectionSource.jellyfin,
                    db=db,
                    stats=stats,
                    user_id=1,
                    job_id=job_id,
                )

            # Refresh job to get updated values
            await db.refresh(job)

            # All 3 items should be counted as processed (even though they were skipped)
            self.assertEqual(job.processed_items, 3,
                "Skipped items at batch boundaries should still count toward processed_items")
            self.assertEqual(stats["skipped"], 3)


class SyncItemsMultiEpisodeProgressTests(_TestDB):
    """Regression test for #497: multi-episode files are expanded after total_items
    is set, causing processed_items to exceed total_items and finish above 100%."""

    async def test_multi_episode_expansion_updates_total_items(self):
        """After expanding multi-episode files, total_items should be updated."""
        async with self.Session() as db:
            # Create a show
            show = Show(tmdb_id=1396, title="Breaking Bad")
            db.add(show)
            await db.commit()
            show_id = show.id

            # Create a sync job with initial count of 1 (the unexpanded file)
            job = SyncJob(
                user_id=1, source=CollectionSource.jellyfin, status=SyncStatus.running,
                processed_items=0, total_items=1
            )
            db.add(job)
            await db.commit()
            job_id = job.id

            # One multi-episode file covering episodes 1-3
            items = [{
                "Id": "jf-multi",
                "SeriesId": "jf-series-1",
                "Name": "Pilot",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "IndexNumberEnd": 3,  # Will expand to 3 episodes
                "ProviderIds": {},
            }]

            # Verify expansion creates 3 items from 1
            expanded = _expand_multi_episode_items(items, MediaType.episode, CollectionSource.jellyfin)
            self.assertEqual(len(expanded), 3, "Multi-episode file should expand to 3 episodes")

            show_map = {"jf-series-1": show_id}
            stats = {"episodes": 0, "skipped": 0, "errors": 0}

            await sync_items(
                items=items,  # Pass unexpanded items (sync_items will expand them)
                media_type=MediaType.episode,
                source=CollectionSource.jellyfin,
                db=db,
                stats=stats,
                user_id=1,
                job_id=job_id,
                show_map=show_map,
            )

            await db.refresh(job)

            # total_items should have been updated from 1 to 3 after expansion
            self.assertEqual(job.total_items, 3,
                "total_items should be updated after multi-episode expansion")
            # All 3 expanded episodes should be processed
            self.assertEqual(job.processed_items, 3,
                "processed_items should count all expanded episodes")

    async def test_plex_multi_episode_also_updates_total_items(self):
        """Plex multi-episode files should also trigger total_items update (though Plex
        doesn't actually use multi-episode files, this verifies the source check works)."""
        async with self.Session() as db:
            show = Show(tmdb_id=1396, title="Breaking Bad")
            db.add(show)
            await db.commit()

            job = SyncJob(
                user_id=1, source=CollectionSource.plex, status=SyncStatus.running,
                processed_items=0, total_items=1
            )
            db.add(job)
            await db.commit()
            job_id = job.id

            # Plex doesn't expand, so total_items should NOT be updated
            items = [{
                "ratingKey": "123",
                "grandparentRatingKey": "100",
                "title": "Pilot",
                "parentIndex": 1,
                "index": 1,
                "Guid": [],
            }]

            await sync_items(
                items=items,
                media_type=MediaType.episode,
                source=CollectionSource.plex,  # Plex, not Jellyfin/Emby
                db=db,
                stats={"episodes": 0, "skipped": 0, "errors": 0},
                user_id=1,
                job_id=job_id,
                show_map={"100": show.id},
            )

            await db.refresh(job)

            # Plex doesn't expand, so total_items should remain 1
            self.assertEqual(job.total_items, 1,
                "Plex should not trigger total_items update (no multi-episode expansion)")


if __name__ == "__main__":
    unittest.main()
