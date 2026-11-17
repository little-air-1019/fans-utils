import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fans_utils.profile import ProfileError, load_profile, parse_profile

REPO = Path(__file__).resolve().parent.parent
NMIXX = REPO / "profiles" / "nmixx-uoo7zw9oie.json"


def valid() -> dict:
    return json.loads(NMIXX.read_text(encoding="utf-8"))


class SuppliedProfileTests(unittest.TestCase):
    def test_nmixx_profile_matches_the_supplied_event(self):
        prof = load_profile(NMIXX)
        self.assertEqual(prof.id, "nmixx-uoo7zw9oie")
        self.assertEqual(prof.url, "https://app.fans/community/nmixx/event/uoo7zw9oie")
        self.assertEqual(prof.timezone, "Asia/Seoul")
        self.assertEqual(prof.signup_start, datetime(2026, 9, 22, 10, 0, tzinfo=UTC))  # 19:00 KST = 18:00 Taipei
        self.assertEqual(prof.signup_end, datetime(2026, 9, 22, 10, 10, tzinfo=UTC))
        self.assertEqual((prof.capacity, prof.alternates), (100, 10))
        self.assertEqual(prof.channel, "app-only")
        self.assertEqual(prof.signup_start.isoformat(), "2026-09-22T19:00:00+09:00")


class MalformedProfileTests(unittest.TestCase):
    def assert_rejected(self, data, fragment: str):
        with self.assertRaises(ProfileError) as ctx:
            parse_profile(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_not_an_object(self):
        self.assert_rejected([], "JSON object")

    def test_missing_field(self):
        d = valid()
        del d["signup_start"]
        self.assert_rejected(d, "missing")

    def test_unknown_field(self):
        self.assert_rejected({**valid(), "signup_strat": "x"}, "unknown")

    def test_wrong_types(self):
        for key, value in [("capacity", "100"), ("capacity", 100.0), ("capacity", True),
                           ("alternates", None), ("url", 1), ("notes", 3)]:
            with self.subTest(key=key, value=value):
                self.assert_rejected({**valid(), key: value}, key)

    def test_negative_counts(self):
        self.assert_rejected({**valid(), "alternates": -1}, "non-negative")

    def test_non_https_url(self):
        self.assert_rejected({**valid(), "url": "http://app.fans/x"}, "https")

    def test_unknown_timezone(self):
        self.assert_rejected({**valid(), "timezone": "Asia/Taipe"}, "timezone")

    def test_naive_datetime(self):
        self.assert_rejected({**valid(), "signup_start": "2026-09-22T19:00:00"}, "offset")

    def test_garbage_datetime(self):
        self.assert_rejected({**valid(), "signup_end": "tonight"}, "ISO-8601")

    def test_offset_must_match_timezone(self):
        self.assert_rejected({**valid(), "signup_start": "2026-09-22T18:00:00+08:00"}, "does not match")

    def test_end_before_start(self):
        self.assert_rejected({**valid(), "signup_end": "2026-09-22T19:00:00+09:00"}, "after")


class FileLoadingTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(Path(tmp.name).unlink)
        tmp.write(text)
        tmp.close()
        return Path(tmp.name)

    def test_nan_and_infinity_literals_are_rejected(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                text = NMIXX.read_text(encoding="utf-8").replace('"capacity": 100', f'"capacity": {literal}')
                with self.assertRaises(ProfileError) as ctx:
                    load_profile(self._write(text))
                self.assertIn("non-finite", str(ctx.exception))

    def test_invalid_json(self):
        with self.assertRaises(ProfileError) as ctx:
            load_profile(self._write("{not json"))
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(ProfileError) as ctx:
            load_profile(REPO / "profiles" / "does-not-exist.json")
        self.assertIn("cannot read", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
