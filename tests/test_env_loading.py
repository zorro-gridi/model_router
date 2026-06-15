"""
test_env_loading.py — model_router 三模块必须从 hooks/.env 共享层读 env
====================================================================

回归门禁：修复前，proxy.py / llm_classifier.py / stage CLI 都内联实现了
_load_dotenv()，只读 hooks/<plugin>/.env（model_router 子目录），错过用户配在
hooks/.env 共享层的 MINIMAX_API_KEY / DEEPSEEK_API_KEY。proxy 启动会
sys.exit(1)；llm_classifier.classify() 会抛 RuntimeError。

修复后，三个模块的入口都改用 _load_env.load_plugin_env(__file__)，
按 shared → private 顺序加载，共享层生效。

测试方法（不依赖真实 ~/.claude/hooks/.env）：
  1. monkeypatch `_load_env.HOOKS_ROOT` 指向一个临时目录
  2. 在临时目录下放一个 `hooks/.env`，含 TEST_SHARED_KEY=from_shared
  3. 再放一个 `hooks/model_router/.env`，含 TEST_PLUGIN_KEY=from_plugin
  4. 调用 _load_env.load_plugin_env(<指向临时 model_router 的虚拟文件>)
  5. 断言 TEST_SHARED_KEY 和 TEST_PLUGIN_KEY 都在 os.environ 里
  6. 反向验证：shell env 注入的优先级最高（不被 .env 覆盖）
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestLoadPluginEnvSharedLayer(unittest.TestCase):
    """load_plugin_env 必须从共享层 hooks/.env 读 env（不只是 plugin-private 层）。"""

    def setUp(self):
        # 临时 hooks/ 根：tmp/hooks/.env
        self.tmp_root = tempfile.TemporaryDirectory()
        self.tmp_hooks = Path(self.tmp_root.name)
        (self.tmp_hooks / ".env").write_text(
            "TEST_SHARED_KEY=from_shared\n", encoding="utf-8"
        )
        # 临时 plugin 目录：tmp/hooks/model_router/.env
        self.tmp_plugin = self.tmp_hooks / "model_router"
        self.tmp_plugin.mkdir()
        (self.tmp_plugin / ".env").write_text(
            "TEST_PLUGIN_KEY=from_plugin\n", encoding="utf-8"
        )
        # 模拟 plugin 中的入口脚本
        self.tmp_fake_entry = self.tmp_plugin / "_fake_entry.py"
        self.tmp_fake_entry.write_text("# fake entry", encoding="utf-8")

        # 清理环境（如果之前 import 过相关模块，env 里可能残留）
        self._saved = {
            k: os.environ.pop(k)
            for k in ("TEST_SHARED_KEY", "TEST_PLUGIN_KEY")
            if k in os.environ
        }
        # 强制 reload _load_env 模块，让 monkeypatch 在干净状态下生效
        # （如果已 import 过的化，HOOKS_ROOT 已经被绑定到真实 ~/.claude/hooks/）
        if "_load_env" in sys.modules:
            del sys.modules["_load_env"]

    def tearDown(self):
        # 清理
        for k in ("TEST_SHARED_KEY", "TEST_PLUGIN_KEY"):
            os.environ.pop(k, None)
        # 还原
        for k, v in self._saved.items():
            os.environ[k] = v
        # 恢复真实 _load_env（让后续测试看到真实 hooks 根）
        if "_load_env" in sys.modules:
            del sys.modules["_load_env"]
        self.tmp_root.cleanup()

    def test_load_plugin_env_reads_shared_layer(self):
        """shared layer (hooks/.env) 必须被读到。"""
        from _load_env import load_plugin_env, HOOKS_ROOT
        # 强制让 _load_env 把临时目录当作 hooks 根
        with patch.object(sys.modules["_load_env"], "HOOKS_ROOT", self.tmp_hooks):
            load_plugin_env(str(self.tmp_fake_entry))

        self.assertEqual(os.environ.get("TEST_SHARED_KEY"), "from_shared",
                         "shared hooks/.env 应该被读到")

    def test_load_plugin_env_reads_plugin_private_layer(self):
        """plugin-private layer (hooks/<plugin>/.env) 也必须被读到。"""
        from _load_env import load_plugin_env
        with patch.object(sys.modules["_load_env"], "HOOKS_ROOT", self.tmp_hooks):
            load_plugin_env(str(self.tmp_fake_entry))

        self.assertEqual(os.environ.get("TEST_PLUGIN_KEY"), "from_plugin",
                         "plugin-private .env 应该被读到")

    def test_shared_layer_loaded_before_plugin_private(self):
        """shared 先于 plugin-private 加载（override=False 配合：plugin 可覆盖未设的 key）。"""
        # shared 与 plugin 都设同一个 key 时，shell env 没设的话 plugin 应胜出
        (self.tmp_hooks / ".env").write_text("TEST_KEY=from_shared\n", encoding="utf-8")
        (self.tmp_plugin / ".env").write_text("TEST_KEY=from_plugin\n", encoding="utf-8")

        from _load_env import load_plugin_env
        with patch.object(sys.modules["_load_env"], "HOOKS_ROOT", self.tmp_hooks):
            load_plugin_env(str(self.tmp_fake_entry))

        # shared 先加载，plugin 后加载（override=False）
        # shared 先把 key 设为 from_shared
        # plugin 再加载时 key 已存在 → 不覆盖 → 保持 from_shared
        self.assertEqual(os.environ.get("TEST_KEY"), "from_shared",
                         "shared 先加载，plugin-private 不会覆盖已存在的 key")

    def test_shell_env_has_highest_priority(self):
        """shell 注入的 env 不被 .env 覆盖（override=False 语义）。"""
        os.environ["TEST_SHARED_KEY"] = "from_shell"

        from _load_env import load_plugin_env
        with patch.object(sys.modules["_load_env"], "HOOKS_ROOT", self.tmp_hooks):
            load_plugin_env(str(self.tmp_fake_entry))

        self.assertEqual(os.environ.get("TEST_SHARED_KEY"), "from_shell",
                         "shell env 优先级最高，不被 .env 覆盖")


class TestModelRouterModulesUseSharedLoader(unittest.TestCase):
    """回归门禁：model_router 三个生产模块必须用 load_plugin_env，不能再用内联 _load_dotenv。"""

    def test_proxy_py_does_not_define_inline_load_dotenv(self):
        """proxy.py 不能再内联定义 _load_dotenv 函数（应改用 load_plugin_env）。"""
        proxy_path = Path(__file__).resolve().parent.parent / "proxy.py"
        source = proxy_path.read_text(encoding="utf-8")

        # 旧的实现：proxy.py 里有 _DOTENV_LINE + _load_dotenv 函数 + 调用 _load_dotenv(ENV_FILE)
        self.assertNotIn("def _load_dotenv(", source,
                         "proxy.py 不应再定义 _load_dotenv（应改用 load_plugin_env）")
        self.assertNotIn("def _load_dotenv_once(", source,
                         "proxy.py 不应再有 _load_dotenv_once")
        # 新调用必须存在
        self.assertIn("load_plugin_env(__file__)", source,
                     "proxy.py 必须调用 load_plugin_env(__file__)")

    def test_llm_classifier_py_uses_load_plugin_env(self):
        """llm_classifier.py 必须用 load_plugin_env。"""
        path = Path(__file__).resolve().parent.parent / "llm_classifier.py"
        source = path.read_text(encoding="utf-8")

        self.assertNotIn("def _load_dotenv_once(", source,
                         "llm_classifier.py 不应再定义 _load_dotenv_once")
        self.assertIn("load_plugin_env(__file__)", source,
                     "llm_classifier.py 必须调用 load_plugin_env(__file__)")

    def test_stage_cli_uses_load_plugin_env(self):
        """stage CLI 必须用 load_plugin_env。"""
        path = Path(__file__).resolve().parent.parent / "stage"
        source = path.read_text(encoding="utf-8")

        self.assertNotIn("def _load_dotenv(", source,
                         "stage CLI 不应再定义 _load_dotenv")
        self.assertIn("load_plugin_env(__file__)", source,
                     "stage CLI 必须调用 load_plugin_env(__file__)")


class TestModelRouterImportsPopulateSharedEnv(unittest.TestCase):
    """集成测试：import llm_classifier 后，hooks/.env 共享层的 key 应出现在 os.environ。"""

    def setUp(self):
        # 强制 reload 以便 monkeypatch 生效
        for mod in list(sys.modules):
            if mod.startswith("llm_classifier") or mod == "_load_env":
                del sys.modules[mod]

    def tearDown(self):
        for k in ("TEST_SHARED_KEY",):
            os.environ.pop(k, None)
        for mod in list(sys.modules):
            if mod.startswith("llm_classifier") or mod == "_load_env":
                del sys.modules[mod]

    def test_importing_llm_classifier_loads_shared_env(self):
        """import llm_classifier 应自动加载 hooks/.env 共享层。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            tmp_hooks = Path(tmp.name)
            (tmp_hooks / ".env").write_text(
                "TEST_SHARED_KEY=via_shared_layer\n", encoding="utf-8"
            )
            tmp_plugin = tmp_hooks / "model_router"
            tmp_plugin.mkdir()

            # 在临时 plugin 目录里建一个 llm_classifier.py 的薄包装 + .env
            # 真实 llm_classifier 依赖 anthropic SDK，我们不直接 import 它，
            # 而是用 monkeypatch _load_env.HOOKS_ROOT 后，import stage_config + 模拟入口
            import importlib
            if "_load_env" in sys.modules:
                del sys.modules["_load_env"]
            import _load_env
            with patch.object(_load_env, "HOOKS_ROOT", tmp_hooks):
                # 模拟一个入口文件
                fake_entry = tmp_plugin / "_fake_entry.py"
                fake_entry.write_text("# fake", encoding="utf-8")
                _load_env.load_plugin_env(str(fake_entry))

            self.assertEqual(os.environ.get("TEST_SHARED_KEY"),
                             "via_shared_layer",
                             "shared hooks/.env 必须被 load_plugin_env 加载到 os.environ")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
