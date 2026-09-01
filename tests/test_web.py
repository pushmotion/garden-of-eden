import unittest

from app import create_app


class WebUITestCase(unittest.TestCase):
    def setUp(self):
        self.client = create_app("default").test_client()

    def test_root_serves_html(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.content_type)
        self.assertIn(b"Garden of Eden", resp.data)

    def test_schedule_endpoints_keep_the_shape_the_page_reads(self):
        """The bundled page reads specific keys off these responses.

        They are a contract, not an implementation detail: the UI renders the
        next-run strip, the pending one-off list and the cron comparison
        straight from these fields, and a rename would blank those readouts
        with no error anywhere. Values are not asserted -- only that the keys
        the page indexes into still exist.
        """
        expected = {
            "/schedule/next": {"light", "pump"},
            "/schedule/pump/once": {"count", "runs"},
            "/schedule/cron": {"count", "cron_lines"},
            "/schedule/state": {"now", "vacation", "light", "pump"},
        }
        for path, keys in expected.items():
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200, f"{path} is unavailable")
                self.assertTrue(
                    keys.issubset(resp.get_json().keys()),
                    f"{path} no longer returns {sorted(keys)}",
                )

    def test_schedule_validate_returns_cron_lines_for_comparison(self):
        """The page diffs these against /schedule/cron to spot a drifted crontab."""
        resp = self.client.post("/schedule/validate", json={"lights": {"enabled": False}})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["valid"])
        self.assertIsInstance(body["cron_lines"], list)


if __name__ == "__main__":
    unittest.main()
