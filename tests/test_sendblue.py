import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool_executor import BaseFunctionToolExecutor
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.provider import Provider

LINE = "+15555550100"
SENDER = "+15555550101"
SECRET = "fixture-webhook-secret"


def event(**changes):
    return {
        "message_handle": "fixture-inbound-1",
        "from_number": SENDER,
        "to_number": LINE,
        "is_outbound": False,
        "status": "RECEIVED",
        "content": "Remember cobalt",
        "group_id": "",
        **changes,
    }


@pytest_asyncio.fixture
async def client(adapter):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{adapter.port}") as client:
        yield client


class MockProvider(Provider):
    def __init__(self):
        super().__init__({}, {})

    def get_current_key(self):
        return "fixture"

    def set_key(self, key):
        pass

    async def get_models(self):
        return ["fixture"]

    async def text_chat(self, **kwargs):
        return LLMResponse(role="assistant", completion_text="I remember cobalt.")


async def post(client, adapter, payload, secret=SECRET):
    return await client.post(
        "/sendblue/webhook",
        json=payload,
        headers={"sb-signing-secret": secret},
    )


@pytest.mark.asyncio
async def test_plugin_http_to_real_agent_runner_and_reply(adapter, client):
    received = []

    async def provider_endpoint(request):
        assert request.headers["sb-api-key-id"] == "fixture-key"
        assert request.headers["sb-api-secret-key"] == "fixture-secret"
        received.append(await request.json())
        return web.json_response(
            {
                "message_handle": "fixture-outbound-1",
                "status": "QUEUED",
                "error_code": 0,
            }
        )

    app = web.Application()
    app.router.add_post("/api/send-message", provider_endpoint)
    async with TestServer(app) as server:
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(
            base_url=str(server.make_url("/")),
            headers={
                "sb-api-key-id": "fixture-key",
                "sb-api-secret-key": "fixture-secret",
            },
        )
        assert (await post(client, adapter, event())).status_code == 200
        incoming = adapter._event_queue.get_nowait()
        assert incoming.get_sender_id() == SENDER
        assert incoming.message_obj.self_id == LINE
        assert incoming.message_obj.message_id == "fixture-inbound-1"
        assert incoming.unified_msg_origin == f"sendblue:FriendMessage:{SENDER}"
        provider = MockProvider()
        provider.text_chat = AsyncMock(
            return_value=LLMResponse(
                role="assistant", completion_text="I remember cobalt."
            )
        )
        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=provider,
            request=ProviderRequest(prompt=incoming.message_str, contexts=[]),
            run_context=ContextWrapper(context=SimpleNamespace(event=incoming)),
            tool_executor=BaseFunctionToolExecutor(),
            agent_hooks=BaseAgentRunHooks(),
            streaming=False,
        )
        async for chain in run_agent(runner):
            await incoming.send(chain)
        assert received == [
            {"number": SENDER, "from_number": LINE, "content": "I remember cobalt."}
        ]
        assert runner.done()
        assert runner.run_context.messages[-1].role == "assistant"
        assert (await post(client, adapter, event())).status_code == 200
        assert adapter._event_queue.empty()
        # Second inbound turn uses the same native session and supplies the first
        # runner's transcript, without replacing the host agent runner.
        history = [m.model_dump() for m in runner.run_context.messages]
        assert (
            await post(
                client,
                adapter,
                event(message_handle="fixture-inbound-2", content="What word?"),
            )
        ).status_code == 200
        followup = adapter._event_queue.get_nowait()
        assert followup.unified_msg_origin == incoming.unified_msg_origin
        await runner.reset(
            provider=provider,
            request=ProviderRequest(prompt=followup.message_str, contexts=history),
            run_context=ContextWrapper(context=SimpleNamespace(event=followup)),
            tool_executor=BaseFunctionToolExecutor(),
            agent_hooks=BaseAgentRunHooks(),
            streaming=False,
        )
        async for chain in run_agent(runner):
            await followup.send(chain)
        assert received[-1]["number"] == SENDER
        assert received[-1]["content"] == "I remember cobalt."
        assert "cobalt" in json.dumps(
            provider.text_chat.call_args.kwargs["contexts"],
            default=lambda value: value.model_dump(),
        )
        await adapter.send_by_session(
            incoming.session, MessageChain().message("Reminder")
        )
        assert received[-1]["content"] == "Reminder"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,status",
    [
        ({"is_outbound": True}, 200),
        ({"is_outbound": "false"}, 200),
        ({"status": "DELIVERED"}, 200),
        ({"group_id": "group-1"}, 200),
        ({"to_number": "+15555550999"}, 200),
        ({"from_number": "+15555550999"}, 200),
        ({"message_handle": ""}, 400),
        ({"content": {}}, 400),
        ({"media_url": []}, 400),
    ],
)
async def test_filtered_events_never_enter_queue(adapter, client, changes, status):
    assert (await post(client, adapter, event(**changes))).status_code == status
    assert adapter._event_queue.empty()


@pytest.mark.asyncio
async def test_auth_and_size_limit_before_json_parse(adapter, client):
    assert (await post(client, adapter, event(), secret="wrong")).status_code == 401
    assert (await post(client, adapter, [])).status_code == 400

    async def chunks():
        yield b'{"content":"'
        yield b"x" * 65537

    response = await client.post(
        "/sendblue/webhook",
        content=chunks(),
        headers={"sb-signing-secret": SECRET},
    )
    assert response.status_code == 413
    assert adapter._event_queue.empty()


@pytest.mark.asyncio
async def test_backpressure_can_retry_and_media_does_not_download(adapter, client):
    for n in range(128):
        assert (
            await post(client, adapter, event(message_handle=f"m-{n}"))
        ).status_code == 200
    assert (
        await post(client, adapter, event(message_handle="retry"))
    ).status_code == 503
    while not adapter._event_queue.empty():
        adapter._event_queue.get_nowait()
    assert (
        await post(
            client,
            adapter,
            event(
                message_handle="retry",
                content=None,
                media_url="http://169.254.169.254/secret",
            ),
        )
    ).status_code == 200
    incoming = adapter._event_queue.get_nowait()
    assert "supports text only" in incoming.message_str
    assert "169.254" not in incoming.message_str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        [],
        {},
        {"message_handle": "m", "status": "ERROR"},
        {"message_handle": "m", "status": "QUEUED", "error_code": 400},
    ],
)
async def test_send_requires_provider_acceptance(adapter, result):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=result)

    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://api.sendblue.com"
    )
    with pytest.raises(RuntimeError, match="did not confirm"):
        await adapter.send_text(SENDER, MessageChain().message("hello"))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_is_not_retried_and_chunks_stop(adapter):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) == 2:
            raise httpx.ReadTimeout("fixture timeout")
        return httpx.Response(200, json={"message_handle": "m", "status": "QUEUED"})

    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://api.sendblue.com"
    )
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await adapter.send_text(SENDER, MessageChain().message("x" * 4500))
    assert len(calls) == 2
    assert len(calls[0]["content"]) == 2000


async def test_missing_credentials_fail_closed(plugin):
    from astrbot.core.platform.register import platform_cls_map

    with pytest.raises(ValueError, match="requires"):
        platform_cls_map["sendblue"]({}, {}, asyncio.Queue())


async def test_blank_install_is_disabled_and_masks_only_plugin_secrets(plugin):
    from pathlib import Path

    from astrbot.core.platform.register import platform_registry

    template = dict(
        next(p.default_config_tmpl for p in platform_registry if p.name == "sendblue")
    )
    assert template["enable"] is False
    assert template["listen_host"] == "127.0.0.1"
    assert not any("secret" in name or "api_key" in name for name in template)
    await plugin.manager.load_platform(template)
    assert plugin.manager.platform_insts == []
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    assert all(
        schema[name]["secret"]
        for name in (
            "sendblue_api_key",
            "sendblue_api_secret",
            "sendblue_signing_secret",
        )
    )


async def test_plugin_reload_and_uninstall_release_listener(plugin, adapter, client):
    from pathlib import Path

    from astrbot.core.platform.register import platform_cls_map, platform_registry

    name = plugin.metadata.name
    plugin.metadata.config.save_config()
    config = plugin.manager.platforms_config[0]
    assert (await post(client, adapter, event())).status_code == 200
    success, error = await plugin.loader.reload(name)
    assert success, error
    assert adapter.client.is_closed
    assert plugin.manager.platform_insts == []
    with pytest.raises(httpx.ConnectError):
        await client.post("/sendblue/webhook", json={})
    assert len([p for p in platform_registry if p.name == "sendblue"]) == 1
    # The documented Bot disable/enable action restarts it using saved plugin settings.
    await plugin.manager.reload(config)
    current = plugin.manager.platform_insts[0]
    for _ in range(100):
        try:
            response = await post(client, current, event(message_handle="after-reload"))
            assert response.status_code == 200
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.01)
    else:
        pytest.fail("Reloaded listener did not bind")
    assert current is not adapter
    assert (
        current._event_queue.get_nowait().message_obj.message_id == "fixture-inbound-1"
    )
    assert current._event_queue.get_nowait().message_obj.message_id == "after-reload"
    await plugin.loader.turn_off_plugin(name)
    assert current.client.is_closed
    assert "sendblue" not in platform_cls_map
    assert plugin.manager.platform_insts == []
    await plugin.loader.turn_on_plugin(name)
    assert "sendblue" in platform_cls_map
    assert plugin.manager.platform_insts == []
    await plugin.loader.uninstall_plugin(name)
    assert current.client.is_closed
    assert plugin.manager.platform_insts == []
    assert "sendblue" not in platform_cls_map
    assert not (Path(plugin.loader.plugin_store_path) / name).exists()
    with pytest.raises(httpx.ConnectError):
        await client.post("/sendblue/webhook", json={})


async def test_listener_concurrency_and_slow_body_are_bounded(
    adapter, client, monkeypatch
):
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def stalled(request):
        entered.set()
        await gate.wait()
        return "ok", 200

    monkeypatch.setattr(adapter, "webhook_callback", stalled)
    tasks = [asyncio.create_task(post(client, adapter, event())) for _ in range(16)]
    try:
        for _ in range(100):
            if adapter._inflight == 16:
                break
            await asyncio.sleep(0.01)
        assert adapter._inflight == 16
        assert (await post(client, adapter, event())).status_code == 503
    finally:
        gate.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
    assert adapter._inflight == 0

    async def timeout(request):
        raise asyncio.TimeoutError

    monkeypatch.setattr(adapter, "webhook_callback", timeout)
    assert (await post(client, adapter, event())).status_code == 408
    assert adapter._inflight == 0


async def test_archive_install_uses_real_installer_and_dependency_precheck(
    plugin, tmp_path, monkeypatch
):
    from pathlib import Path
    from zipfile import ZipFile

    from astrbot.core.platform.register import platform_cls_map
    from astrbot.core.star import star_manager as sm

    await plugin.loader.uninstall_plugin(plugin.metadata.name, delete_config=True)
    assert "sendblue" not in platform_cls_map
    archive = tmp_path / "plugin.zip"
    source = Path(__file__).parents[1]
    with ZipFile(archive, "w") as bundle:
        for path in source.iterdir():
            if path.is_file() and not path.name.startswith("."):
                bundle.write(path, f"astrbot_plugin_sendblue-main/{path.name}")
    monkeypatch.setattr(
        sm, "get_astrbot_system_tmp_path", lambda: str(tmp_path / "staging")
    )
    result = await plugin.loader.install_plugin_from_file(str(archive))
    assert result["repo"] == "https://github.com/lookevink/astrbot_plugin_sendblue"
    assert "sendblue" in platform_cls_map
    assert plugin.manager.platform_insts == []
    assert not archive.exists()
