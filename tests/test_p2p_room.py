"""离线联机信令客户端：请求只发向 download_base、create/open/join 载荷、名册解析。"""

import unittest

from offcraft.mirror import Mirror
from offcraft.p2p.room import PeerInfo, RoomClient

BASE = "https://mc.example.com"
DEVICE = "ab" * 16


class _FakeDownloader:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []  # (url, method, body)

    def request_json(self, url, method="GET", *, body=None, headers=None, **_kw):
        self.calls.append((url, method, body))
        action = url.rsplit("/", 1)[-1]
        return self._responses.get(action, {})


class RoomClientTest(unittest.TestCase):
    def setUp(self):
        self.mirror = Mirror(BASE)

    def _client(self, downloader, **kw):
        return RoomClient(downloader=downloader, mirror=self.mirror,
                          name="ROOM9", password="secret", device_id=DEVICE, **kw)

    def test_name_is_canonicalized(self):
        dl = _FakeDownloader({})
        client = RoomClient(downloader=dl, mirror=self.mirror,
                            name="  MyRoom  ", password="p", device_id=DEVICE)
        self.assertEqual(client.name, "myroom")

    def test_create_registers_name_and_device(self):
        dl = _FakeDownloader({"create": {"ok": True, "rooms": []}})
        client = self._client(dl)
        client.create()
        url, method, body = dl.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/p2p/create"))
        self.assertEqual(body["room"], "room9")
        self.assertEqual(body["device_id"], DEVICE)

    def test_open_sends_auth_key_and_device(self):
        dl = _FakeDownloader({"open": {"sid": "a" * 32, "host": "a" * 32, "peers": [],
                                       "relay": {"host": "h", "port": 7}}})
        client = self._client(dl, world="W", port=25565)
        client.open()
        _url, method, body = dl.calls[0]
        self.assertTrue(dl.calls[0][0].endswith("/p2p/open"))
        self.assertEqual(method, "POST")
        self.assertNotIn("is_host", body)
        self.assertEqual(len(bytes.fromhex(body["auth_key"])), 32)
        self.assertEqual(len(bytes.fromhex(body["client_pub"])), 32)
        self.assertEqual(body["device_id"], DEVICE)
        self.assertNotIn("proof", body)
        self.assertEqual(client.sid, "a" * 32)
        self.assertEqual(client.relay["port"], 7)

    def test_join_sends_proof(self):
        peers = [{"pid": "h" * 32, "addr": "1.2.3.4:5", "world": "W", "port": 25565}]
        dl = _FakeDownloader({"join": {"sid": "b" * 32, "host": "h" * 32, "peers": peers,
                                       "relay": {"host": "h", "port": 7}}})
        client = self._client(dl)
        client.join()
        _url, _method, body = dl.calls[0]
        self.assertTrue(dl.calls[0][0].endswith("/p2p/join"))
        self.assertNotIn("auth_key", body)
        self.assertEqual(len(bytes.fromhex(body["proof"])), 32)
        self.assertEqual(client.host_sid, "h" * 32)
        self.assertEqual(len(client.peers), 1)
        self.assertIsInstance(client.peers[0], PeerInfo)
        self.assertEqual(client.peers[0].world, "W")

    def test_list_and_delete(self):
        dl = _FakeDownloader({"list": {"rooms": [], "remaining": 10, "limit": 10},
                              "delete": {"ok": True}})
        client = self._client(dl)
        client.list_rooms()
        self.assertTrue(dl.calls[-1][0].endswith("/p2p/list"))
        self.assertEqual(dl.calls[-1][2]["device_id"], DEVICE)
        client.delete()
        self.assertTrue(dl.calls[-1][0].endswith("/p2p/delete"))
        self.assertEqual(dl.calls[-1][2]["room"], "room9")

    def test_request_relay_and_leave(self):
        dl = _FakeDownloader({
            "join": {"sid": "b" * 32, "host": "h" * 32, "peers": [], "relay": {"host": "h", "port": 7}},
            "relay": {"token": "tok", "relay": {"host": "h", "port": 7}},
            "leave": {"ok": True},
        })
        client = self._client(dl)
        client.join()
        info = client.request_relay("h" * 32)
        self.assertEqual(info["token"], "tok")
        self.assertTrue(dl.calls[-1][0].endswith("/p2p/relay"))
        client.leave()
        self.assertTrue(dl.calls[-1][0].endswith("/p2p/leave"))

    def test_all_calls_on_download_base(self):
        dl = _FakeDownloader({"join": {"sid": "b" * 32, "host": "h" * 32, "peers": [], "relay": {}}})
        client = self._client(dl)
        client.create()
        client.join()
        client.request_relay("h" * 32)
        client.announce()
        client.leave()
        for url, _m, _b in dl.calls:
            self.assertTrue(url.startswith(BASE + "/p2p/"), url)

    def test_empty_name_rejected(self):
        dl = _FakeDownloader({})
        with self.assertRaises(ValueError):
            RoomClient(downloader=dl, mirror=self.mirror, name="  ", password="p", device_id=DEVICE)

    def test_empty_password_rejected(self):
        dl = _FakeDownloader({})
        with self.assertRaises(ValueError):
            RoomClient(downloader=dl, mirror=self.mirror, name="R", password="", device_id=DEVICE)


if __name__ == "__main__":
    unittest.main()
