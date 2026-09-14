from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from firstrun.agent.strands import (
    _read_provider_api_key,
    _validated_anthropic_endpoint,
    _validated_mantle_endpoint,
)
from firstrun.domain.repair import RepairProviderConfig


def _config(**changes: object) -> RepairProviderConfig:
    values: dict[str, object] = {
        "model_id": "example-model",
        "provider_cost_acknowledged": True,
        "credential_identity_verified": True,
    }
    values.update(changes)
    return RepairProviderConfig(**values)  # type: ignore[arg-type]


class RepairProviderSelectionTests(unittest.TestCase):
    def test_default_provider_is_bedrock_and_keeps_existing_contract(self) -> None:
        config = _config(aws_profile="firstrun", region="us-east-1")

        self.assertEqual("amazon-bedrock", config.provider_id)
        self.assertIsNone(config.anthropic_api_key_path)

    def test_bedrock_providers_require_profile_and_region(self) -> None:
        for provider in ("amazon-bedrock", "amazon-bedrock-mantle"):
            with self.subTest(provider=provider):
                with self.assertRaises(ValidationError):
                    _config(provider_id=provider, region="us-east-1")
                with self.assertRaises(ValidationError):
                    _config(provider_id=provider, aws_profile="firstrun")
                config = _config(
                    provider_id=provider, aws_profile="firstrun", region="us-east-1"
                )
                self.assertEqual(provider, config.provider_id)

    def test_bedrock_providers_reject_a_stray_api_key_path(self) -> None:
        for provider in ("amazon-bedrock", "amazon-bedrock-mantle"):
            with self.subTest(provider=provider), self.assertRaises(ValidationError):
                _config(
                    provider_id=provider,
                    aws_profile="firstrun",
                    region="us-east-1",
                    anthropic_api_key_path=Path("key.txt"),
                )

    def test_anthropic_requires_key_path_and_rejects_aws_fields(self) -> None:
        with self.assertRaises(ValidationError):
            _config(provider_id="anthropic")
        with self.assertRaises(ValidationError):
            _config(
                provider_id="anthropic",
                anthropic_api_key_path=Path("key.txt"),
                aws_profile="firstrun",
            )
        with self.assertRaises(ValidationError):
            _config(
                provider_id="anthropic",
                anthropic_api_key_path=Path("key.txt"),
                region="us-east-1",
            )
        config = _config(
            provider_id="anthropic", anthropic_api_key_path=Path("key.txt")
        )
        self.assertEqual(Path("key.txt"), config.anthropic_api_key_path)

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _config(provider_id="openai", aws_profile="x", region="us-east-1")


class ProviderApiKeyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _write(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_reads_a_well_formed_key_and_strips_surrounding_whitespace(self) -> None:
        key = "sk-ant-api03-" + "a" * 40
        path = self._write("key", f"  {key}\n".encode("ascii"))

        self.assertEqual(key, _read_provider_api_key(path))

    def test_rejects_missing_path(self) -> None:
        with self.assertRaises(ValueError):
            _read_provider_api_key(None)
        with self.assertRaises(ValueError):
            _read_provider_api_key(self.root / "absent")

    def test_rejects_empty_oversized_and_malformed_keys(self) -> None:
        cases = {
            "empty": b"",
            "oversized": b"a" * 600,
            "too_short": b"short",
            "shell_text": b"$(cat /etc/passwd)" + b"a" * 40,
            "non_ascii": "sk-ant-é".encode("utf-8") + b"a" * 40,
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                path = self._write(f"key-{label}", content)
                with self.assertRaises(ValueError):
                    _read_provider_api_key(path)

    def test_rejects_directory_and_symlink(self) -> None:
        with self.assertRaises(ValueError):
            _read_provider_api_key(self.root)
        target = self._write("real", b"sk-ant-" + b"a" * 40)
        link = self.root / "link"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not permitted on this host")
        with self.assertRaises(ValueError):
            _read_provider_api_key(link)


class ProviderEndpointValidationTests(unittest.TestCase):
    @staticmethod
    def _model(base_url: object) -> SimpleNamespace:
        return SimpleNamespace(client=SimpleNamespace(base_url=base_url))

    def test_mantle_endpoint_must_match_the_selected_region(self) -> None:
        good = self._model("https://bedrock-mantle.us-east-1.api.aws/anthropic/")

        self.assertEqual(
            "https://bedrock-mantle.us-east-1.api.aws",
            _validated_mantle_endpoint(good, "us-east-1"),
        )
        with self.assertRaises(ValueError):
            _validated_mantle_endpoint(good, "us-west-2")

    def test_mantle_endpoint_rejects_foreign_or_downgraded_origins(self) -> None:
        bad = (
            "http://bedrock-mantle.us-east-1.api.aws/anthropic/",
            "https://bedrock-mantle.us-east-1.api.aws.evil.example/anthropic/",
            "https://bedrock-mantle.us-east-1.api.aws:8443/anthropic/",
            "https://api.anthropic.com/",
            "",
            None,
        )
        for endpoint in bad:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                _validated_mantle_endpoint(self._model(endpoint), "us-east-1")

    def test_anthropic_endpoint_must_be_the_canonical_origin(self) -> None:
        self.assertEqual(
            "https://api.anthropic.com",
            _validated_anthropic_endpoint(self._model("https://api.anthropic.com/")),
        )
        bad = (
            "http://api.anthropic.com/",
            "https://api.anthropic.com.evil.example/",
            "https://api.anthropic.com/?proxy=1",
            "https://bedrock-mantle.us-east-1.api.aws/anthropic/",
            None,
        )
        for endpoint in bad:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                _validated_anthropic_endpoint(self._model(endpoint))


if __name__ == "__main__":
    unittest.main()
