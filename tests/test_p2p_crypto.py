"""离线联机密码学：房间密钥派生、准入 proof、AEAD 通道往返与篡改/重放拒绝。"""

import hmac
import unittest
from hashlib import sha256

from offcraft.p2p.crypto import (
    PROOF_MESSAGE_PREFIX,
    TamperError,
    canonical_name,
    derive_room_keys,
    load_or_create_device_id,
    make_channel,
    make_proof,
    new_identity,
    proof_message,
)


class DeriveTest(unittest.TestCase):
    def test_deterministic(self):
        a = derive_room_keys("psk", "ROOM1")
        b = derive_room_keys("psk", "ROOM1")
        self.assertEqual(a, b)

    def test_room_and_psk_diverge(self):
        base = derive_room_keys("psk", "ROOM1")
        other_room = derive_room_keys("psk", "ROOM2")
        other_psk = derive_room_keys("psk2", "ROOM1")
        self.assertNotEqual(base.auth_key, other_room.auth_key)
        self.assertNotEqual(base.key_ab, other_psk.key_ab)

    def test_keys_are_32_bytes_and_distinct(self):
        keys = derive_room_keys("psk", "ROOM")
        for value in (keys.auth_key, keys.key_ab, keys.key_ba):
            self.assertEqual(len(value), 32)
        self.assertEqual(len({keys.auth_key, keys.key_ab, keys.key_ba}), 3)

    def test_empty_psk_rejected(self):
        with self.assertRaises(Exception):
            derive_room_keys("", "ROOM")

    def test_empty_name_rejected(self):
        with self.assertRaises(Exception):
            derive_room_keys("psk", "   ")

    def test_name_is_case_insensitive(self):
        # 同一房间不同大小写派生出同一把密钥（房主与加入者必须一致）
        self.assertEqual(derive_room_keys("psk", "RoomOne"), derive_room_keys("psk", "roomone"))
        self.assertEqual(derive_room_keys("psk", "  ROOM  ").auth_key,
                       derive_room_keys("psk", "room").auth_key)


class CanonicalNameTest(unittest.TestCase):
    def test_strip_lower_truncate(self):
        self.assertEqual(canonical_name("  MyRoom  "), "myroom")
        self.assertEqual(canonical_name(""), "")
        self.assertEqual(len(canonical_name("a" * 100)), 64)


class DeviceIdTest(unittest.TestCase):
    class _Cfg:
        def __init__(self, value=""):
            self.p2p_device_id = value

    def test_generates_when_missing(self):
        cfg = self._Cfg()
        device = load_or_create_device_id(cfg)
        self.assertEqual(len(device), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in device))
        self.assertEqual(cfg.p2p_device_id, device)

    def test_stable_when_present(self):
        cfg = self._Cfg("ab" * 16)
        self.assertEqual(load_or_create_device_id(cfg), "ab" * 16)

    def test_regenerates_invalid(self):
        cfg = self._Cfg("zzz")  # 非法字符
        device = load_or_create_device_id(cfg)
        self.assertNotEqual(device, "zzz")
        self.assertEqual(len(device), 32)


class ProofTest(unittest.TestCase):
    def test_proof_matches_independent_hmac(self):
        keys = derive_room_keys("psk", "ROOM")
        pub, nonce = new_identity()
        proof = make_proof(keys.auth_key, pub, nonce)
        expected = hmac.new(
            keys.auth_key,
            PROOF_MESSAGE_PREFIX + bytes.fromhex(pub) + bytes.fromhex(nonce),
            sha256,
        ).hexdigest()
        self.assertEqual(proof, expected)
        self.assertEqual(len(proof), 64)

    def test_proof_message_binds_pub_and_nonce(self):
        msg = proof_message(b"\x01" * 32, b"\x02" * 16)
        self.assertTrue(msg.startswith(PROOF_MESSAGE_PREFIX))

    def test_wrong_key_yields_different_proof(self):
        pub, nonce = new_identity()
        p1 = make_proof(derive_room_keys("psk", "ROOM").auth_key, pub, nonce)
        p2 = make_proof(derive_room_keys("psk", "OTHER").auth_key, pub, nonce)
        self.assertNotEqual(p1, p2)


class ChannelTest(unittest.TestCase):
    def test_roundtrip_both_directions(self):
        keys = derive_room_keys("psk", "ROOM")
        host = make_channel(keys, is_host=True)
        joiner = make_channel(keys, is_host=False)
        nonce, ct = host.encrypt(b"handshake", channel=7)
        self.assertEqual(joiner.decrypt(nonce, ct, 7), b"handshake")
        nonce2, ct2 = joiner.encrypt(b"world data")
        self.assertEqual(host.decrypt(nonce2, ct2, 0), b"world data")

    def test_tamper_rejected(self):
        keys = derive_room_keys("psk", "ROOM")
        host = make_channel(keys, is_host=True)
        joiner = make_channel(keys, is_host=False)
        nonce, ct = host.encrypt(b"secret")
        bad = bytes([ct[0] ^ 1]) + ct[1:]
        with self.assertRaises(TamperError):
            joiner.decrypt(nonce, bad, 0)

    def test_replay_rejected(self):
        keys = derive_room_keys("psk", "ROOM")
        host = make_channel(keys, is_host=True)
        joiner = make_channel(keys, is_host=False)
        nonce, ct = host.encrypt(b"one")
        joiner.decrypt(nonce, ct, 0)
        with self.assertRaises(TamperError):
            joiner.decrypt(nonce, ct, 0)  # 同一帧重复投递

    def test_channel_binding_mismatch_rejected(self):
        keys = derive_room_keys("psk", "ROOM")
        host = make_channel(keys, is_host=True)
        joiner = make_channel(keys, is_host=False)
        nonce, ct = host.encrypt(b"x", channel=1)
        with self.assertRaises(TamperError):
            joiner.decrypt(nonce, ct, 2)  # 换 channel → AAD 不符

    def test_bad_nonce_length_rejected(self):
        keys = derive_room_keys("psk", "ROOM")
        joiner = make_channel(keys, is_host=False)
        with self.assertRaises(TamperError):
            joiner.decrypt(b"\x00" * 8, b"\x00" * 32, 0)


if __name__ == "__main__":
    unittest.main()
