import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from pydantic import ValidationError

from schemas import MAX_EXTERNAL_LINKS, ExternalLink, UserSettings


class ExternalLinkSchemaTests(unittest.TestCase):
    def test_accepts_http_and_app_schemes_with_known_placeholders(self) -> None:
        for template in (
            "stremio:///detail/{type}/{imdb_id}",
            "https://web.stremio.com/#/detail/{type}/{imdb_id}",
            "https://mdblist.com/{type}/{imdb_id}",
            "https://example.com/{tmdb_id}?tvdb={tvdb_id}",
        ):
            with self.subTest(template=template):
                self.assertEqual(ExternalLink(label="X", url_template=template).url_template, template)

    def test_strips_whitespace(self) -> None:
        link = ExternalLink(label="  Stremio ", url_template=" stremio:///detail/{type}/{imdb_id} ")
        self.assertEqual(link.label, "Stremio")
        self.assertEqual(link.url_template, "stremio:///detail/{type}/{imdb_id}")

    def test_rejects_script_schemes(self) -> None:
        for template in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,x", "vbscript:x"):
            with self.subTest(template=template), self.assertRaises(ValidationError):
                ExternalLink(label="X", url_template=template)

    def test_rejects_missing_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            ExternalLink(label="X", url_template="/relative/{imdb_id}")

    def test_rejects_unknown_placeholder(self) -> None:
        with self.assertRaisesRegex(ValidationError, r"\{show_id\}"):
            ExternalLink(label="X", url_template="https://example.com/{show_id}")

    def test_rejects_blank_label(self) -> None:
        with self.assertRaises(ValidationError):
            ExternalLink(label="   ", url_template="https://example.com/{imdb_id}")

    def test_settings_caps_list_length(self) -> None:
        link = {"label": "X", "url_template": "https://example.com/{imdb_id}"}
        UserSettings(external_links=[link] * MAX_EXTERNAL_LINKS)
        with self.assertRaises(ValidationError):
            UserSettings(external_links=[link] * (MAX_EXTERNAL_LINKS + 1))

    def test_settings_dump_stores_plain_dicts(self) -> None:
        settings = UserSettings(external_links=[{"label": "Stremio", "url_template": "stremio:///detail/{type}/{imdb_id}"}])
        self.assertEqual(
            settings.model_dump(exclude_unset=True),
            {"external_links": [{"label": "Stremio", "url_template": "stremio:///detail/{type}/{imdb_id}"}]},
        )


if __name__ == "__main__":
    unittest.main()
