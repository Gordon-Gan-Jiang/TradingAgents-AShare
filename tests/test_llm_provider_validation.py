import unittest

from api.main import _build_runtime_config
from api.services import model_profile_service
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.validators import (
    looks_like_uuid,
    validate_llm_provider,
)

ARK_ENDPOINT_ID = "e6f062d9-df51-4710-a6ff-ce7e4f9a6061"


class TestLlmProviderValidation(unittest.TestCase):
    def test_looks_like_uuid(self):
        self.assertTrue(looks_like_uuid(ARK_ENDPOINT_ID))
        self.assertFalse(looks_like_uuid("openai"))
        self.assertFalse(looks_like_uuid("doubao-1-5-lite-32k-250115"))

    def test_validate_openai_provider(self):
        self.assertEqual(validate_llm_provider("openai"), "openai")
        self.assertEqual(validate_llm_provider(" OpenAI "), "openai")

    def test_uuid_in_llm_provider_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "接入点 ID"):
            validate_llm_provider(ARK_ENDPOINT_ID)

    def test_create_llm_client_rejects_uuid_provider(self):
        with self.assertRaisesRegex(ValueError, "接入点 ID"):
            create_llm_client(
                ARK_ENDPOINT_ID,
                "doubao-1-5-lite-32k-250115",
                base_url="https://ark.cn-beijing.volces.com/api/v3",
            )

    def test_uuid_as_model_name_with_openai_provider_is_allowed(self):
        client = create_llm_client(
            "openai",
            ARK_ENDPOINT_ID,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key="test-key",
        )
        self.assertEqual(client.model, ARK_ENDPOINT_ID)
        self.assertEqual(client.provider, "openai")

    def test_build_runtime_config_rejects_uuid_provider(self):
        with self.assertRaisesRegex(ValueError, "接入点 ID"):
            _build_runtime_config(
                {
                    "llm_provider": ARK_ENDPOINT_ID,
                    "backend_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "quick_think_llm": "doubao-1-5-lite-32k-250115",
                },
                trusted_overrides=True,
            )

    def test_build_runtime_config_accepts_uuid_model_name(self):
        config = _build_runtime_config(
            {
                "llm_provider": "openai",
                "backend_url": "https://ark.cn-beijing.volces.com/api/v3",
                "quick_think_llm": ARK_ENDPOINT_ID,
                "deep_think_llm": ARK_ENDPOINT_ID,
                "api_key": "test-key",
            },
            trusted_overrides=True,
        )
        self.assertEqual(config["llm_provider"], "openai")
        self.assertEqual(config["quick_think_llm"], ARK_ENDPOINT_ID)

    def test_model_profile_service_rejects_uuid_provider(self):
        with self.assertRaisesRegex(ValueError, "接入点 ID"):
            model_profile_service._normalize_provider(ARK_ENDPOINT_ID)


if __name__ == "__main__":
    unittest.main()
