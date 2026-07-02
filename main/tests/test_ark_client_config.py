import unittest
from unittest.mock import patch

from app.services.ark_client import ArkNotConfigured, ark_config


class ArkClientConfigTest(unittest.TestCase):
    def tearDown(self):
        ark_config.cache_clear()

    def test_env_config_takes_priority_over_file(self):
        with patch.dict(
            "os.environ",
            {
                "ARK_API_KEY": "sk-env-valid-key",
                "ARK_ENDPOINT_ID": "qwen-vl-plus",
                "ARK_BASE_URL": "https://example.test/v1",
            },
            clear=True,
        ):
            ark_config.cache_clear()
            config = ark_config()

        self.assertEqual(config["api_key"], "sk-env-valid-key")
        self.assertEqual(config["endpoint_id"], "qwen-vl-plus")
        self.assertEqual(config["base_url"], "https://example.test/v1")

    def test_partial_env_config_is_rejected(self):
        with patch.dict("os.environ", {"ARK_API_KEY": "sk-env-valid-key", "ARK_ENDPOINT_ID": ""}, clear=True):
            ark_config.cache_clear()
            with self.assertRaises(ArkNotConfigured):
                ark_config()


if __name__ == "__main__":
    unittest.main()
