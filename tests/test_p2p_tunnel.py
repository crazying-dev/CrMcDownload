"""离线联机隧道泵送：本地 TCP <-> 加密通道的双向转发与关闭传播。"""

import socket
import threading
import unittest

from offcraft.p2p.crypto import derive_room_keys, make_channel
from offcraft.p2p.transport import SecureChannel
from offcraft.p2p.tunnel import forward


def _pair():
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    return a, b


class ForwardTest(unittest.TestCase):
    def test_bidirectional_pump(self):
        keys = derive_room_keys("psk", "ROOM")
        game, fwd_local = _pair()          # 模拟游戏 <-> 隧道本地端
        tunnel_a, tunnel_b = _pair()       # 模拟中继两端（密文管道）

        # 隧道端：forward 把 fwd_local 灌进以“加入者”密钥加密的通道（tunnel_b）
        channel_joiner = SecureChannel(tunnel_b, make_channel(keys, is_host=False))
        cancel = threading.Event()
        worker = threading.Thread(target=forward, args=(fwd_local, channel_joiner, cancel), daemon=True)
        worker.start()

        # 对端：以“房主”密钥读写
        channel_host = SecureChannel(tunnel_a, make_channel(keys, is_host=True))
        try:
            game.sendall(b"client->server")
            self.assertEqual(channel_host.recv(), b"client->server")
            channel_host.send(b"server->client")
            self.assertEqual(game.recv(64), b"server->client")
        finally:
            cancel.set()
            game.close()
            channel_host.close()
            worker.join(3)

    def test_local_close_propagates(self):
        keys = derive_room_keys("psk", "ROOM")
        game, fwd_local = _pair()
        tunnel_a, tunnel_b = _pair()
        channel_joiner = SecureChannel(tunnel_b, make_channel(keys, is_host=False))
        cancel = threading.Event()
        worker = threading.Thread(target=forward, args=(fwd_local, channel_joiner, cancel), daemon=True)
        worker.start()
        channel_host = SecureChannel(tunnel_a, make_channel(keys, is_host=True))
        try:
            game.close()                    # 游戏侧关闭 → forward 应拆除并置位 cancel
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(cancel.is_set())
        finally:
            channel_host.close()


if __name__ == "__main__":
    unittest.main()
