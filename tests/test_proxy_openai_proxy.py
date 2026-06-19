import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200
        self.headers = {"content-type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req, timeout))
        return _FakeResponse(self.body)


class TestOpenAIProxyRouting(unittest.TestCase):
    def _import_proxy(self):
        fake_anthropic = types.ModuleType("anthropic")
        fake_httpx = types.ModuleType("httpx")
        fake_llm_classifier = types.ModuleType("llm_classifier")
        fake_llm_classifier.classify = lambda *args, **kwargs: {}
        fake_rate_limit = types.ModuleType("rate_limit")
        fake_rate_limit.check_rate_limit = lambda *args, **kwargs: True
        fake_rate_limit.consume = lambda *args, **kwargs: None
        fake_hooks = types.ModuleType("hooks")
        fake_hooks_compact = types.ModuleType("hooks.compact")
        fake_hooks_utils = types.ModuleType("hooks.compact.utils")
        fake_hooks_utils._find_project_root = lambda path=None: Path("/tmp")
        fake_modules = {
            "anthropic": fake_anthropic,
            "httpx": fake_httpx,
            "llm_classifier": fake_llm_classifier,
            "rate_limit": fake_rate_limit,
            "hooks": fake_hooks,
            "hooks.compact": fake_hooks_compact,
            "hooks.compact.utils": fake_hooks_utils,
        }
        with patch.dict(sys.modules, fake_modules):
            if "proxy" in sys.modules:
                del sys.modules["proxy"]
            import proxy
        return proxy

    def test_openai_model_uses_local_https_proxy(self):
        proxy = self._import_proxy()

        openai_body = json.dumps({
            "id": "chatcmpl-test",
            "model": "GPT-5.4",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        fake_opener = _FakeOpener(openai_body)
        captured = {}

        def _fake_build_opener(handler):
            captured["handler"] = handler
            return fake_opener

        with patch.dict(proxy.os.environ, {"OPENAI_API_KEY": "sk-openai-test"}, clear=False):
            with patch.object(proxy.urllib.request, "build_opener", side_effect=_fake_build_opener) as build_opener:
                with patch.object(proxy.urllib.request, "urlopen") as urlopen:
                    status, _, body, _ = proxy.forward_request(
                        method="POST",
                        path="/v1/messages",
                        headers={},
                        body=json.dumps({"model": "claude-sonnet-4-6", "messages": []}).encode(),
                        target_base="https://api.openai.com",
                        target_model="GPT-5.4",
                        api_key_env="OPENAI_API_KEY",
                        protocol="openai",
                        dry_run=False,
                    )

        self.assertEqual(status, 200)
        self.assertIn(b'"type": "message"', body)
        self.assertEqual(build_opener.call_count, 1)
        self.assertEqual(urlopen.call_count, 0)
        self.assertEqual(captured["handler"].proxies["https"], proxy.OPENAI_LOCAL_HTTPS_PROXY)
        self.assertEqual(len(fake_opener.calls), 1)

    def test_non_openai_model_does_not_use_local_https_proxy(self):
        proxy = self._import_proxy()

        anthropic_body = json.dumps({
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "MiniMax-M3",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()

        with patch.dict(proxy.os.environ, {"MINIMAX_API_KEY": "sk-mm-test"}, clear=False):
            with patch.object(proxy.urllib.request, "build_opener") as build_opener:
                with patch.object(proxy.urllib.request, "urlopen", return_value=_FakeResponse(anthropic_body)) as urlopen:
                    status, _, body, _ = proxy.forward_request(
                        method="POST",
                        path="/v1/messages",
                        headers={},
                        body=json.dumps({"model": "claude-sonnet-4-6", "messages": []}).encode(),
                        target_base="https://api.minimaxi.com/anthropic",
                        target_model="MiniMax-M3",
                        api_key_env="MINIMAX_API_KEY",
                        protocol="anthropic",
                        dry_run=False,
                    )

        self.assertEqual(status, 200)
        self.assertIn(b"MiniMax-M3", body)
        self.assertEqual(build_opener.call_count, 0)
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
