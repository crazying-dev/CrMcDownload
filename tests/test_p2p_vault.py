"""机器绑定存储：DPAPI 往返、回落 SecretBox 往返、密文不含明文、SavedRooms 持久化。

用 ``OFFCRAFT_DATA`` 指向临时目录，避免污染真实配置与 ``machine.key``。
"""

import os
import tempfile
import unittest
from pathlib import Path

from offcraft.p2p import vault


class _TempData(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("OFFCRAFT_DATA")
        os.environ["OFFCRAFT_DATA"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OFFCRAFT_DATA", None)
        else:
            os.environ["OFFCRAFT_DATA"] = self._prev
        self._tmp.cleanup()


class ProtectTest(_TempData):
    def test_dpapi_roundtrip(self):
        if not vault._dpapi_available():
            self.skipTest("DPAPI 仅 Windows")
        secret = " hunter2 ".encode("utf-8")
        token = vault.protect(secret)
        self.assertTrue(token.startswith("dpapi:"))
        self.assertEqual(vault.unprotect(token), secret)

    def test_fallback_roundtrip(self):
        # 强制走机器密钥 + SecretBox 回落路径
        original = vault._dpapi_available
        vault._dpapi_available = lambda: False
        try:
            secret = "p@ssw0rd-测试".encode("utf-8")
            token = vault.protect(secret)
            self.assertTrue(token.startswith("sb:"))
            self.assertEqual(vault.unprotect(token), secret)
        finally:
            vault._dpapi_available = original

    def test_ciphertext_has_no_plaintext(self):
        original = vault._dpapi_available
        vault._dpapi_available = lambda: False  # 用可控的 SecretBox 便于检查
        try:
            secret = b"super-secret-room-password"
            token = vault.protect(secret)
            # base64 段解码后不应出现明文
            import base64
            blob = base64.b64decode(token.split(":", 1)[1])
            self.assertNotIn(secret, blob)
            self.assertNotIn(secret.decode(), token)
        finally:
            vault._dpapi_available = original

    def test_unprotect_rejects_garbage(self):
        with self.assertRaises(vault.VaultError):
            vault.unprotect("not-a-token")
        with self.assertRaises(vault.VaultError):
            vault.unprotect("unknown:AAAA")


class SavedRoomsTest(_TempData):
    def _vault(self):
        return vault.SavedRooms(Path(self._tmp.name) / "rooms.json")

    def test_add_list_password_remove(self):
        store = self._vault()
        store.add("RoomOne", "alpha123")
        store.add("  ROOMTWO ", "beta456")
        self.assertEqual(store.names(), ["roomone", "roomtwo"])
        self.assertEqual(store.password("roomone"), "alpha123")
        self.assertEqual(store.password("RoomTwo"), "beta456")  # 大小写不敏感
        self.assertTrue(store.remove("roomone"))
        self.assertFalse(store.remove("roomone"))
        self.assertEqual(store.password("roomone"), "")

    def test_persistence_across_reload(self):
        store = self._vault()
        store.add("Persist", "keepme")
        reloaded = self._vault()
        self.assertEqual(reloaded.password("persist"), "keepme")

    def test_disk_is_ciphertext(self):
        store = self._vault()
        store.add("Secret", "plainhunter")
        raw = store.path.read_text(encoding="utf-8")
        # 密码以密文落盘；房间名可明文列出（不含密码明文）
        self.assertNotIn("plainhunter", raw)
        self.assertIn("secret", raw)


if __name__ == "__main__":
    unittest.main()
