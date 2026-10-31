"""离线联机帧编解码与加密通道：往返、半包/超长/非法帧、大 blob 分片。"""

import socket
import struct
import threading
import unittest

from offcraft.p2p.crypto import P2PError, derive_room_keys, make_channel
from offcraft.p2p.transport import (
    MAX_FRAME,
    MIN_FRAME,
    FrameReader,
    SecureChannel,
    decode_frame,
    encode_frame,
)


def _socketpair():
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    return a, b


class CodecTest(unittest.TestCase):
    def test_roundtrip(self):
        frame = encode_frame(9, b"\x01" * 12, b"cipherbody" + b"\x02" * 16)
        channel, nonce, body = decode_frame(frame[4:])
        self.assertEqual(channel, 9)
        self.assertEqual(nonce, b"\x01" * 12)
        self.assertTrue(body.startswith(b"cipherbody"))

    def test_payload_too_short(self):
        with self.assertRaises(P2PError):
            decode_frame(b"\x00" * (MIN_FRAME - 1))

    def test_encode_oversize_rejected(self):
        with self.assertRaises(P2PError):
            encode_frame(0, b"\x00" * 12, b"\x00" * (MAX_FRAME + 1))


class FrameReaderTest(unittest.TestCase):
    def test_reads_framed_then_eof(self):
        a, b = _socketpair()
        try:
            reader = FrameReader(b)
            raw = encode_frame(1, b"\x11" * 12, b"hello!!!" + b"\x99" * 16)
            a.sendall(raw)
            a.sendall(raw)
            a.shutdown(socket.SHUT_WR)
            first = reader.read()
            second = reader.read()
            self.assertIsNotNone(first)
            self.assertEqual(first[0], 1)
            self.assertEqual(second[0], 1)
            self.assertIsNone(reader.read())  # 干净 EOF
        finally:
            a.close()
            b.close()

    def test_half_packet_raises(self):
        a, b = _socketpair()
        try:
            reader = FrameReader(b)
            payload = (0).to_bytes(4, "big") + b"\x00" * 12 + b"partial"
            a.sendall(struct.pack(">I", len(payload)) + payload)  # 声明长度但少发
            a.close()
            with self.assertRaises(P2PError):
                reader.read()
        finally:
            b.close()

    def test_illegal_length_raises(self):
        a, b = _socketpair()
        try:
            reader = FrameReader(b)
            a.sendall(struct.pack(">I", MAX_FRAME + 1))
            with self.assertRaises(P2PError):
                reader.read()
        finally:
            a.close()
            b.close()


class SecureChannelTest(unittest.TestCase):
    def test_roundtrip_small(self):
        keys = derive_room_keys("psk", "ROOM")
        a, b = _socketpair()
        ch_a = SecureChannel(a, make_channel(keys, is_host=True))
        ch_b = SecureChannel(b, make_channel(keys, is_host=False))
        try:
            ch_a.send(b"mc packet")
            self.assertEqual(ch_b.recv(), b"mc packet")
            ch_b.send(b"reply")
            self.assertEqual(ch_a.recv(), b"reply")
        finally:
            ch_a.close()
            ch_b.close()

    def test_large_blob_reassembles(self):
        keys = derive_room_keys("psk", "ROOM")
        a, b = _socketpair()
        ch_a = SecureChannel(a, make_channel(keys, is_host=True))
        ch_b = SecureChannel(b, make_channel(keys, is_host=False))
        blob = b"X" * (MAX_FRAME + 10_000)  # 跨多个分片帧
        try:
            threading.Thread(target=ch_a.send, args=(blob,), daemon=True).start()
            got = bytearray()
            while len(got) < len(blob):
                piece = ch_b.recv()
                if piece is None:
                    break
                got += piece
            self.assertEqual(bytes(got), blob)
        finally:
            ch_a.close()
            ch_b.close()


if __name__ == "__main__":
    unittest.main()
