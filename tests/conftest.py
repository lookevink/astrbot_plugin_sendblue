"""Exercise the plugin through an unmodified AstrBot checkout."""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

HOST = Path(os.environ["ASTRBOT_TEST_HOST"]).resolve()
sys.path.insert(0, str(HOST))
ROOT = Path(__file__).resolve().parents[1]
# Import-time AstrBot defaults must never touch an operator's data directory.
_BOOT = tempfile.TemporaryDirectory(prefix="sendblue-astrbot-test-")
os.environ["ASTRBOT_ROOT"] = _BOOT.name
os.environ["TESTING"] = "true"
os.environ["ASTRBOT_TEST_MODE"] = "true"

from astrbot.core.platform.manager import PlatformManager
from astrbot.core.platform.register import platform_cls_map, platform_registry
from astrbot.core.star import star_manager as sm
from astrbot.core.utils.metrics import Metric

NAME = "astrbot_plugin_sendblue"


@pytest_asyncio.fixture
async def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name == "data" or name.startswith("data."):
            del sys.modules[name]
    destination = tmp_path / "data" / "plugins" / NAME
    shutil.copytree(
        ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".git", "tests", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )
    for directory in (tmp_path / "data", tmp_path / "data/plugins"):
        (directory / "__init__.py").touch()
    (tmp_path / "data/config").mkdir()
    monkeypatch.setattr(Metric, "upload", AsyncMock())
    preferences = {}

    async def get(key, default=None):
        return preferences.get(key, default)

    async def put(key, value):
        preferences[key] = value

    monkeypatch.setattr(sm.sp, "global_get", get)
    monkeypatch.setattr(sm.sp, "global_put", put)
    monkeypatch.setattr(sm, "sync_command_configs", AsyncMock())
    manager = PlatformManager(
        {"platform": [], "platform_settings": {}}, asyncio.Queue()
    )
    context = SimpleNamespace(
        platform_manager=manager,
        get_all_stars=lambda: list(sm.star_registry),
        get_registered_star=lambda name: next(
            (s for s in sm.star_registry if s.name == name), None
        ),
    )
    loader = sm.PluginManager(context, {})
    loader.reserved_plugin_path = str(tmp_path / "no-builtin-plugins")
    assert "sendblue" not in platform_cls_map  # absent on upstream before installation
    success, error = await loader.load(specified_dir_name=NAME)
    assert success, error
    metadata = context.get_registered_star(NAME)
    assert metadata and metadata.activated
    yield SimpleNamespace(
        loader=loader,
        metadata=metadata,
        manager=manager,
        context=context,
        preferences=preferences,
    )
    current = context.get_registered_star(NAME)
    if current:
        await loader._terminate_plugin(current)
        await loader._unbind_plugin(NAME, current.module_path)
    await manager.terminate()
    assert "sendblue" not in platform_cls_map
    assert not any(p.name == "sendblue" for p in platform_registry)


@pytest_asyncio.fixture
async def adapter(plugin, unused_tcp_port):
    plugin.metadata.config.update(
        sendblue_api_key="fixture-key",
        sendblue_api_secret="fixture-secret",
        sendblue_signing_secret="fixture-webhook-secret",
        sendblue_from_number="+15555550100",
        sendblue_allow_from=["+15555550101"],
    )
    config = dict(
        next(p.default_config_tmpl for p in platform_registry if p.name == "sendblue")
    )
    assert config["enable"] is False
    config.update(enable=True, listen_port=unused_tcp_port)
    plugin.manager.platforms_config.append(config)
    await plugin.manager.load_platform(config)
    instance = next(
        p for p in plugin.manager.platform_insts if p.meta().name == "sendblue"
    )
    import httpx

    # A wrong-secret response proves the real listener is bound, without an event.
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                response = await client.post(
                    f"http://127.0.0.1:{unused_tcp_port}/sendblue/webhook", json={}
                )
                assert response.status_code == 401
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.01)
        else:
            pytest.fail("Plugin listener did not start")
    yield instance
