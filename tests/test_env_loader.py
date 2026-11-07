"""``.env`` 自动加载：手动 ``uv run main.py`` 时也能读到部署配置。"""

import os

from server_mirror.config import load_env_file


def test_load_env_file_parses_common_syntax(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释行\n"
        "\n"
        "MIRROR_PLUGIN_DIR=./plugins\n"
        'export MIRROR_LISTEN="0.0.0.0:8081"\n'
        "MIRROR_PLUGIN_DIR=/obj/CrMcDownload/plugins\n"
        "NO_SEPARATOR\n",
        encoding="utf-8",
    )
    keys = ("MIRROR_PLUGIN_DIR", "MIRROR_LISTEN")
    originals = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)

    try:
        loaded = load_env_file(env, tmp_path / "missing.env")

        # 同一文件内后写的键生效
        assert loaded["MIRROR_PLUGIN_DIR"] == "/obj/CrMcDownload/plugins"
        assert os.environ["MIRROR_PLUGIN_DIR"] == "/obj/CrMcDownload/plugins"
        assert os.environ["MIRROR_LISTEN"] == "0.0.0.0:8081"
        assert "NO_SEPARATOR" not in loaded
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_load_env_file_keeps_existing_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MIRROR_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MIRROR_TOKEN", "from-process")

    load_env_file(env)

    assert os.environ["MIRROR_TOKEN"] == "from-process"
