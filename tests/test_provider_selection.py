import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scout import cli, config
from scout.providers.ollama_llm import OllamaLLMProvider
from scout.providers.search_factory import build_search_provider


class ProviderSelectionTests(unittest.TestCase):
    def test_default_provider_is_ollama_with_local_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = config.load_config(require_llm=True)
        self.assertEqual("ollama", cfg.llm_provider)
        self.assertEqual("exa", cfg.search_provider)
        self.assertEqual("qwen3:8b", cfg.ollama_model)
        self.assertEqual("http://127.0.0.1:11434", cfg.ollama_base_url)

    def test_ollama_does_not_require_anthropic_or_import_it(self):
        env = {"SCOUT_LLM_PROVIDER": "ollama", "EXA_API_KEY": "key"}
        with patch.dict(os.environ, env, clear=True):
            cfg = config.load_config(require_llm=True)
            self.assertIsNone(cfg.anthropic_api_key)
            _, _, llm = cli._build_providers(cfg)
        self.assertEqual("OllamaLLMProvider", type(llm).__name__)
        self.assertNotIn("anthropic", sys.modules)

    def test_anthropic_requires_key_and_invalid_provider_is_rejected(self):
        with patch.dict(os.environ, {"SCOUT_LLM_PROVIDER": "anthropic"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY"):
                config.load_config(require_llm=True)
        with patch.dict(os.environ, {"SCOUT_LLM_PROVIDER": "unknown"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SCOUT_LLM_PROVIDER"):
                config.load_config()

    def test_anthropic_sdk_fails_only_when_anthropic_is_selected(self):
        env = {"SCOUT_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "key", "EXA_API_KEY": "exa"}
        with patch.dict(os.environ, env, clear=True):
            cfg = config.load_config(require_llm=True)
            with self.assertRaisesRegex(RuntimeError, "Anthropic SDK"):
                cli._build_providers(cfg)

    def test_custom_ollama_settings_work(self):
        with patch.dict(os.environ, {"SCOUT_OLLAMA_MODEL": "qwen2.5:3b", "SCOUT_OLLAMA_BASE_URL": "http://local.test:1234"}, clear=True):
            cfg = config.load_config(require_llm=True)
        self.assertEqual("qwen2.5:3b", cfg.ollama_model)
        self.assertEqual("http://local.test:1234", cfg.ollama_base_url)

    def test_discovery_boundary_requires_selected_search_credentials(self):
        with patch.dict(os.environ, {"SCOUT_LLM_PROVIDER": "ollama"}, clear=True):
            cfg = config.load_config(require_llm=True)
            with self.assertRaisesRegex(RuntimeError, "EXA_API_KEY"):
                cli._build_providers(cfg)

    def test_google_cse_remains_selectable(self):
        env = {"SEARCH_PROVIDER": "google_cse", "GOOGLE_CSE_API_KEY": "key", "GOOGLE_CSE_CX": "cx"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual("GoogleCSEProvider", type(build_search_provider(config.load_config())).__name__)

    def test_doctor_reports_search_chain_without_credential_values(self):
        env = {"SEARCH_PROVIDERS": "exa,tavily", "EXA_API_KEY": "secret-exa", "TAVILY_API_KEY": "secret-tavily", "SCOUT_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "secret-anthropic"}
        with patch.dict(os.environ, env, clear=True):
            lines = cli.doctor_report(config.load_config())
        output = "\n".join(lines)
        self.assertIn("Search provider chain: exa -> tavily", output)
        self.assertNotIn("secret-", output)

    def test_help_and_stored_data_command_need_no_provider_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            for name in ("ANTHROPIC_API_KEY", "GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX", "SCOUT_LLM_PROVIDER"):
                env.pop(name, None)
            env["SCOUT_DB_PATH"] = str(Path(directory) / "scout.db")
            help_result = subprocess.run([sys.executable, "-m", "scout.cli", "--help"], cwd=Path(__file__).parents[1], env=env,
                                         text=True, capture_output=True, check=False)
            entities_result = subprocess.run([sys.executable, "-m", "scout.cli", "entities"], cwd=Path(__file__).parents[1], env=env,
                                             text=True, capture_output=True, check=False)
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertEqual(0, entities_result.returncode, entities_result.stderr)

    def test_ollama_adapter_uses_native_json_and_disables_thinking(self):
        response = Mock()
        response.json.return_value = {"response": '{"status":"ok"}'}
        with patch("scout.providers.ollama_llm.requests.post", return_value=response) as post:
            result = OllamaLLMProvider("qwen3:8b").extract(
                "system", "content", "tool", "description",
                {"type": "object", "properties": {"status": {"type": "string"}}}, max_tokens=32,
            )
        self.assertEqual({"status": "ok"}, result.input)
        self.assertEqual("http://localhost:11434/api/generate", post.call_args[0][0])
        payload = post.call_args.kwargs["json"]
        self.assertFalse(payload["stream"])
        self.assertEqual("json", payload["format"])
        self.assertFalse(payload["think"])
        self.assertEqual(32, payload["options"]["num_predict"])
