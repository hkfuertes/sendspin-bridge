from __future__ import annotations

import asyncio
import socket
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import aiohttp
import numpy as np
from aiosendspin.models.core import ClientHelloMessage, ClientHelloPayload
from aiosendspin.server import VolumeChangedEvent

from sendspin_bridge.app import AirPlayInput, Config, GroupTarget, Manager, Target
from sendspin_bridge.raop import Receiver, VOLUME
from sendspin_bridge.audio import CHUNK_SAMPLES, GroupBuffer
from sendspin_bridge.registry import Group, INBOUND, Registry, Speaker, Stereo


class FakeTarget:
    def __init__(self, volume: int = 40, connected: bool = True) -> None:
        self.player = object() if connected else None
        self.volume = volume
        self.playing = False
        self.input = Mock()
        self.calls: list[int] = []

    def set_volume(self, volume: int) -> None:
        self.calls.append(volume)
        self.volume = volume


class ManagerVolumeTests(unittest.TestCase):
    def test_only_unpaired_exposed_speakers_get_an_input(self) -> None:
        manager = object.__new__(Manager)
        self.assertIsNone(Target(manager, Speaker(id="kitchen", exposed=False), []).input)
        self.assertIsNotNone(Target(manager, Speaker(id="bedroom", exposed=True), []).input)
        speaker = Speaker(id="left", exposed=True)
        self.assertIsNone(Target(manager, speaker, [], paired=True).input)
        self.assertTrue(speaker.exposed)  # Unpairing restores the saved preference.

    def test_individual_volume_is_live_only(self) -> None:
        manager = object.__new__(Manager)
        kitchen = FakeTarget()
        offline = FakeTarget(connected=False)
        manager.targets = {"kitchen": kitchen, "offline": offline}
        manager.stereo_targets = {}

        self.assertEqual(manager.speaker_state("kitchen"), {"connected": True, "volume": 40})
        kitchen.playing = True
        self.assertEqual(manager.set_speaker_volume("kitchen", 73), {"kitchen": 73})
        self.assertEqual(kitchen.calls, [73])
        kitchen.input.send_volume.assert_called_once_with(73)
        kitchen.playing = False
        manager.set_speaker_volume("kitchen", 72)
        self.assertEqual(kitchen.calls, [73, 72])
        kitchen.input.send_volume.assert_called_once()
        self.assertEqual(manager.speaker_state("kitchen")["volume"], 72)
        self.assertIsNone(manager.set_speaker_volume("offline", 50))
        self.assertIsNone(manager.set_speaker_volume("missing", 50))

    def test_group_volume_preserves_member_balance_and_skips_offline(self) -> None:
        manager = object.__new__(Manager)
        kitchen, bedroom, offline = FakeTarget(20), FakeTarget(60), FakeTarget(90, connected=False)
        manager.targets = {"kitchen": kitchen, "bedroom": bedroom, "offline": offline}
        group = Mock(active=True)
        manager.group_targets = {"salon": group}
        manager.stereo_targets = {}

        self.assertEqual(manager.set_group_volume(["kitchen", "bedroom", "offline"], 50, "salon"), {"kitchen": 30, "bedroom": 70})
        self.assertEqual((kitchen.calls, bedroom.calls, offline.calls), ([30], [70], []))
        group.input.send_volume.assert_called_once_with(50)
        for member in (kitchen, bedroom, offline):
            member.input.send_volume.assert_not_called()
        manager.set_group_volume(["kitchen", "bedroom"], 60)  # AirPlay-sent volume must not echo back.
        group.active = False
        manager.set_group_volume(["kitchen", "bedroom"], 65, "salon")
        group.input.send_volume.assert_called_once()
        self.assertEqual(manager.set_group_volume(["missing", "offline"], 50, "salon"), {})

    def test_stereo_volume_notifies_only_its_active_airplay_target(self) -> None:
        manager = object.__new__(Manager)
        left, right = FakeTarget(20), FakeTarget(60)
        manager.targets = {"left": left, "right": right}
        stereo = Mock(active=True, channels={"left": (0, "right"), "right": (1, "left")})
        manager.stereo_targets = {"pair": stereo}
        self.assertEqual(manager.set_stereo_volume(["left", "right"], 50, "pair"), {"left": 50, "right": 50})
        stereo.input.send_volume.assert_called_once_with(50)
        left.input.send_volume.assert_not_called()
        right.input.send_volume.assert_not_called()

    def test_stereo_halves_share_one_volume(self) -> None:
        manager = object.__new__(Manager)
        pair = Stereo("pair", "left", "right")
        manager.stereo_targets = {"pair": GroupTarget(manager, Group("pair", speaker_ids=["left", "right"]), [pair], key="stereo:pair")}
        manager.targets = {}
        for speaker_id, volume in (("left", 30), ("right", 70)):
            target = manager.targets[speaker_id] = Target(manager, Speaker(id=speaker_id, exposed=False), [], paired=True)
            target.player, target.volume = Mock(), volume
        left, right = manager.targets["left"], manager.targets["right"]
        sent = lambda target: [item.args[0] for item in target.player.group.group_role.return_value.set_volume.call_args_list]

        right.volume_ready_at = time.monotonic() + 60
        manager.equalize_stereo("left")  # Right is still reporting its connect-time volume.
        self.assertEqual((sent(left), sent(right)), ([], []))
        right.volume_ready_at = 0
        manager.equalize_stereo("right")  # Pairing or reconnecting settles on the lower volume.
        self.assertEqual((left.volume, right.volume, sent(left), sent(right)), (30, 30, [], [30]))

        left._on_player_event(left.player, VolumeChangedEvent(volume=45, muted=False))  # Left's own buttons.
        self.assertEqual(sent(right), [30, 45])
        right._on_player_event(right.player, VolumeChangedEvent(volume=30, muted=False))  # Late echo of our command.
        self.assertEqual(sent(left), [])
        self.assertEqual(manager.set_speaker_volume("right", 60), {"left": 60, "right": 60})

    def test_incoming_airplay_volume_does_not_echo(self) -> None:
        target = Target(Mock(), Speaker(id="kitchen"), [])
        target.input = Mock(events=lambda: [(VOLUME, 0.60)])
        target.handle_events()
        self.assertEqual(target.volume, 60)
        target.input.send_volume.assert_not_called()


class StereoRoutingTests(unittest.TestCase):
    def test_stereo_pair_and_multiroom_share_timestamp_but_not_channels(self) -> None:
        manager = object.__new__(Manager)
        manager.targets = {}
        pair = Stereo("pair", "left", "right")
        group = GroupTarget(manager, Group("home", speaker_ids=["left", "right", "kitchen"]), [pair])
        group.active = True
        audio = np.empty(CHUNK_SAMPLES, dtype="<i2")
        audio[::2], audio[1::2] = 100, 300
        group.buffer = GroupBuffer(lambda _frames: audio.tobytes())
        for speaker_id in ("left", "right", "kitchen"):
            target = Target(manager, Speaker(id=speaker_id, exposed=False), [group])
            target.player = object()
            manager.targets[speaker_id] = target
        for speaker_id, left, right in (("left", 100, 100), ("right", 300, 300), ("kitchen", 100, 300)):
            with self.subTest(speaker=speaker_id):
                result = np.frombuffer(manager.targets[speaker_id].render(0), dtype="<i2")
                self.assertTrue(np.all(result[::2] == left))
                self.assertTrue(np.all(result[1::2] == right))
        stereo = GroupTarget(manager, Group("pair", speaker_ids=["left", "right"]), [pair], key="stereo:pair")
        stereo.active = True
        stereo.buffer = GroupBuffer(lambda _frames: audio.tobytes())
        manager.targets["left"].groups.append(stereo)
        manager.targets["right"].groups.append(stereo)
        solo = np.empty(CHUNK_SAMPLES, dtype="<i2")
        solo[::2], solo[1::2] = 50, 150
        manager.targets["left"].playing = True
        manager.targets["left"].input = Mock(read_pcm=lambda: solo.tobytes())
        mixed = np.frombuffer(manager.targets["left"].render(0), dtype="<i2")
        self.assertTrue(np.all(mixed[::2] == 250))  # Individual L + multiroom L + stereo L.
        self.assertTrue(np.all(mixed[1::2] == 350))  # Individual R + multiroom L + stereo L.
        manager.targets["left"].playing = False
        manager.targets["right"].player = None
        fallback = np.frombuffer(manager.targets["left"].render(0), dtype="<i2")
        self.assertTrue(np.all(fallback[::2] == 200))
        self.assertTrue(np.all(fallback[1::2] == 600))


class VolumeFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesces_feedback_and_waits_before_receiver_close(self) -> None:
        airplay = AirPlayInput(Mock(), "salon", "Salon", 7030)
        receiver = Mock()
        airplay.receiver = receiver
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_notify(fn, level):
            started.set()
            await release.wait()
            fn(level)

        with patch("sendspin_bridge.app.asyncio.to_thread", side_effect=slow_notify):
            airplay.send_volume(10)
            await started.wait()
            airplay.send_volume(30)
            airplay.send_volume(50)
            release.set()
            await airplay.close()

        receiver.notify_volume.assert_has_calls([call(0.1), call(0.5)])
        self.assertEqual(receiver.notify_volume.call_count, 2)
        receiver.close.assert_called_once()
        self.assertIsNone(airplay.receiver)
        self.assertIsNone(airplay._volume_task)

    async def test_raop_boundary_accepts_normalized_volume(self) -> None:
        receiver = object.__new__(Receiver)
        receiver._lib = Mock()
        receiver._ffi = Mock(NULL=object())
        receiver._receiver = object()
        receiver.notify_volume(0.42)
        receiver._lib.bridge_receiver_notify_volume.assert_called_once_with(receiver._receiver, 0.42)


class ManagerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpairing_restores_only_previously_exposed_individual_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = Manager(Config(config_path=f"{temp}/config.xml"))
            left, right = Speaker(id="left", exposed=True), Speaker(id="right", exposed=False)
            manager.registry = Registry(speakers=[left, right], stereos=[Stereo("pair", "left", "right")])
            with patch.object(AirPlayInput, "start", new_callable=AsyncMock):
                paired = await manager._ensure_target(left)
                self.assertIsNone(paired.input)
                await paired.close()
                manager.targets.clear()
                manager.registry = Registry(speakers=[left, right])
                unpaired = await manager._ensure_target(left)
                hidden = await manager._ensure_target(right)
                self.assertIsNotNone(unpaired.input)
                self.assertIsNone(hidden.input)
                await unpaired.close()
                await hidden.close()

    async def test_early_inbound_player_joins_stereo_and_multiroom(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = Manager(Config(config_path=f"{temp}/config.xml"))
            speaker = Speaker(id="left", direction=INBOUND, exposed=True)
            other = Speaker(id="right", direction=INBOUND, exposed=False)
            manager.registry = Registry(
                speakers=[speaker, other], stereos=[Stereo("pair", "left", "right")],
                groups=[Group("home", speaker_ids=["left", "right"])],
            )

            async def connect_early(**_):
                await manager._ensure_target(speaker)

            server = Mock(start_server=AsyncMock(side_effect=connect_early), close=AsyncMock())
            server.add_event_listener.return_value = lambda: None
            web = Mock(start=AsyncMock(), close=AsyncMock(), url="http://localhost")
            with (
                patch("sendspin_bridge.app._load_identity"),
                patch("sendspin_bridge.app.FileServerPairingStore.open", new_callable=AsyncMock),
                patch("sendspin_bridge.app.lan_ipv4", return_value="127.0.0.1"),
                patch("sendspin_bridge.app.SendspinServer", return_value=server),
                patch("sendspin_bridge.app.ConfigWeb", return_value=web),
                patch.object(GroupTarget, "start", new_callable=AsyncMock),
            ):
                try:
                    await manager.start()
                    manager.stereo_targets["pair"].active = True
                    self.assertTrue(manager.targets[speaker.id].active)
                    self.assertIsNone(manager.targets[speaker.id].input)
                    self.assertEqual(len(manager.targets[speaker.id].groups), 2)
                finally:
                    await manager.close()

    async def test_hidden_stereo_stays_a_pair_without_its_own_airplay_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = Manager(Config(config_path=f"{temp}/config.xml"))
            manager.registry = Registry(
                speakers=[Speaker(id=speaker_id) for speaker_id in ("a", "b", "c", "d")],
                stereos=[Stereo("shown", "a", "b"), Stereo("hidden", "c", "d", exposed=False)],
                groups=[Group("home", speaker_ids=["a", "b", "c", "d"])],
            )
            server = Mock(start_server=AsyncMock(), close=AsyncMock())
            server.add_event_listener.return_value = lambda: None
            web = Mock(start=AsyncMock(), close=AsyncMock(), url="http://localhost")
            with (
                patch("sendspin_bridge.app._load_identity"),
                patch("sendspin_bridge.app.FileServerPairingStore.open", new_callable=AsyncMock),
                patch("sendspin_bridge.app.lan_ipv4", return_value="127.0.0.1"),
                patch("sendspin_bridge.app.SendspinServer", return_value=server),
                patch("sendspin_bridge.app.ConfigWeb", return_value=web),
                patch.object(AirPlayInput, "start", new_callable=AsyncMock) as advertise,
            ):
                try:
                    await manager.start()
                    self.assertEqual(advertise.await_count, 2)  # The shown pair and the group only.
                    self.assertIs(manager._stereo("c"), manager.stereo_targets["hidden"])  # Still one shared volume.
                    self.assertEqual(manager.group_targets["home"].channels["d"], (1, "c"))  # Still L/R in groups.
                finally:
                    await manager.close()

    async def test_early_inbound_player_joins_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = Manager(Config(config_path=f"{temp}/config.xml"))
            speaker = Speaker(id="echo-show", direction=INBOUND, exposed=False)
            manager.registry = Registry(speakers=[speaker], groups=[Group(id="salon", speaker_ids=[speaker.id])])

            async def connect_early(**_):
                await manager._ensure_target(speaker)

            server = Mock(start_server=AsyncMock(side_effect=connect_early), close=AsyncMock())
            server.add_event_listener.return_value = lambda: None
            web = Mock(start=AsyncMock(), close=AsyncMock(), url="http://localhost")
            with (
                patch("sendspin_bridge.app._load_identity"),
                patch("sendspin_bridge.app.FileServerPairingStore.open", new_callable=AsyncMock),
                patch("sendspin_bridge.app.lan_ipv4", return_value="127.0.0.1"),
                patch("sendspin_bridge.app.SendspinServer", return_value=server),
                patch("sendspin_bridge.app.ConfigWeb", return_value=web),
                patch.object(GroupTarget, "start", new_callable=AsyncMock),
            ):
                try:
                    await manager.start()
                    manager.group_targets["salon"].active = True
                    self.assertTrue(manager.targets[speaker.id].active)
                finally:
                    await manager.close()

    async def test_close_does_not_wait_for_a_replaced_inbound_connection(self) -> None:
        # A player reconnecting with its client_id leaves aiosendspin's old handler waiting; aiohttp's
        # default 60 s shutdown_timeout then stalled Save & restart for a minute.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        payload = ClientHelloPayload(name="Echo", supported_roles=["controller@v1"], client_id="echo")
        hello = ClientHelloMessage(payload=payload).to_json()
        url = f"http://127.0.0.1:{port}/sendspin"
        with tempfile.TemporaryDirectory() as temp:
            manager = Manager(Config(config_path=f"{temp}/config.xml", server_port=port))
            web = Mock(start=AsyncMock(), close=AsyncMock(), url="http://localhost")
            with (
                patch("sendspin_bridge.app.lan_ipv4", return_value="127.0.0.1"),
                patch("sendspin_bridge.app.ConfigWeb", return_value=web),
                patch("aiosendspin.server.server.AsyncZeroconf", return_value=AsyncMock()),  # Stay off the LAN.
                patch("aiosendspin.server.server.AsyncServiceBrowser", return_value=AsyncMock()),
            ):
                await manager.start()
                async with aiohttp.ClientSession() as session, asyncio.timeout(10):
                    stale = await session.ws_connect(url, autoping=False)
                    await stale.send_str(hello)
                    while (client := manager.server.get_client("echo")) is None or client.connection is None:
                        await asyncio.sleep(0.02)
                    old = client.connection
                    fresh = await session.ws_connect(url)
                    await fresh.send_str(hello)
                    while manager.server.get_client("echo").connection in (old, None):
                        await asyncio.sleep(0.02)
                    await manager.close()
                    await stale.close()
                    await fresh.close()


if __name__ == "__main__":
    unittest.main()
