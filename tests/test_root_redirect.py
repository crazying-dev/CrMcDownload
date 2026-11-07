"""根路径 ``/`` 直接 302 到官网 ``/page/``（不再返回 400「缺少路由前缀」）。"""

from server_mirror.api import ApiRouter


def test_root_redirects_to_page():
    router = ApiRouter(settings=object(), db=None)

    assert router.handles("/")
    assert router.handles("/?from=root")

    status, headers, body = router.dispatch("GET", "/?from=root", {}, None)

    assert status == 302
    assert headers["Location"] == "/page/"
    assert headers["Cache-Control"] == "no-store"
    assert body == b""
