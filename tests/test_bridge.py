import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge import govee_mqtt_bridge as bridge


def lamp(fingerprint):
    return SimpleNamespace(
        fingerprint=fingerprint,
        as_dict=lambda: {"on": True, "colorTemperature": 3500},
        segment_count=2,
        turn_on=AsyncMock(),
        turn_off=AsyncMock(),
        set_scene=AsyncMock(),
        set_temperature=AsyncMock(),
        set_rgb_color=AsyncMock(),
        set_segment_rgb_color=AsyncMock(),
    )


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scene_name_goes_directly_to_library(self):
        device = lamp("AB")
        await bridge._apply_device_command(device, {"scene": "Sunset"})
        device.turn_on.assert_awaited_once()
        device.set_scene.assert_awaited_once_with("Sunset")

    async def test_kelvin_limits_and_precedence(self):
        device = lamp("AB")
        for value in (2000, 6500):
            await bridge._apply_device_command(
                device, {"color_temp": value, "color_temperature": 4000, "color": [1, 2, 3]}
            )
        await bridge._apply_device_command(device, {"color_temp_mired": 3200})
        for value in (1999, 6501, 4000.5, None):
            await bridge._apply_device_command(device, {"color_temp": value})
        self.assertEqual(
            [call.args for call in device.set_temperature.await_args_list],
            [(2000,), (6500,), (3200,)],
        )
        device.set_rgb_color.assert_not_awaited()
        self.assertEqual(bridge._build_device_state(device)["color_temp"], 3500)
        self.assertNotIn("colorTemperature", bridge._extract_info(device))

    async def test_initialization_has_no_device_effect(self):
        device = lamp("A:B")
        controller = SimpleNamespace(devices=[device])
        mqtt = SimpleNamespace(publish=AsyncMock())
        await bridge._publish_set_trigger(mqtt, device)
        self.assertEqual(
            [call.args[0] for call in mqtt.publish.await_args_list],
            ["govee/AB/set", "govee/AB/segment/1/set", "govee/AB/segment/2/set"],
        )
        with patch.object(bridge, "publish_device_state", new_callable=AsyncMock) as publish:
            for call in mqtt.publish.await_args_list:
                self.assertEqual(json.loads(call.args[1]), {})
                self.assertFalse(call.kwargs["retain"])
                await bridge.handle_command(controller, mqtt, *call.args)
            publish.assert_not_awaited()
        device.turn_off.assert_not_awaited()
        device.set_rgb_color.assert_not_awaited()

    async def test_commands_route_by_device_and_segment(self):
        first, second = lamp("A:B"), lamp("C:D")
        controller = SimpleNamespace(devices={"first": first, "second": second})
        with patch.object(bridge, "publish_device_state", new_callable=AsyncMock):
            await bridge.handle_command(controller, Mock(), "govee/AB/set", '{"on":false}')
            await bridge.handle_command(
                controller, Mock(), "govee/CD/segment/2/set", '{"color":[1,2,3]}'
            )
        first.turn_off.assert_awaited_once()
        second.turn_off.assert_not_awaited()
        first.set_segment_rgb_color.assert_not_awaited()
        second.set_segment_rgb_color.assert_awaited_once_with(2, 1, 2, 3)

    async def test_receiver_is_ready_before_subscribe_and_skips_retained_commands(self):
        mqtt = SimpleNamespace(subscribe=AsyncMock())
        topic = "govee/AB/set"

        class EndLoop(Exception):
            pass

        async def stream():
            yield SimpleNamespace(topic=topic, payload=b'{"on":false}', retain=True)
            yield SimpleNamespace(topic=topic, payload=b'{"on":true}', retain=False)
            raise EndLoop

        @asynccontextmanager
        async def messages():
            mqtt.subscribe.assert_not_awaited()
            yield stream()

        @asynccontextmanager
        async def client(*args, **kwargs):
            yield mqtt

        mqtt.messages = messages
        controller = Mock()
        with (
            patch.object(bridge, "MQTTClient", client),
            patch.object(bridge, "handle_command", new_callable=AsyncMock) as handle,
            self.assertRaises(EndLoop),
        ):
            await bridge.mqtt_command_loop(controller)
        handle.assert_awaited_once_with(controller, mqtt, topic, '{"on":true}')
