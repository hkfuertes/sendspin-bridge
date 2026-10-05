from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from sendspin_bridge.registry import Group, Registry, Speaker, Stereo
from sendspin_bridge.web import ConfigWeb, registry_from_payload, registry_to_payload


class ConfigPayloadTests(unittest.TestCase):
    def test_payload_round_trip_saves_a_group(self) -> None:
        payload = {
            "exposed_suffix": " (Sendspin)",
            "speakers": [
                {
                    "id": "kitchen",
                    "exposed_name": "Kitchen",
                    "direction": "outbound",
                    "port": 0,
                    "client_id": "client-kitchen",
                    "exposed": False,
                    "delay_ms": -20,
                    "endpoint": {"instance": "", "host": "192.0.2.10", "port": 8928, "path": "/sendspin"},
                }
            ],
            "groups": [{"id": "home", "exposed_name": "Home", "port": 0, "speaker_ids": ["kitchen"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            registry = registry_from_payload(payload, str(path), 7000, 10)
            registry.save()
            restored = Registry.load(path)
            restored_payload = registry_to_payload(restored)
            self.assertEqual(restored_payload["exposed_suffix"], " (Sendspin)")
            self.assertEqual(restored_payload["speakers"][0]["exposed_name"], "Kitchen")
            self.assertFalse(restored_payload["speakers"][0]["exposed"])
            self.assertEqual(restored_payload["speakers"][0]["delay_ms"], -20)
            self.assertEqual(restored_payload["groups"][0]["exposed_name"], "Home")
            self.assertEqual(restored_payload["groups"][0]["speaker_ids"], ["kitchen"])

    def test_removing_a_speaker_preserves_other_group_members(self) -> None:
        payload = {
            "speakers": [{"id": "kitchen"}, {"id": "bedroom"}],
            "groups": [{"id": "home", "speaker_ids": ["kitchen", "bedroom"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            registry_from_payload(payload, str(path), 7000, 10).save()
            payload["speakers"] = payload["speakers"][1:]
            payload["groups"][0]["speaker_ids"].remove("kitchen")
            registry_from_payload(payload, str(path), 7000, 10).save()
            restored = Registry.load(path)
            self.assertIsNone(restored.speaker("kitchen"))
            self.assertEqual(restored.groups()[0].speaker_ids, ["bedroom"])

    def test_payload_round_trip_keeps_a_stereo_pair_in_multiroom(self) -> None:
        payload = {
            "speakers": [{"id": "left"}, {"id": "right"}, {"id": "kitchen"}],
            "stereos": [{"id": "pair", "left_id": "left", "right_id": "right", "exposed_name": "Living room", "port": 0, "exposed": False}],
            "groups": [{"id": "home", "speaker_ids": ["left", "right", "kitchen"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            registry_from_payload(payload, str(path), 7000, 10).save()
            restored = registry_to_payload(Registry.load(path))
            self.assertEqual(restored["stereos"][0]["exposed_name"], "Living room")
            self.assertIs(restored["stereos"][0]["exposed"], False)
            self.assertEqual(restored["stereos"][0]["port"], 7030)
            self.assertEqual(restored["groups"][0]["speaker_ids"], ["left", "right", "kitchen"])
            payload["stereos"][0]["right_id"] = "missing"
            with self.assertRaisesRegex(ValueError, "existing"):
                registry_from_payload(payload, str(path), 7000, 10)

    def test_payload_rejects_invalid_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "exposed must be true or false"):
                registry_from_payload({"speakers": [{"id": "kitchen", "exposed": "false"}]}, str(Path(directory) / "config.xml"), 7000, 10)

    def test_payload_rejects_an_unknown_group_member(self) -> None:
        payload = {"exposed_suffix": "", "speakers": [], "groups": [{"id": "home", "speaker_ids": ["missing"]}]}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unknown"):
                registry_from_payload(payload, str(Path(directory) / "config.xml"), 7000, 10)


class GroupVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_speaker_volume_returns_every_level_it_moved(self) -> None:
        set_volume = Mock(return_value={"left": 60, "right": 60})
        config = ConfigWeb(
            config_path="/tmp/unused.xml", port_base=7000, port_range=10,
            host="127.0.0.1", port=8080, advertised_host="127.0.0.1",
            registry=lambda: Registry(), speaker_state=lambda _: {}, set_speaker_volume=set_volume,
            set_group_volume=Mock(), set_stereo_volume=Mock(), replace_registry=Mock(), restart=Mock(),
        )
        request = Mock(match_info={"speaker_id": "left"})
        request.json = AsyncMock(return_value={"volume": 60})
        response = await config._put_volume(request)
        self.assertEqual(json.loads(response.body), {"volume": 60, "speakers": {"left": 60, "right": 60}})
        set_volume.return_value = None
        self.assertEqual((await config._put_volume(request)).status, 409)

    async def test_group_volume_validates_members_and_returns_live_levels(self) -> None:
        group = Group(id="home", speaker_ids=["kitchen", "bedroom"])
        set_volume = Mock(return_value={"kitchen": 30, "bedroom": 70})
        config = ConfigWeb(
            config_path="/tmp/unused.xml", port_base=7000, port_range=10,
            host="127.0.0.1", port=8080, advertised_host="127.0.0.1",
            registry=lambda: Registry(speakers=[Speaker(id="kitchen"), Speaker(id="bedroom")], groups=[group]),
            speaker_state=lambda _: {}, set_speaker_volume=Mock(), set_group_volume=set_volume,
            set_stereo_volume=Mock(), replace_registry=Mock(), restart=Mock(),
        )
        request = Mock(match_info={"group_id": "home"})
        request.json = AsyncMock(return_value={"volume": 50, "speaker_ids": ["kitchen", "bedroom"]})
        response = await config._put_group_volume(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {"volume": 50, "speakers": {"kitchen": 30, "bedroom": 70}})
        set_volume.assert_called_once_with(["kitchen", "bedroom"], 50, "home")

        request.json.return_value = {"volume": 50, "speaker_ids": ["kitchen"]}
        self.assertEqual((await config._put_group_volume(request)).status, 409)
        request.json.return_value = {"volume": 101, "speaker_ids": ["kitchen", "bedroom"]}
        self.assertEqual((await config._put_group_volume(request)).status, 400)
        set_volume.assert_called_once()

        request.json.return_value = {"volume": 50, "speaker_ids": ["kitchen", "bedroom"]}
        set_volume.return_value = {}
        self.assertEqual((await config._put_group_volume(request)).status, 409)

    async def test_stereo_volume_rejects_stale_members_and_updates_live_levels(self) -> None:
        set_volume = Mock(return_value={"left": 30, "right": 70})
        config = ConfigWeb(
            config_path="/tmp/unused.xml", port_base=7000, port_range=10,
            host="127.0.0.1", port=8080, advertised_host="127.0.0.1",
            registry=lambda: Registry(stereos=[Stereo("pair", "left", "right")]),
            speaker_state=lambda _: {}, set_speaker_volume=Mock(), set_group_volume=Mock(),
            set_stereo_volume=set_volume, replace_registry=Mock(), restart=Mock(),
        )
        request = Mock(match_info={"stereo_id": "pair"})
        request.json = AsyncMock(return_value={"volume": 50, "speaker_ids": ["left", "right"]})
        response = await config._put_stereo_volume(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {"volume": 50, "speakers": {"left": 30, "right": 70}})
        set_volume.assert_called_once_with(["left", "right"], 50, "pair")
        request.json.return_value = {"volume": 50, "speaker_ids": ["right", "left"]}
        self.assertEqual((await config._put_stereo_volume(request)).status, 409)
        request.json.return_value = {"volume": 101, "speaker_ids": ["left", "right"]}
        self.assertEqual((await config._put_stereo_volume(request)).status, 400)
        set_volume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
