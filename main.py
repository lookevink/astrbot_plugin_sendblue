"""Register a Sendblue platform without patching AstrBot core."""

from astrbot.api import AstrBotConfig
from astrbot.api.platform import register_platform_adapter
from astrbot.api.star import Context, Star
from astrbot.core.platform.register import unregister_platform_adapters_by_module


class SendbluePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        from .sendblue_adapter import SendblueAdapter

        register_platform_adapter(
            "sendblue",
            "Direct iMessage/SMS text through Sendblue; configure credentials in the plugin first.",
            default_config_tmpl={
                "id": "sendblue",
                "type": "sendblue",
                "enable": False,
                "listen_host": "127.0.0.1",
                "listen_port": 6198,
            },
            adapter_display_name="Sendblue iMessage / SMS",
            support_streaming_message=False,
        )(SendblueAdapter)
        SendblueAdapter.plugin_config = config
        self._adapter_type = SendblueAdapter

    async def terminate(self):
        """Stop this plugin's active adapters before AstrBot unregisters them."""
        manager = self.context.platform_manager
        for instance in list(manager.platform_insts):
            if isinstance(instance, self._adapter_type):
                await manager.terminate_platform(instance.meta().id)
        self._adapter_type.plugin_config = {}
        unregister_platform_adapters_by_module(self._adapter_type.__module__)
