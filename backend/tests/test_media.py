import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from models.episode_order import ShowEpisodePosition
from routers import media as media_router
from routers.media import _attach_episode_order_fields, _resolve_add_overrides, RequestOverrides


def _pos(sid, canon_season, canon_episode, display_season, display_episode, order_key="tvdb:dvd"):
    return ShowEpisodePosition(
        series_tmdb_id=sid, order_key=order_key,
        display_season=display_season, display_episode=display_episode,
        tmdb_episode_id=1,
        tmdb_season_number=canon_season, tmdb_episode_number=canon_episode,
    )


class AttachEpisodeOrderFieldsTests(unittest.TestCase):
    """#174: every surface renders a show's episodes in the ordering the user
    picked for it - _attach_episode_order_fields attaches the order key plus
    display_season_number/display_episode_number from ShowEpisodePosition."""

    def test_episode_gets_display_position(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(
            item, {100: "tvdb:dvd"}, {(100, 4, 12): _pos(100, 4, 12, 3, 8)},
        )
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertEqual(item["display_season_number"], 3)
        self.assertEqual(item["display_episode_number"], 8)

    def test_tvdb_sourced_episode_never_gets_a_translated_position(self) -> None:
        item = {
            "type": "episode", "show_tmdb_id": 100,
            "season_number": 4, "episode_number": 12, "tvdb_sourced": True,
        }
        _attach_episode_order_fields(
            item, {100: "tvdb:dvd"}, {(100, 4, 12): _pos(100, 4, 12, 9, 99)},
        )
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertNotIn("display_season_number", item)
        self.assertNotIn("display_episode_number", item)

    def test_episode_with_no_positions_only_sets_key(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(item, {100: "tmdb:group:abc"}, {})
        self.assertEqual(item["show_episode_order"], "tmdb:group:abc")
        self.assertNotIn("display_season_number", item)

    def test_aired_key_is_a_noop(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(
            item, {100: "tmdb:aired"}, {(100, 4, 12): _pos(100, 4, 12, 3, 8)},
        )
        self.assertNotIn("show_episode_order", item)
        self.assertNotIn("display_season_number", item)

    def test_episode_with_no_key_is_a_noop(self) -> None:
        item = {"type": "episode", "show_tmdb_id": 100, "season_number": 4, "episode_number": 12}
        _attach_episode_order_fields(item, {}, {})
        self.assertNotIn("show_episode_order", item)

    def test_season_list_item_picks_lowest_episode_numbers_display_season(self) -> None:
        item = {"type": "series", "tmdb_id": 100, "season_number": 4}
        _attach_episode_order_fields(item, {100: "tvdb:dvd"}, {
            (100, 4, 5): _pos(100, 4, 5, 3, 1),
            (100, 4, 1): _pos(100, 4, 1, 3, 9),
            (100, 5, 1): _pos(100, 5, 1, 4, 1),
        })
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        # Lowest canonical episode in season 4 is episode 1 -> display season 3.
        self.assertEqual(item["display_season_number"], 3)
        self.assertNotIn("display_episode_number", item)

    def test_whole_show_item_sets_key_without_season_fields(self) -> None:
        item = {"type": "series", "tmdb_id": 100}
        _attach_episode_order_fields(item, {100: "tvdb:dvd"}, {(100, 4, 12): _pos(100, 4, 12, 3, 8)})
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertNotIn("display_season_number", item)

    def test_movie_item_is_a_noop(self) -> None:
        item = {"type": "movie", "tmdb_id": 550}
        _attach_episode_order_fields(item, {550: "tvdb:dvd"}, {})
        self.assertNotIn("show_episode_order", item)
class ResolveAddOverridesTests(unittest.TestCase):
    """The customize-on-add popup lets an admin override the root folder/
    quality profile/tags/season-folder for a single Radarr/Sonarr add - this
    must only ever apply for an admin, regardless of the *_customize_on_add
    settings (those only control whether the frontend shows the picker)."""

    def test_admin_overrides_are_honored(self) -> None:
        overrides = RequestOverrides(root_folder="/movies-4k")
        self.assertIs(_resolve_add_overrides(overrides, True), overrides)

    def test_non_admin_overrides_are_ignored(self) -> None:
        overrides = RequestOverrides(root_folder="/movies-4k")
        self.assertIsNone(_resolve_add_overrides(overrides, False))

    def test_no_overrides_sent_is_a_noop_for_an_admin(self) -> None:
        self.assertIsNone(_resolve_add_overrides(None, True))

    def test_no_overrides_sent_is_a_noop_for_a_non_admin(self) -> None:
        self.assertIsNone(_resolve_add_overrides(None, False))


class _FakeExecResult:
    def all(self):
        return []

    def scalars(self):
        return self

    def first(self):
        return None


class TmdbListStudioParamsTests(unittest.IsolatedAsyncioTestCase):
    """/media/tmdb/list with network=/company= drives the /network/{id} and
    /studio/{id} browse pages: network is TV-only and the vote-count floor is
    dropped so a whole back-catalogue shows."""

    async def _call(self, **kwargs):
        base = dict(
            type=MediaType.movie, category="popular", page=1, genre=[], year=[],
            min_rating=None, status=None, original_language=None, network=None,
            company=None, collection=[], watch=[], arr=[], in_list=[],
            db=SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult())),
            current_user=SimpleNamespace(id=7),
        )
        base.update(kwargs)
        discover_shows = AsyncMock(return_value={"results": [], "page": 1, "total_pages": 1, "total_results": 0})
        discover_movies = AsyncMock(return_value={"results": [], "page": 1, "total_pages": 1, "total_results": 0})
        with patch.object(media_router, "get_user_tmdb_key", AsyncMock(return_value="k")), \
             patch.object(media_router, "check_tmdb_key", lambda _k: True), \
             patch.object(media_router, "get_user_metadata_language", AsyncMock(return_value=None)), \
             patch.object(media_router, "enrich_with_state", AsyncMock(side_effect=lambda db, uid, items: items)), \
             patch.object(media_router.tmdb, "discover_shows", discover_shows), \
             patch.object(media_router.tmdb, "discover_movies", discover_movies):
            await media_router.get_tmdb_list(**base)
        return discover_shows, discover_movies

    async def test_network_forces_series_and_zero_vote_floor(self) -> None:
        shows, movies = await self._call(type=MediaType.movie, network=9)
        movies.assert_not_called()
        shows.assert_awaited_once()
        kw = shows.await_args.kwargs
        self.assertEqual(kw["with_networks"], 9)
        self.assertEqual(kw["vote_count_min"], 0)

    async def test_company_passes_with_companies_to_movies(self) -> None:
        shows, movies = await self._call(type=MediaType.movie, company=1957)
        movies.assert_awaited_once()
        kw = movies.await_args.kwargs
        self.assertEqual(kw["with_companies"], 1957)
        self.assertEqual(kw["vote_count_min"], 0)

    async def test_plain_list_keeps_default_vote_floor(self) -> None:
        shows, movies = await self._call(type=MediaType.series, genre=["Drama"])
        shows.assert_awaited_once()
        self.assertIsNone(shows.await_args.kwargs["vote_count_min"])
        self.assertIsNone(shows.await_args.kwargs["with_networks"])


if __name__ == "__main__":
    unittest.main()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class SearchMetadataLanguageTests(unittest.IsolatedAsyncioTestCase):
    """#417: search results showed TMDB's English title under a French poster."""

    def _fake_db(self, local_rows=()):
        db = SimpleNamespace()
        db.execute = AsyncMock(side_effect=lambda *_a, **_k: _Rows(list(local_rows)))
        return db

    async def _search(self, lang, db=None, local_title=None):
        tmdb_show = {
            "id": 42, "name": "Surveillant !" if lang == "fr" else "Discipline and Punish",
            "overview": "fr overview" if lang == "fr" else "en overview",
            "poster_path": "/p.jpg", "first_air_date": "2026-01-01",
        }
        search_shows = AsyncMock(return_value={"results": [tmdb_show], "total_pages": 1, "total_results": 1})
        with (
            patch.object(media_router, "get_user_metadata_language", AsyncMock(return_value=lang)),
            patch.object(media_router, "get_user_tmdb_key", AsyncMock(return_value="key")),
            patch.object(media_router, "check_tmdb_key", return_value=True),
            patch.object(media_router, "enrich_with_state", AsyncMock()),
            patch.object(media_router.tmdb, "search_shows", search_shows),
        ):
            result = await media_router.search_media(
                q="Surveillant", type="series", year=None, page=1, in_library=False,
                db=db or self._fake_db(), current_user=SimpleNamespace(id=1),
            )
        return result, search_shows

    async def test_language_is_passed_to_tmdb(self):
        result, search_shows = await self._search("fr")
        self.assertEqual(search_shows.await_args.kwargs["language"], "fr")
        self.assertEqual(result["results"][0]["title"], "Surveillant !")

    async def test_no_language_leaves_the_default_call_alone(self):
        _, search_shows = await self._search(None)
        self.assertIsNone(search_shows.await_args.kwargs["language"])

    async def test_library_item_takes_the_localized_title(self):
        item = {"id": 7, "title": "Discipline and Punish", "overview": "en overview", "poster_path": None}
        db = self._fake_db()
        with patch.object(media_router, "format_media", return_value=item):
            # First query is the local-library lookup; later ones find nothing.
            db.execute = AsyncMock(side_effect=[
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [
                    SimpleNamespace(tmdb_id=42, media_type=MediaType.series, id=7),
                ])),
                _Rows([]),
                _Rows([]),
            ])
            with patch.object(media_router, "get_media_translations", AsyncMock(return_value={})):
                result, _ = await self._search("fr", db=db)
        self.assertEqual(result["results"][0]["title"], "Surveillant !")
        self.assertTrue(result["results"][0]["in_library"])

    async def test_stored_translation_wins_for_library_items(self):
        item = {"id": 7, "title": "Discipline and Punish", "overview": "x", "poster_path": None}
        db = self._fake_db()
        with patch.object(media_router, "format_media", return_value=item):
            db.execute = AsyncMock(side_effect=[
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [
                    SimpleNamespace(tmdb_id=42, media_type=MediaType.series, id=7),
                ])),
                _Rows([]),
                _Rows([]),
            ])
            stored = {7: {"title": "Surveillant (stocke)", "overview": None, "tagline": None, "poster_path": None}}
            with patch.object(media_router, "get_media_translations", AsyncMock(return_value=stored)):
                result, _ = await self._search("fr", db=db)
        self.assertEqual(result["results"][0]["title"], "Surveillant (stocke)")


from sqlalchemy import select as _sa_select
from sqlalchemy.dialects.postgresql import JSONB as _JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker as _async_sessionmaker, create_async_engine as _create_async_engine
from sqlalchemy.ext.compiler import compiles as _compiles
from sqlalchemy.pool import StaticPool as _StaticPool

from models.collection import Collection
from models.media import Media
from models.show import Show as ShowModel


@_compiles(_JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


class _CollectionDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import models  # noqa: F401
        from models.base import Base

        self.engine = _create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=_StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = _async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

    async def _add_show(self, db, tmdb_id: int, episode_count: int = 10) -> ShowModel:
        # "Ended" keeps enrich_with_state off its live TMDB aired-count lookup.
        show = ShowModel(
            tmdb_id=tmdb_id, title=f"Show {tmdb_id}", status="Ended",
            tmdb_data={"seasons": [{"season_number": 1, "episode_count": episode_count}]},
        )
        db.add(show)
        await db.flush()
        return show


class EnrichCollectedAsSeriesTests(_CollectionDB):
    """A show collected as a whole (a series-level Collection row, as Stremio
    library sync creates) is in the library even with no episodes collected -
    list_shows already counts it, so its card mustn't read 0% / not in library."""

    async def _enrich(self, db, items):
        with patch.object(media_router, "check_tmdb_key", return_value=False):
            return await media_router.enrich_with_state(db, 7, items)

    async def test_series_level_row_only_is_in_library_at_zero_pct(self) -> None:
        async with self.Session() as db:
            await self._add_show(db, 100)
            series = Media(tmdb_id=100, media_type=MediaType.series, title="Show 100")
            db.add(series)
            await db.flush()
            db.add(Collection(user_id=7, media_id=series.id))
            await db.commit()

            [item] = await self._enrich(db, [{"tmdb_id": 100, "type": "series"}])

        self.assertTrue(item["in_library"])
        self.assertTrue(item["collected_as_series"])
        self.assertEqual(item["collection_pct"], 0)

    async def test_episode_collected_show_unchanged(self) -> None:
        async with self.Session() as db:
            show = await self._add_show(db, 200)
            for ep in range(1, 6):
                episode = Media(
                    tmdb_id=2000 + ep, media_type=MediaType.episode, title=f"E{ep}",
                    show_id=show.id, season_number=1, episode_number=ep,
                )
                db.add(episode)
                await db.flush()
                db.add(Collection(user_id=7, media_id=episode.id))
            await db.commit()

            [item] = await self._enrich(db, [{"tmdb_id": 200, "type": "series"}])

        self.assertTrue(item["in_library"])
        self.assertFalse(item["collected_as_series"])
        self.assertEqual(item["collection_pct"], 50)

    async def test_season_item_ignores_series_level_row(self) -> None:
        async with self.Session() as db:
            await self._add_show(db, 300)
            series = Media(tmdb_id=300, media_type=MediaType.series, title="Show 300")
            db.add(series)
            await db.flush()
            db.add(Collection(user_id=7, media_id=series.id))
            await db.commit()

            [item] = await self._enrich(db, [{"tmdb_id": 300, "type": "series", "season_number": 1}])

        self.assertFalse(item["in_library"])
        self.assertNotIn("collected_as_series", item)
        self.assertEqual(item["collection_pct"], 0)

    async def test_other_users_series_row_does_not_count(self) -> None:
        async with self.Session() as db:
            await self._add_show(db, 400)
            series = Media(tmdb_id=400, media_type=MediaType.series, title="Show 400")
            db.add(series)
            await db.flush()
            db.add(Collection(user_id=8, media_id=series.id))
            await db.commit()

            [item] = await self._enrich(db, [{"tmdb_id": 400, "type": "series"}])

        self.assertFalse(item["in_library"])
        self.assertFalse(item["collected_as_series"])


class UncollectShowSeriesLevelTests(_CollectionDB):
    """Removing a show from the collection must drop its series-level row too,
    and push it, or a Stremio-collected show could never be uncollected."""

    async def test_removes_series_and_episode_rows_and_pushes_both(self) -> None:
        async with self.Session() as db:
            show = await self._add_show(db, 500)
            series = Media(tmdb_id=500, media_type=MediaType.series, title="Show 500")
            episode = Media(
                tmdb_id=5001, media_type=MediaType.episode, title="E1",
                show_id=show.id, season_number=1, episode_number=1,
            )
            other = Media(tmdb_id=600, media_type=MediaType.series, title="Other")
            db.add_all([series, episode, other])
            await db.flush()
            db.add_all([
                Collection(user_id=7, media_id=series.id),
                Collection(user_id=7, media_id=episode.id),
                Collection(user_id=7, media_id=other.id),
            ])
            await db.commit()

            push = AsyncMock()
            with patch.object(media_router, "_push_collection_change", push):
                await media_router.uncollect_show(tmdb_id=500, db=db, current_user=SimpleNamespace(id=7))

            remaining = (await db.execute(_sa_select(Collection.media_id))).scalars().all()

        self.assertEqual(remaining, [other.id])
        push.assert_awaited_once()
        self.assertEqual(push.await_args.args[2], {series.id, episode.id})
        self.assertEqual(push.await_args.kwargs, {"added": False})

    async def test_series_row_removed_without_local_show_row(self) -> None:
        async with self.Session() as db:
            series = Media(tmdb_id=700, media_type=MediaType.series, title="No Show Row")
            db.add(series)
            await db.flush()
            db.add(Collection(user_id=7, media_id=series.id))
            await db.commit()

            with patch.object(media_router, "_push_collection_change", AsyncMock()):
                await media_router.uncollect_show(tmdb_id=700, db=db, current_user=SimpleNamespace(id=7))

            remaining = (await db.execute(_sa_select(Collection.media_id))).scalars().all()

        self.assertEqual(remaining, [])
