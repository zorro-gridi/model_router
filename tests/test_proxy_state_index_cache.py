"""
test_proxy_state_index_cache.py — proxy state_index 读取缓存测试
===============================================================

目标：
  1. _read_state_index_all() 在文件未变化时命中缓存
  2. state_index.json 更新后缓存自动失效并返回新值
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class TestStateIndexCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_index = Path(self.tmp.name) / "state_index.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data: dict) -> None:
        self.state_index.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_cache_hits_and_invalidates_on_file_change(self):
        import proxy

        self._write({
            "/project/a": {"session_id": "sid-1", "stage": "implement", "last_active": 1},
        })

        with patch.object(proxy, "STATE_INDEX_FILE", self.state_index):
            proxy._STATE_INDEX_CACHE["mtime_ns"] = None
            proxy._STATE_INDEX_CACHE["size"] = None
            proxy._STATE_INDEX_CACHE["data"] = {}

            first = proxy._read_state_index_all()
            second = proxy._read_state_index_all()

            self.assertEqual(first["/project/a"]["session_id"], "sid-1")
            self.assertEqual(second["/project/a"]["session_id"], "sid-1")
            self.assertIs(
                first, second,
                "文件未变化时应直接复用缓存对象，避免重复 JSON 解析",
            )

            time.sleep(0.01)
            self._write({
                "/project/a": {"session_id": "sid-2", "stage": "decide", "last_active": 2},
            })

            third = proxy._read_state_index_all()
            self.assertEqual(
                third["/project/a"]["session_id"], "sid-2",
                "state_index.json 更新后缓存必须自动失效并返回新值",
            )


if __name__ == "__main__":
    unittest.main()
