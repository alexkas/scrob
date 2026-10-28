import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core import stremio
from models.base import MediaType
from models.connections import MediaServerConnection
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.show import Show
from schemas import MediaServerConnectionResponse, MediaServerConnectionUpdate, StremioLinkPollRequest
from routers import auth
from routers.history import _push_watch_state, dismiss_continue_watching
from routers.sync import (
    _apply_nuvio_watch_history,
    _pull_stremio_items,
    _push_stremio_connection,
    _rewind_stremio_progress,
    _stremio_notified_videos,
    _stremio_records,
    _stremio_same_item,
    _stremio_sorted_videos,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """JSONB is PostgreSQL-only and has no SQLite rendering of its own."""
    return "JSON"


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class StremioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_poll_treats_code_101_as_pending(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v2/read")
            self.assertEqual(request.url.params["type"], "Read")
            self.assertEqual(request.url.params["code"], "ABCD")
            return httpx.Response(
                200,
                json={"error": {"code": 101, "message": "Invalid or expired token"}},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            stremio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            auth_key = await stremio.read_link_code(" abcd ")

        self.assertIsNone(auth_key)

    async def test_datastore_put_uses_official_envelope(self) -> None:
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"result": {"success": True}})

        transport = httpx.MockTransport(handler)
        with patch.object(
            stremio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await stremio.datastore_put("secret-auth-key", [{"_id": "tt0133093"}])

        self.assertEqual(
            requests,
            [
                {
                    "authKey": "secret-auth-key",
                    "collection": "libraryItem",
                    "changes": [{"_id": "tt0133093"}],
                }
            ],
        )


class StremioBitfieldTests(unittest.TestCase):
    def test_watched_bitfield_round_trip_uses_little_endian_bits(self) -> None:
        video_ids = [f"tt0944947:1:{episode}" for episode in range(1, 12)]
        watched = {video_ids[0], video_ids[7], video_ids[8], video_ids[10]}

        serialized = stremio.encode_watched_bitfield(watched, video_ids)

        self.assertIsNotNone(serialized)
        self.assertEqual(stremio.decode_watched_bitfield(serialized, video_ids), watched)

    def test_watched_bitfield_keeps_alignment_when_episodes_are_prepended(self) -> None:
        old_video_ids = ["tt1234567:1:2", "tt1234567:1:3"]
        serialized = stremio.encode_watched_bitfield({old_video_ids[1]}, old_video_ids)
        current_video_ids = ["tt1234567:1:1", *old_video_ids]

        self.assertEqual(
            stremio.decode_watched_bitfield(serialized, current_video_ids),
            {"tt1234567:1:3"},
        )

    def test_malformed_watched_bitfield_is_ignored(self) -> None:
        self.assertEqual(
            stremio.decode_watched_bitfield("invalid", ["tt1234567:1:1"]),
            set(),
        )


class StremioSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Library series pull Cinemeta last-videos for notifications; none by default.
        patcher = patch.object(stremio, "get_cinemeta_last_videos", AsyncMock(return_value={}))
        self.last_videos = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_incremental_pull_consumes_datastore_meta_tuples(self) -> None:
        cursor = datetime.now(timezone.utc).replace(tzinfo=None)
        recent_ms = int((cursor - timedelta(minutes=1)).replace(tzinfo=timezone.utc).timestamp() * 1000)
        old_ms = int((cursor - timedelta(minutes=10)).replace(tzinfo=timezone.utc).timestamp() * 1000)
        connection = SimpleNamespace(
            token="auth-key",
            stremio_full_sync_done=True,
            stremio_pull_cursor_at=cursor,
        )

        with (
            patch.object(
                stremio,
                "datastore_meta",
                AsyncMock(return_value=[["tt-recent", recent_ms], ["tt-old", old_ms], ["broken"]]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=[{"_id": "tt-recent"}]),
            ) as datastore_get,
        ):
            items, complete_snapshot, started_at, progress_only_ids = await _pull_stremio_items(
                connection,
                full_resync=False,
            )

        self.assertEqual(items, [{"_id": "tt-recent"}])
        self.assertFalse(complete_snapshot)
        self.assertGreaterEqual(started_at, cursor)
        self.assertEqual(progress_only_ids, set())
        datastore_get.assert_awaited_once_with("auth-key", ids=["tt-recent"])

    async def test_incremental_pull_always_refetches_library_series(self) -> None:
        cursor = datetime.now(timezone.utc).replace(tzinfo=None)
        recent_ms = int((cursor - timedelta(minutes=1)).replace(tzinfo=timezone.utc).timestamp() * 1000)
        connection = SimpleNamespace(
            token="auth-key",
            stremio_full_sync_done=True,
            stremio_pull_cursor_at=cursor,
        )

        with (
            patch.object(stremio, "datastore_meta", AsyncMock(return_value=[["tt-recent", recent_ms]])),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[])) as datastore_get,
        ):
            *_, progress_only_ids = await _pull_stremio_items(
                connection,
                full_resync=False,
                always_ids={"tt23112594", "tt-recent"},
            )

        datastore_get.assert_awaited_once_with("auth-key", ids=["tt-recent", "tt23112594"])
        # tt-recent changed, so it gets the full treatment.
        self.assertEqual(progress_only_ids, {"tt23112594"})

    async def test_series_records_use_cinemeta_episode_order_and_progress(self) -> None:
        videos = [
            {"id": "tt0944947:2:1", "season": 2, "episode": 1, "name": "S2E1"},
            {"id": "tt0944947:1:2", "season": 1, "episode": 2, "name": "S1E2"},
            {"id": "tt0944947:1:1", "season": 1, "episode": 1, "name": "S1E1"},
        ]
        sorted_ids = [video["id"] for video in _stremio_sorted_videos({"videos": videos})]
        watched = stremio.encode_watched_bitfield(
            {"tt0944947:1:1", "tt0944947:2:1"},
            sorted_ids,
        )
        item = {
            "_id": "tt0944947",
            "type": "series",
            "name": "Game of Thrones",
            "state": {
                "watched": watched,
                "video_id": "tt0944947:1:2",
                "timeOffset": 120_000,
                "duration": 3_600_000,
                "lastWatched": "2026-07-26T12:00:00Z",
            },
        }

        with patch.object(
            stremio,
            "get_cinemeta_series",
            AsyncMock(return_value={"videos": videos}),
        ):
            library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual([record["content_id"] for record in library], ["tt0944947"])
        self.assertEqual(
            {(record["season"], record["episode"]) for record in watched_records},
            {(1, 1), (2, 1)},
        )
        self.assertEqual((progress[0]["season"], progress[0]["episode"]), (1, 2))
        self.assertEqual(progress[0]["position"], 120_000)
        self.assertEqual(removed, set())

    async def test_temporary_removed_movie_still_emits_watched_state(self) -> None:
        item = {
            "_id": "tt0133093",
            "type": "movie",
            "name": "The Matrix",
            "removed": True,
            "temp": True,
            "state": {
                "timesWatched": 1,
                "lastWatched": "2026-07-31T20:00:00Z",
            },
        }

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(library, [])
        self.assertEqual(
            watched_records,
            [
                {
                    "content_id": "tt0133093",
                    "content_type": "movie",
                    "title": "The Matrix",
                    "watched_at": 1785528000000,
                }
            ],
        )
        self.assertEqual(progress, [])
        self.assertEqual([clear["content_id"] for clear in clears], ["tt0133093"])
        self.assertEqual(removed, {"tt0133093"})

    async def test_temporary_removed_series_still_emits_episode_state(self) -> None:
        videos = [
            {"id": "tt0944947:1:1", "season": 1, "episode": 1, "name": "S1E1"},
            {"id": "tt0944947:1:2", "season": 1, "episode": 2, "name": "S1E2"},
        ]
        watched = stremio.encode_watched_bitfield(
            {"tt0944947:1:1"},
            [video["id"] for video in videos],
        )
        item = {
            "_id": "tt0944947",
            "type": "series",
            "name": "Game of Thrones",
            "removed": True,
            "temp": True,
            "state": {
                "watched": watched,
                "video_id": "tt0944947:1:2",
                "timeOffset": 120_000,
                "duration": 3_600_000,
                "lastWatched": "2026-07-31T20:00:00Z",
            },
        }

        with patch.object(
            stremio,
            "get_cinemeta_series",
            AsyncMock(return_value={"videos": videos}),
        ):
            library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(library, [])
        self.assertEqual(
            [(record["season"], record["episode"]) for record in watched_records],
            [(1, 1)],
        )
        self.assertEqual(
            [(record["season"], record["episode"]) for record in progress],
            [(1, 2)],
        )
        self.assertEqual(removed, {"tt0944947"})

    async def test_tmdb_catalog_series_still_resolves_episodes_via_cinemeta(self) -> None:
        # Series added through a TMDB-based catalog addon carry a tmdb:<id>
        # `_id`, but Stremio still tracks per-episode watch state using the
        # IMDb-prefixed episode ids returned by Cinemeta.
        videos = [
            {"id": "tt34866681:1:1", "season": 1, "episode": 1, "name": "S1E1"},
            {"id": "tt34866681:1:2", "season": 1, "episode": 2, "name": "S1E2"},
        ]
        watched = stremio.encode_watched_bitfield(
            {"tt34866681:1:1"},
            [video["id"] for video in videos],
        )
        item = {
            "_id": "tmdb:278624",
            "type": "series",
            "name": "Lucky",
            "removed": True,
            "temp": True,
            "state": {
                "watched": watched,
                "video_id": "tt34866681:1:2",
                "timeOffset": 1,
                "duration": 2_850_216,
                "lastWatched": "2026-07-31T20:35:41.332Z",
            },
        }

        with patch.object(
            stremio,
            "get_cinemeta_series",
            AsyncMock(return_value={"videos": videos}),
        ) as get_series:
            library, watched_records, progress, clears, removed = await _stremio_records([item])

        get_series.assert_awaited_once_with("tt34866681")
        self.assertEqual(library, [])
        self.assertEqual(
            [(record["content_id"], record["season"], record["episode"]) for record in watched_records],
            [("tmdb:278624", 1, 1)],
        )
        self.assertEqual(
            [(record["content_id"], record["season"], record["episode"]) for record in progress],
            [("tmdb:278624", 1, 2)],
        )
        self.assertEqual(removed, {"tmdb:278624"})

    async def test_movie_reset_to_zero_emits_clear(self) -> None:
        item = {
            "_id": "tt0133093",
            "type": "movie",
            "name": "The Matrix",
            "_mtime": "2026-08-01T10:00:00Z",
            "state": {
                "timeOffset": 0,
                "duration": 8_160_000,
                "lastWatched": "2026-07-31T20:00:00Z",
            },
        }

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(progress, [])
        self.assertEqual(
            clears,
            [
                {
                    "content_id": "tt0133093",
                    "content_type": "movie",
                    "title": "The Matrix",
                    # The later of lastWatched and _mtime.
                    "cleared_at": 1785578400000,
                }
            ],
        )

    async def test_movie_removed_from_library_emits_clear_despite_offset(self) -> None:
        item = {
            "_id": "tt0133093",
            "type": "movie",
            "name": "The Matrix",
            "removed": True,
            "state": {"timeOffset": 600_000, "duration": 8_160_000},
        }

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(progress, [])
        self.assertEqual([clear["content_id"] for clear in clears], ["tt0133093"])
        self.assertIsNone(clears[0]["cleared_at"])
        self.assertEqual(removed, {"tt0133093"})

    async def test_movie_in_continue_watching_emits_no_clear(self) -> None:
        item = {
            "_id": "tt0133093",
            "type": "movie",
            "name": "The Matrix",
            "state": {"timeOffset": 600_000, "duration": 8_160_000},
        }

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(len(progress), 1)
        self.assertEqual(clears, [])

    async def test_series_in_continue_watching_keeps_only_current_episode(self) -> None:
        videos = [
            {"id": "tt0944947:1:3", "season": 1, "episode": 3},
            {"id": "tt0944947:1:4", "season": 1, "episode": 4},
        ]
        item = {
            "_id": "tt0944947",
            "type": "series",
            "name": "Game of Thrones",
            "state": {
                "video_id": "tt0944947:1:4",
                "timeOffset": 120_000,
                "duration": 3_600_000,
                "lastWatched": "2026-07-26T12:00:00Z",
            },
        }

        with patch.object(stremio, "get_cinemeta_series", AsyncMock(return_value={"videos": videos})):
            library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual([(record["season"], record["episode"]) for record in progress], [(1, 4)])
        self.assertEqual(len(clears), 1)
        self.assertEqual((clears[0]["keep_season"], clears[0]["keep_episode"]), (1, 4))

    async def test_series_reset_to_zero_clears_whole_show(self) -> None:
        videos = [{"id": "tt0944947:1:4", "season": 1, "episode": 4}]
        item = {
            "_id": "tt0944947",
            "type": "series",
            "name": "Game of Thrones",
            "state": {"video_id": "tt0944947:1:4", "timeOffset": 0, "duration": 3_600_000},
        }

        with patch.object(stremio, "get_cinemeta_series", AsyncMock(return_value={"videos": videos})):
            library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(progress, [])
        self.assertEqual(len(clears), 1)
        self.assertNotIn("keep_season", clears[0])

    def _visionquest(self, **overrides) -> dict:
        # Real values from a Stremio library: last watched 2026-10-22,
        # S1E5/E6 released 2026-10-28 05:00Z, S1E7 still upcoming.
        item = {
            "_id": "tt23112594",
            "type": "series",
            "removed": False,
            "temp": False,
            "_mtime": "2026-10-22T19:49:52.927Z",
            "state": {"video_id": "tt23112594:1:5", "timeOffset": 1, "lastWatched": "2026-10-22T19:49:52.253Z"},
        }
        return {**item, **overrides}

    _VISIONQUEST_VIDEOS = [
        {"id": "tt23112594:1:4", "season": 1, "episode": 4, "released": "2026-10-21T05:00:00.000Z"},
        {"id": "tt23112594:1:6", "season": 1, "episode": 6, "released": "2026-10-28T05:00:00.000Z"},
        {"id": "tt23112594:1:5", "season": 1, "episode": 5, "released": "2026-10-28T05:00:00.000Z"},
        {"id": "tt23112594:1:7", "season": 1, "episode": 7, "released": "2026-11-04T05:00:00.000Z"},
    ]
    _NOW_MS = 1793167200000  # 2026-10-28T06:00:00Z

    def test_notifications_are_released_after_last_watch_and_not_in_future(self) -> None:
        notified = _stremio_notified_videos(self._visionquest(), self._VISIONQUEST_VIDEOS, self._NOW_MS)
        self.assertEqual([video["id"] for video in notified], ["tt23112594:1:5", "tt23112594:1:6"])

    def test_no_notifications_outside_stremios_rules(self) -> None:
        state = self._visionquest()["state"]
        for name, item in [
            ("temp item", self._visionquest(removed=True, temp=True)),
            ("noNotif", self._visionquest(state={**state, "noNotif": True})),
            ("defaultVideoId", self._visionquest(behaviorHints={"defaultVideoId": "tt23112594:1:1"})),
            ("never watched", self._visionquest(state={**state, "lastWatched": None})),
            ("movie", self._visionquest(type="movie")),
        ]:
            with self.subTest(name):
                self.assertEqual(_stremio_notified_videos(item, self._VISIONQUEST_VIDEOS, self._NOW_MS), [])

    def _library_series(self, **state) -> dict:
        return {
            "_id": "tt23112594",
            "type": "series",
            "name": "VisionQuest",
            "removed": False,
            "temp": False,
            "_mtime": "2021-01-01T00:00:00Z",
            "state": {"video_id": "tt23112594:1:4", "lastWatched": "2021-01-01T00:00:00Z", **state},
        }

    _OLD_RELEASES = [
        {"id": "tt23112594:1:5", "season": 1, "episode": 5, "released": "2022-01-01T00:00:00Z"},
        {"id": "tt23112594:1:6", "season": 1, "episode": 6, "released": "2022-01-08T00:00:00Z"},
        {"id": "tt23112594:1:7", "season": 1, "episode": 7, "released": "2999-01-01T00:00:00Z"},
    ]

    async def test_new_episode_ranks_series_in_continue_watching(self) -> None:
        self.last_videos.return_value = {"tt23112594": self._OLD_RELEASES}
        item = self._library_series(video_id="tt23112594:1:5", timeOffset=1, duration=2_889_344)

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        [record] = progress
        self.assertEqual((record["season"], record["episode"], record["position"]), (1, 5, 1))
        self.assertTrue(record["notified"])
        self.assertEqual(record["sort_at"], 1641600000000)  # newest notification, 2022-01-08
        self.assertEqual(self.last_videos.await_args.args[0], ["tt23112594"])

    async def test_series_with_only_new_episodes_is_mirrored_at_zero(self) -> None:
        self.last_videos.return_value = {"tt23112594": self._OLD_RELEASES}
        item = self._library_series(timeOffset=0)

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        [record] = progress
        self.assertEqual((record["season"], record["episode"]), (1, 5))
        self.assertEqual((record["position"], record["duration"]), (0, 0))
        self.assertTrue(record["notified"])
        self.assertEqual([(c.get("keep_season"), c.get("keep_episode")) for c in clears], [(1, 5)])

    async def test_series_without_notifications_or_offset_is_cleared(self) -> None:
        item = self._library_series(timeOffset=0)

        library, watched_records, progress, clears, removed = await _stremio_records([item])

        self.assertEqual(progress, [])
        self.assertEqual(len(clears), 1)
        self.assertNotIn("keep_season", clears[0])

    async def test_unreachable_cinemeta_leaves_notification_rows_alone(self) -> None:
        self.last_videos.side_effect = stremio.StremioAPIError("down")
        idle = self._library_series(timeOffset=0)
        resuming = {**self._library_series(video_id="tt0944947:1:2", timeOffset=600_000, duration=3_600_000),
                    "_id": "tt0944947"}

        library, watched_records, progress, clears, removed = await _stremio_records([idle, resuming])

        # The idle series' membership depends on notifications: no record, no clear.
        self.assertEqual([c["content_id"] for c in clears], ["tt0944947"])
        [record] = progress
        self.assertEqual(record["content_id"], "tt0944947")
        self.assertNotIn("sort_at", record)

    async def test_progress_only_items_skip_library_and_watched_records(self) -> None:
        item = {**self._library_series(timeOffset=1, duration=100, video_id="tt23112594:1:5",
                                       watched="tt23112594:1:1:1:eJwDAAAAAAE="), "removed": True, "temp": True}

        library, watched_records, progress, clears, removed = await _stremio_records(
            [item], progress_only_ids={"tt23112594"},
        )

        self.assertEqual((library, watched_records, removed), ([], [], set()))
        self.assertEqual(len(progress), 1)

    def test_noop_comparison_ignores_only_mtime(self) -> None:
        left = {"_id": "tt0133093", "_mtime": "old", "state": {"timeOffset": 42}, "custom": True}
        same = {**left, "_mtime": "new"}
        changed = {**same, "custom": False}

        self.assertTrue(_stremio_same_item(left, same))
        self.assertFalse(_stremio_same_item(left, changed))

    async def test_full_push_preserves_unrelated_remote_items(self) -> None:
        connection = SimpleNamespace(
            id=44,
            token="auth-key",
            push_collection=True,
            push_watched=False,
            push_playback=False,
            stremio_pushed_library_ids=None,
        )
        local = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "name": "The Matrix",
            "poster": "https://example.test/matrix.jpg",
            "poster_shape": "poster",
        }
        remote_only = {
            "_id": "tt9999999",
            "name": "Remote only",
            "type": "movie",
            "removed": False,
            "temp": False,
            "_ctime": "2026-01-01T00:00:00Z",
            "_mtime": "2026-01-01T00:00:00Z",
            "state": {"customPlaybackState": True},
            "unknownField": {"preserve": True},
        }
        datastore_put = AsyncMock()

        with (
            patch(
                "routers.sync._build_nuvio_library_items",
                AsyncMock(return_value=[local]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=[remote_only]),
            ),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            changed = await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        self.assertEqual(changed, 1)
        pushed = datastore_put.await_args.args[1]
        self.assertEqual([item["_id"] for item in pushed], ["tt0133093"])
        self.assertEqual(remote_only["unknownField"], {"preserve": True})
        self.assertEqual(connection.stremio_pushed_library_ids, ["tt0133093"])

    async def test_remote_removal_requires_prior_scrob_push(self) -> None:
        connection = SimpleNamespace(
            id=45,
            token="auth-key",
            push_collection=True,
            push_watched=False,
            push_playback=False,
            stremio_pushed_library_ids=["tt0133093"],
        )
        remote_items = [
            {
                "_id": "tt0133093",
                "name": "The Matrix",
                "type": "movie",
                "removed": False,
                "temp": False,
                "_ctime": "2026-01-01T00:00:00Z",
                "_mtime": "2026-01-01T00:00:00Z",
                "state": {},
            },
            {
                "_id": "tt9999999",
                "name": "Remote only",
                "type": "movie",
                "removed": False,
                "temp": False,
                "_ctime": "2026-01-01T00:00:00Z",
                "_mtime": "2026-01-01T00:00:00Z",
                "state": {},
            },
        ]
        datastore_put = AsyncMock()

        with (
            patch(
                "routers.sync._build_nuvio_library_items",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=remote_items),
            ),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            changed = await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        self.assertEqual(changed, 1)
        pushed = datastore_put.await_args.args[1]
        self.assertEqual([item["_id"] for item in pushed], ["tt0133093"])
        self.assertTrue(pushed[0]["removed"])
        self.assertEqual(connection.stremio_pushed_library_ids, [])

    async def test_unknown_watch_creates_temporary_item_without_fabricating_date(self) -> None:
        connection = SimpleNamespace(
            id=46,
            token="auth-key",
            push_collection=False,
            push_watched=True,
            push_playback=False,
            stremio_pushed_library_ids=None,
        )
        record = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
            "watched_at": None,
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_watched_items",
                AsyncMock(return_value=[record]),
            ) as build_watched,
            patch(
                "routers.sync._stremio_media_records",
                AsyncMock(return_value={10: {key: record[key] for key in ("content_id", "content_type", "title")}}),
            ),
            patch(
                "routers.sync._latest_watched_at",
                AsyncMock(return_value={10: None}),
            ),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[])),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            changed = await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
                changed_media_ids={10},
                watch_overrides={10: True},
            )

        self.assertEqual(changed, 1)
        build_watched.assert_awaited_once_with(
            ANY,
            7,
            media_ids={10},
            api_key="tmdb-key",
            include_unknown_dates=True,
        )
        pushed = datastore_put.await_args.args[1][0]
        self.assertTrue(pushed["removed"])
        self.assertTrue(pushed["temp"])
        self.assertEqual(pushed["state"]["timesWatched"], 1)
        self.assertIsNone(pushed["state"]["lastWatched"])

    async def test_unwatch_clears_state_without_erasing_last_known_date(self) -> None:
        connection = SimpleNamespace(
            id=47,
            token="auth-key",
            push_collection=False,
            push_watched=True,
            push_playback=False,
            stremio_pushed_library_ids=None,
        )
        record = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
        }
        remote = {
            "_id": "tt0133093",
            "name": "The Matrix",
            "type": "movie",
            "removed": True,
            "temp": True,
            "_ctime": "2026-01-01T00:00:00Z",
            "_mtime": "2026-01-01T00:00:00Z",
            "state": {
                "timesWatched": 1,
                "lastWatched": "2026-01-01T00:00:00Z",
            },
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_watched_items",
                AsyncMock(return_value=[]),
            ),
            patch(
                "routers.sync._stremio_media_records",
                AsyncMock(return_value={10: record}),
            ),
            patch(
                "routers.sync._latest_watched_at",
                AsyncMock(return_value={}),
            ),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[remote])),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
                changed_media_ids={10},
                watch_overrides={10: False},
            )

        pushed = datastore_put.await_args.args[1][0]
        self.assertEqual(pushed["state"]["timesWatched"], 0)
        self.assertEqual(
            pushed["state"]["lastWatched"],
            "2026-01-01T00:00:00Z",
        )

    async def test_progress_without_collection_creates_temporary_item(self) -> None:
        connection = SimpleNamespace(
            id=48,
            token="auth-key",
            push_collection=False,
            push_watched=False,
            push_playback=True,
            stremio_pushed_library_ids=None,
        )
        progress = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
            "position": 120_000,
            "duration": 600_000,
            "last_watched": None,
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_progress_items",
                AsyncMock(return_value=[progress]),
            ),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[])),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        pushed = datastore_put.await_args.args[1][0]
        self.assertTrue(pushed["removed"])
        self.assertTrue(pushed["temp"])
        self.assertEqual(pushed["state"]["timeOffset"], 120_000)
        self.assertEqual(pushed["state"]["duration"], 600_000)

    async def test_progress_push_moves_item_to_now(self) -> None:
        connection = SimpleNamespace(
            id=49,
            token="auth-key",
            push_collection=False,
            push_watched=False,
            push_playback=True,
            stremio_pushed_library_ids=None,
        )
        progress = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
            "position": 120_000,
            "duration": 600_000,
            "last_watched": None,
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_progress_items",
                AsyncMock(return_value=[progress]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=[_remote_library_item("tt0133093", None)]),
            ),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        pushed = datastore_put.await_args.args[1][0]
        self.assertGreater(pushed["_mtime"], "2026-10")


_POSTER_TEMPLATE = "https://openposterdb.example/secret-key/imdb/poster-default/{imdb}.jpg"


def _remote_library_item(content_id: str, poster: str | None) -> dict:
    return {
        "_id": content_id,
        "name": content_id,
        "type": "movie",
        "poster": poster,
        "posterShape": "poster",
        "removed": False,
        "temp": False,
        "_ctime": "2026-01-01T00:00:00Z",
        "_mtime": "2026-01-01T00:00:00Z",
        "state": {},
    }


def _local_library_record(content_id: str) -> dict:
    return {
        "content_id": content_id,
        "content_type": "movie",
        "name": content_id,
        "poster": f"https://image.tmdb.org/{content_id}.jpg",
        "poster_shape": "poster",
    }


class StremioPosterTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def _push(
        self,
        local: list[dict],
        remote: list[dict],
        *,
        template: str | None,
        pushed_ids: list[str] | None,
    ) -> dict[str, dict]:
        connection = SimpleNamespace(
            id=60,
            token="auth-key",
            push_collection=True,
            push_watched=False,
            push_playback=False,
            stremio_pushed_library_ids=pushed_ids,
            stremio_poster_template=template,
        )
        datastore_put = AsyncMock()
        with (
            patch("routers.sync._build_nuvio_library_items", AsyncMock(return_value=local)),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=remote)),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _push_stremio_connection(SimpleNamespace(), connection, 7, api_key="tmdb-key")
        return {
            item["_id"]: item
            for call in datastore_put.await_args_list
            for item in call.args[1]
        }

    async def test_template_applied_to_new_items(self) -> None:
        pushed = await self._push(
            [_local_library_record("tt0133093")],
            [],
            template=_POSTER_TEMPLATE,
            pushed_ids=None,
        )

        self.assertEqual(
            pushed["tt0133093"]["poster"],
            "https://openposterdb.example/secret-key/imdb/poster-default/tt0133093.jpg",
        )

    async def test_template_applied_to_previously_pushed_items(self) -> None:
        pushed = await self._push(
            [_local_library_record("tt0133093")],
            [_remote_library_item("tt0133093", "https://image.tmdb.org/old.jpg")],
            template=_POSTER_TEMPLATE,
            pushed_ids=["tt0133093"],
        )

        self.assertEqual(
            pushed["tt0133093"]["poster"],
            "https://openposterdb.example/secret-key/imdb/poster-default/tt0133093.jpg",
        )

    async def test_template_applied_to_user_added_items(self) -> None:
        # tt0133093 is also in the Scrob collection; tt0000002 exists only in
        # Stremio; tt0000003 was removed from the library (history only).
        removed = _remote_library_item("tt0000003", None)
        removed["removed"] = True
        pushed = await self._push(
            [_local_library_record("tt0133093")],
            [
                _remote_library_item("tt0133093", "https://user.example/poster.jpg"),
                _remote_library_item("tt0000002", "https://user.example/other.jpg"),
                removed,
            ],
            template=_POSTER_TEMPLATE,
            pushed_ids=[],
        )

        for content_id in ("tt0133093", "tt0000002", "tt0000003"):
            self.assertEqual(
                pushed[content_id]["poster"],
                f"https://openposterdb.example/secret-key/imdb/poster-default/{content_id}.jpg",
            )
        self.assertTrue(pushed["tt0000003"]["removed"])

    async def test_poster_rewrite_keeps_continue_watching_order(self) -> None:
        # Stremio sorts by _mtime; stamping "now" on every rewritten poster
        # tied the whole library and shuffled Continue Watching.
        older = _remote_library_item("tt0000001", "https://image.tmdb.org/a.jpg")
        older["_mtime"] = "2026-10-21T04:56:52.097Z"
        newer = _remote_library_item("tt0000002", "https://image.tmdb.org/b.jpg")
        newer["_mtime"] = "2026-10-27T05:27:25.373991234Z"
        pushed = await self._push(
            [],
            [older, newer],
            template=_POSTER_TEMPLATE,
            pushed_ids=[],
        )

        self.assertEqual(pushed["tt0000001"]["_mtime"], "2026-10-21T04:56:52.098Z")
        self.assertEqual(pushed["tt0000002"]["_mtime"], "2026-10-27T05:27:25.374Z")

    async def test_items_already_on_the_template_are_not_rewritten(self) -> None:
        pushed = await self._push(
            [],
            [
                _remote_library_item(
                    "tt0000002",
                    "https://openposterdb.example/secret-key/imdb/poster-default/tt0000002.jpg",
                )
            ],
            template=_POSTER_TEMPLATE,
            pushed_ids=[],
        )

        self.assertEqual(pushed, {})

    async def test_non_imdb_ids_keep_the_tmdb_poster(self) -> None:
        pushed = await self._push(
            [_local_library_record("tmdb:603"), _local_library_record("tmdb:604")],
            [_remote_library_item("tmdb:604", "https://image.tmdb.org/old.jpg")],
            template=_POSTER_TEMPLATE,
            pushed_ids=["tmdb:604"],
        )

        self.assertEqual(pushed["tmdb:603"]["poster"], "https://image.tmdb.org/tmdb:603.jpg")
        self.assertNotIn("tmdb:604", pushed)

    async def test_unset_template_keeps_current_behavior(self) -> None:
        pushed = await self._push(
            [_local_library_record("tt0133093"), _local_library_record("tt0000001")],
            [_remote_library_item("tt0000001", "https://image.tmdb.org/old.jpg")],
            template=None,
            pushed_ids=["tt0000001"],
        )

        self.assertEqual(pushed["tt0133093"]["poster"], "https://image.tmdb.org/tt0133093.jpg")
        self.assertNotIn("tt0000001", pushed)

    def test_connection_response_masks_poster_template(self) -> None:
        response = MediaServerConnectionResponse.model_validate(
            {
                "id": 1,
                "user_id": 7,
                "type": "stremio",
                "name": "Stremio",
                "url": stremio.DEFAULT_URL,
                "token": "secret-auth-key",
                "stremio_poster_template": _POSTER_TEMPLATE,
                "created_at": datetime(2026, 7, 31),
            }
        )

        self.assertEqual(response.stremio_poster_template, "https://openposterdb.example/…/{imdb}.jpg")
        self.assertNotIn("secret-key", response.model_dump_json())

    async def _update(self, connection: SimpleNamespace, template: str):
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: connection)),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )
        return await auth.update_connection(
            connection.id,
            MediaServerConnectionUpdate(stremio_poster_template=template),
            db=db,
            current_user=SimpleNamespace(id=7),
        )

    async def test_update_saves_and_clears_template(self) -> None:
        connection = _stremio_connection(stremio_poster_template=None)

        await self._update(connection, f"  {_POSTER_TEMPLATE}  ")
        self.assertEqual(connection.stremio_poster_template, _POSTER_TEMPLATE)

        await self._update(connection, "")
        self.assertIsNone(connection.stremio_poster_template)

    async def test_update_decodes_percent_encoded_placeholder(self) -> None:
        connection = _stremio_connection(stremio_poster_template=None)

        await self._update(connection, _POSTER_TEMPLATE.replace("{imdb}", "%7bimdb%7D"))

        self.assertEqual(connection.stremio_poster_template, _POSTER_TEMPLATE)

    async def test_update_rejects_template_without_placeholder_without_echoing_it(self) -> None:
        connection = _stremio_connection(stremio_poster_template=None)

        with self.assertRaises(HTTPException) as ctx:
            await self._update(connection, "https://openposterdb.example/secret-key/poster.jpg")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn("secret-key", str(ctx.exception.detail))
        self.assertIsNone(connection.stremio_poster_template)


class StremioDismissTests(unittest.IsolatedAsyncioTestCase):
    """Dismissing in Scrob rewinds the item in Stremio (timeOffset = 0)."""

    async def asyncSetUp(self) -> None:
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = SimpleNamespace(id=48, token="auth-key")
        patcher = patch.object(stremio, "get_cinemeta_last_videos", AsyncMock(return_value={}))
        self.last_videos = patcher.start()
        self.addCleanup(patcher.stop)

    async def _movie(self, db) -> Media:
        movie = Media(
            tmdb_id=603, media_type=MediaType.movie, title="The Matrix", tmdb_data={"imdb_id": "tt0133093"},
        )
        db.add(movie)
        await db.commit()
        return movie

    async def _episode(self, db, season: int, episode: int) -> Media:
        show = Show(tmdb_id=1399, title="Game of Thrones", tmdb_data={"external_ids": {"imdb_id": "tt0944947"}})
        db.add(show)
        await db.flush()
        media = Media(
            tmdb_id=63056, media_type=MediaType.episode, title="Episode", show_id=show.id,
            season_number=season, episode_number=episode,
        )
        db.add(media)
        await db.commit()
        return media

    async def _rewind(self, db, media: Media, remote: list[dict]) -> AsyncMock:
        datastore_put = AsyncMock()
        with (
            patch.object(stremio, "datastore_get", AsyncMock(return_value=remote)),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _rewind_stremio_progress(db, self.connection, media_ids={media.id}, api_key="tmdb-key")
        return datastore_put

    async def test_movie_rewind_zeroes_offset_and_keeps_unknown_fields(self) -> None:
        async with self.Session() as db:
            movie = await self._movie(db)
            remote = {
                "_id": "tt0133093",
                "type": "movie",
                "custom": {"kept": True},
                "state": {"timeOffset": 600_000, "duration": 8_160_000, "timesWatched": 2},
            }

            datastore_put = await self._rewind(db, movie, [remote])

        [pushed] = datastore_put.await_args.args[1]
        self.assertEqual(pushed["state"]["timeOffset"], 0)
        self.assertEqual(pushed["state"]["duration"], 8_160_000)
        self.assertEqual(pushed["state"]["timesWatched"], 2)
        self.assertEqual(pushed["custom"], {"kept": True})
        self.assertNotEqual(pushed.get("_mtime"), None)

    async def test_already_rewound_item_is_not_written(self) -> None:
        async with self.Session() as db:
            movie = await self._movie(db)
            remote = {"_id": "tt0133093", "type": "movie", "state": {"timeOffset": 0}}

            datastore_put = await self._rewind(db, movie, [remote])

        self.assertEqual(datastore_put.await_args.args[1], [])

    async def test_episode_rewind_only_when_it_is_stremios_current_video(self) -> None:
        async with self.Session() as db:
            episode = await self._episode(db, 1, 3)
            remote = {
                "_id": "tt0944947",
                "type": "series",
                "state": {"video_id": "tt0944947:1:4", "timeOffset": 120_000},
            }

            datastore_put = await self._rewind(db, episode, [remote])

        # Stremio has already moved on to S1E4; dismissing S1E3 must not rewind it.
        self.assertEqual(datastore_put.await_args.args[1], [])

    async def test_episode_rewind_matches_tmdb_catalog_item(self) -> None:
        async with self.Session() as db:
            episode = await self._episode(db, 1, 4)
            remote = {
                "_id": "tmdb:1399",
                "type": "series",
                "state": {"video_id": "tt0944947:1:4", "timeOffset": 120_000},
            }
            datastore_get = AsyncMock(return_value=[remote])
            datastore_put = AsyncMock()
            with (
                patch.object(stremio, "datastore_get", datastore_get),
                patch.object(stremio, "datastore_put", datastore_put),
            ):
                await _rewind_stremio_progress(db, self.connection, media_ids={episode.id}, api_key="tmdb-key")

        self.assertEqual(datastore_get.await_args.kwargs["ids"], ["tmdb:1399", "tt0944947"])
        [pushed] = datastore_put.await_args.args[1]
        self.assertEqual(pushed["_id"], "tmdb:1399")
        self.assertEqual(pushed["state"]["timeOffset"], 0)

    async def test_dismissing_new_episode_entry_retires_its_notifications(self) -> None:
        """Stremio's own dismiss: RewindLibraryItem + DismissNotificationItem."""
        async with self.Session() as db:
            episode = await self._episode(db, 1, 5)
            self.last_videos.return_value = {"tt0944947": [
                {"id": "tt0944947:1:5", "season": 1, "episode": 5, "released": "2022-01-01T00:00:00Z"},
            ]}
            remote = {
                "_id": "tt0944947",
                "type": "series",
                "removed": False,
                "temp": False,
                "state": {"video_id": "tt0944947:1:4", "timeOffset": 0, "lastWatched": "2021-01-01T00:00:00Z"},
            }

            datastore_put = await self._rewind(db, episode, [remote])

        [pushed] = datastore_put.await_args.args[1]
        self.assertEqual(pushed["state"]["timeOffset"], 0)
        self.assertGreater(pushed["state"]["lastWatched"], "2026-")
        self.assertEqual(pushed["state"]["video_id"], "tt0944947:1:4")

    async def test_resumable_series_with_notifications_gets_both(self) -> None:
        async with self.Session() as db:
            episode = await self._episode(db, 1, 5)
            self.last_videos.return_value = {"tt0944947": [
                {"id": "tt0944947:1:6", "season": 1, "episode": 6, "released": "2022-01-01T00:00:00Z"},
            ]}
            remote = {
                "_id": "tt0944947",
                "type": "series",
                "removed": False,
                "temp": False,
                "state": {"video_id": "tt0944947:1:5", "timeOffset": 1, "lastWatched": "2021-01-01T00:00:00Z"},
            }

            datastore_put = await self._rewind(db, episode, [remote])

        [pushed] = datastore_put.await_args.args[1]
        self.assertEqual(pushed["state"]["timeOffset"], 0)
        self.assertGreater(pushed["state"]["lastWatched"], "2026-")

    async def test_dismiss_endpoint_rewinds_stremio_and_survives_failure(self) -> None:
        async with self.Session() as db:
            movie = await self._movie(db)
            db.add(MediaServerConnection(user_id=7, type="stremio", name="Stremio", url="", token="auth-key"))
            db.add(PlaybackProgress(user_id=7, media_id=movie.id, progress_percent=0.4, progress_seconds=600))
            await db.commit()
            rewind = AsyncMock(side_effect=stremio.StremioAPIError("down"))
            with (
                patch("routers.sync._rewind_stremio_progress", rewind),
                patch("routers.sync._get_effective_tmdb_key", AsyncMock(return_value="tmdb-key")),
            ):
                result = await dismiss_continue_watching(
                    media_id=movie.id, db=db, current_user=SimpleNamespace(id=7),
                )

            self.assertEqual(result, {"status": "ok"})
            self.assertEqual(rewind.await_args.kwargs["media_ids"], {movie.id})
            self.assertEqual((await db.execute(select(PlaybackProgress))).scalars().all(), [])


class StremioCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_inbound_watch_is_persisted_without_a_date(self) -> None:
        movie = Media(
            id=10,
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
        )
        media_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [movie]),
        )
        existing_result = SimpleNamespace(all=lambda: [])
        # get_dedup_window_minutes's UserSettings lookup (#390) - no configured
        # override for this test.
        dedup_window_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        # record_rewatch_progress's own Media lookup for the one new
        # WatchEvent - no-ops since this test isn't exercising rewatch
        # behavior.
        rewatch_media_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[media_result, existing_result, dedup_window_result, rewatch_media_result]),
            add=MagicMock(),
            commit=AsyncMock(),
        )

        added = await _apply_nuvio_watch_history(
            db,
            user_id=7,
            rows=[
                {
                    "content_id": "tt0133093",
                    "content_type": "movie",
                    "watched_at": None,
                }
            ],
            show_map={},
            tmdb_ids={"tt0133093": 603},
            include_unknown_dates=True,
        )

        self.assertEqual(added, {10})
        self.assertIsNone(db.add.call_args.args[0].watched_at)

    async def test_manual_unwatch_is_forwarded_to_stremio(self) -> None:
        connection = SimpleNamespace(id=49, type="stremio")
        settings = SimpleNamespace(
            trakt_push_watched=False,
            mdblist_push_watched=False,
            simkl_push_watched=False,
        )
        connection_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [connection]),
        )
        settings_result = SimpleNamespace(scalar_one_or_none=lambda: settings)
        # _push_watch_state selects (CollectionFile, Collection.media_id) and
        # reads it via .all() directly, not .scalars().all().
        files_result = SimpleNamespace(all=lambda: [])
        movie = Media(id=10, tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
        stremio_media_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [movie]),
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[connection_result, settings_result, files_result, stremio_media_result],
            ),
            commit=AsyncMock(),
        )
        push = AsyncMock()
        with (
            patch(
                "routers.sync._get_effective_tmdb_key",
                AsyncMock(return_value="tmdb-key"),
            ),
            patch("routers.sync._push_stremio_connection", push),
        ):
            await _push_watch_state(
                db,
                user_id=7,
                media_ids=[10],
                watched=False,
            )

        push.assert_awaited_once_with(
            db,
            connection,
            7,
            api_key="tmdb-key",
            changed_media_ids={10},
            watch_overrides={10: False},
        )
        db.commit.assert_awaited_once()


    def test_connection_response_redacts_auth_key(self) -> None:
        response = MediaServerConnectionResponse.model_validate(
            {
                "id": 1,
                "user_id": 7,
                "type": "stremio",
                "name": "Stremio",
                "url": stremio.DEFAULT_URL,
                "token": "secret-auth-key",
                "created_at": datetime(2026, 7, 31),
            }
        )

        self.assertEqual(response.token, "")


def _stremio_connection(**overrides) -> SimpleNamespace:
    fields = dict(
        id=14,
        user_id=7,
        type="stremio",
        name="Stremio",
        url=stremio.DEFAULT_URL,
        token="dead-auth-key",
        server_user_id="old-account-id",
        server_username="old@example.com",
        sync_collection=False,
        sync_watched=True,
        sync_ratings=False,
        sync_playback=False,
        push_watched=True,
        push_collection=True,
        push_playback=False,
        push_ratings=False,
        auto_sync_interval=6.0,
        auto_push_interval=None,
        watchlist_to_radarr=False,
        watchlist_to_sonarr=False,
        watchlist_all_users=False,
        watchlist_monitored_users=None,
        plex_sync_watchlist=False,
        plex_push_watchlist=False,
        created_at=datetime(2026, 6, 1),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class StremioLinkReconnectTests(unittest.IsolatedAsyncioTestCase):
    """A disconnected Stremio connection has no in-place recovery path today
    (unlike a fresh 'add new', poll_stremio_link 409s if one already exists).
    Passing the existing connection's id reconnects it instead."""

    async def test_reconnect_replaces_auth_key_and_preserves_existing_settings(self) -> None:
        existing = _stremio_connection()
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )
        body = StremioLinkPollRequest(code="ABCD", name="Ignored on reconnect", connection_id=14)

        with (
            patch("core.stremio.read_link_code", AsyncMock(return_value="fresh-auth-key")),
            patch(
                "core.stremio.validate_auth_key",
                AsyncMock(return_value={"_id": "new-account-id", "email": "new@example.com"}),
            ),
        ):
            result = await auth.poll_stremio_link(body, db=db, current_user=SimpleNamespace(id=7))

        self.assertEqual(result["status"], "connected")
        self.assertEqual(existing.token, "fresh-auth-key")
        self.assertEqual(existing.server_user_id, "new-account-id")
        self.assertEqual(existing.server_username, "new@example.com")
        # Existing sync/push preferences must survive a reconnect untouched —
        # the request body's defaults only apply to a brand-new connection.
        self.assertFalse(existing.sync_collection)
        self.assertTrue(existing.push_collection)
        self.assertEqual(existing.auto_sync_interval, 6.0)
        db.commit.assert_awaited_once()

    async def test_poll_without_matching_connection_id_still_rejects(self) -> None:
        existing = _stremio_connection()
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)),
        )
        body = StremioLinkPollRequest(code="ABCD", connection_id=None)

        with self.assertRaises(HTTPException) as ctx:
            await auth.poll_stremio_link(body, db=db, current_user=SimpleNamespace(id=7))

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_poll_creates_new_connection_when_none_exists(self) -> None:
        async def _fake_refresh(connection) -> None:
            # Simulate the server-side column defaults a real DB flush would
            # populate — this test never touches a real database.
            connection.id = 99
            connection.created_at = datetime(2026, 8, 1)
            connection.watchlist_to_radarr = False
            connection.watchlist_to_sonarr = False
            connection.watchlist_all_users = False
            connection.watchlist_monitored_users = None
            connection.plex_sync_watchlist = False
            connection.plex_push_watchlist = False

        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
            add=MagicMock(),
            commit=AsyncMock(),
            refresh=AsyncMock(side_effect=_fake_refresh),
        )
        body = StremioLinkPollRequest(code="ABCD", name="My Stremio")

        with (
            patch("core.stremio.read_link_code", AsyncMock(return_value="fresh-auth-key")),
            patch(
                "core.stremio.validate_auth_key",
                AsyncMock(return_value={"_id": "account-id", "email": "me@example.com"}),
            ),
        ):
            result = await auth.poll_stremio_link(body, db=db, current_user=SimpleNamespace(id=7))

        self.assertEqual(result["status"], "connected")
        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        self.assertEqual(added.token, "fresh-auth-key")
        self.assertEqual(added.name, "My Stremio")

    async def test_stremio_sync_dedup_by_media_id_only_skips_existing_watched(self) -> None:
        movie = Media(
            id=10,
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
        )
        media_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [movie]),
        )
        existing_result = SimpleNamespace(
            all=lambda: [(10, datetime(2026, 8, 13, 10, 0, 0))],
        )
        rewatch_media_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[media_result, existing_result, rewatch_media_result]),
            add=MagicMock(),
            commit=AsyncMock(),
        )

        added = await _apply_nuvio_watch_history(
            db,
            user_id=7,
            rows=[
                {
                    "content_id": "tt0133093",
                    "content_type": "movie",
                    "watched_at": datetime(2026, 8, 15, 11, 0, 0).timestamp() * 1000,
                }
            ],
            show_map={},
            tmdb_ids={"tt0133093": 603},
            include_unknown_dates=True,
            dedupe_by_media_id_only=True,
        )

        self.assertEqual(added, set())
        db.add.assert_not_called()

    async def test_nuvio_sync_allows_new_timestamps_by_default(self) -> None:
        movie = Media(
            id=10,
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
        )
        media_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [movie]),
        )
        existing_result = SimpleNamespace(
            all=lambda: [(10, datetime(2026, 8, 13, 10, 0, 0))],
        )
        # get_dedup_window_minutes's UserSettings lookup (#390) - no configured
        # override for this test.
        dedup_window_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        rewatch_media_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[media_result, existing_result, dedup_window_result, rewatch_media_result]),
            add=MagicMock(),
            commit=AsyncMock(),
        )

        expected_watched_at = datetime(2026, 8, 15, 11, 0, 0)
        added = await _apply_nuvio_watch_history(
            db,
            user_id=7,
            rows=[
                {
                    "content_id": "tt0133093",
                    "content_type": "movie",
                    "watched_at": int(
                        datetime(2026, 8, 15, 11, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
                    ),
                }
            ],
            show_map={},
            tmdb_ids={"tt0133093": 603},
            include_unknown_dates=True,
        )

        self.assertEqual(added, {10})
        db.add.assert_called_once()
        event = db.add.call_args.args[0]
        self.assertEqual(event.media_id, 10)
        self.assertEqual(event.watched_at, expected_watched_at)
