"""设备标识：首启生成、落盘持久、跨重载稳定（离线联机每设备配额锚点）。"""

import unittest

from offcraft.config import Config, load_config, save_config


class ConfigNormalizeTest(unittest.TestCase):
    def test_empty_generates_hex32(self):
        cfg = Config()
        self.assertEqual(cfg.p2p_device_id, "")
        cfg.normalize()
        self.assertEqual(len(cfg.p2p_device_id), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in cfg.p2p_device_id))

    def test_valid_is_preserved(self):
        cfg = Config()
        cfg.normalize()
        first = cfg.p2p_device_id
        cfg.normalize()  # 已合法，再次规范化不应改变
        self.assertEqual(cfg.p2p_device_id, first)

    def test_invalid_regenerated(self):
        cfg = Config()
        cfg.p2p_device_id = "NOT-HEX!!"
        cfg.normalize()
        self.assertNotEqual(cfg.p2p_device_id, "NOT-HEX!!")
        self.assertEqual(len(cfg.p2p_device_id), 32)


class PersistenceTest(unittest.TestCase):
    def test_stable_across_save_load(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            cfg = Config()
            save_config(cfg, data)
            reloaded = load_config(data)
            self.assertEqual(reloaded.p2p_device_id, cfg.p2p_device_id)
            # 二次加载仍是同一标识（不会每次刷新）
            self.assertEqual(load_config(data).p2p_device_id, cfg.p2p_device_id)


if __name__ == "__main__":
    unittest.main()
