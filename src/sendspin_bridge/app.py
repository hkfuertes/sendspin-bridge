"""Async AirPlay targets backed by the official Sendspin server."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from aiosendspin.noise.keys import Identity, b64url_decode
from aiosendspin.noise.trust_store import FileServerPairingStore
from aiosendspin.server import (
    AudioFormat,
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientUpdatedEvent,
    SendspinServer,
    VolumeChangedEvent,
)

from .airplay import Advertiser, lan_ipv4, virtual_mac
from .audio import CHUNK_FRAMES, CHUNK_MS, CHUNK_SAMPLES, GroupBuffer, chunk, mixed_bytes
from .raop import FLUSH, PLAY, STOP, VOLUME, Receiver
from .registry import Endpoint, Group, INBOUND, OUTBOUND, Registry, Speaker, Stereo
from .web import ConfigWeb

LOG = logging.getLogger(__name__)
AIRPLAY_FORMAT = AudioFormat(sample_rate=44_100, bit_depth=16, channels=2)
# ponytail: fixed time for a player to report its volume after connecting or echo our command; raise for slow WiFi.
VOLUME_SETTLE_S = 2.0


@dataclass(frozen=True)
class Config:
    port_base: int = 7000
    port_range: int = 10
    config_path: str = "config.xml"
    server_port: int = 8927
    server_name: str = "Sendspin Bridge"
    web_host: str = "0.0.0.0"
    web_port: int = 8080


class AirPlayInput:
    """A libraop receiver plus its `_raop._tcp` advertisement."""

    def __init__(self, manager: Manager, key: str, name: str, port: int) -> None:
        self.manager = manager
        self.key = key
        self.name = name
        self.port_base = port
        self.receiver: Receiver | None = None
        self.advertiser: Advertiser | None = None
        self._next_volume: int | None = None
        self._volume_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        mac = virtual_mac(self.key)
        self.receiver = Receiver(
            self.name + self.manager.registry.exposed_suffix,
            mac,
            self.manager.address,
            self.port_base,
            self.manager.config.port_range,
        )
        try:
            self.advertiser = Advertiser(
                self.name + self.manager.registry.exposed_suffix,
                mac,
                self.manager.address,
                self.receiver.port,
            )
            await self.advertiser.start()
        except BaseException:
            if self.advertiser is not None:
                await self.advertiser.close()
                self.advertiser = None
            self.receiver.close()
            self.receiver = None
            raise

    def read_pcm(self, frames: int = CHUNK_FRAMES) -> bytes:
        return self.receiver.read_pcm(frames) if self.receiver is not None else b""

    def events(self):
        if self.receiver is None:
            return
        while event := self.receiver.read_event():
            yield event

    def send_volume(self, volume: int) -> None:
        if self.receiver is None:
            return
        self._next_volume = volume
        if self._volume_task is None or self._volume_task.done():
            self._volume_task = asyncio.create_task(self._send_volume(), name=f"airplay-volume-{self.key}")

    async def _send_volume(self) -> None:
        while self._next_volume is not None:
            volume, self._next_volume = self._next_volume, None
            try:
                # ponytail: DACP is best-effort; serialize/coalesce updates so slow remotes cannot stall playback.
                await asyncio.to_thread(self.receiver.notify_volume, volume / 100)
            except Exception:
                LOG.exception("AirPlay volume feedback failed for %s", self.name)

    async def close(self) -> None:
        if self.advertiser is not None:
            await self.advertiser.close()
            self.advertiser = None
        if self._volume_task is not None:
            await self._volume_task
            self._volume_task = None
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None


class GroupTarget:
    """One configured group AirPlay input, cached once for every member."""

    def __init__(self, manager: Manager, group: Group, stereos: list[Stereo] | None = None, *, key: str | None = None) -> None:
        self.manager = manager
        self.group = group
        self.input = AirPlayInput(manager, key or f"group:{group.id}", group.exposed_name, group.port)
        self.buffer = GroupBuffer(self.input.read_pcm)
        self.channels: dict[str, tuple[int, str]] = {}
        for stereo in stereos or []:
            if stereo.left_id in group.speaker_ids and stereo.right_id in group.speaker_ids:
                self.channels[stereo.left_id] = (0, stereo.right_id)
                self.channels[stereo.right_id] = (1, stereo.left_id)
        self.active = False

    async def start(self) -> None:
        await self.input.start()
        LOG.info("AirPlay target %r (%s) -> %d speakers", self.group.exposed_name, self.group.id, len(self.group.speaker_ids))

    def handle_events(self) -> None:
        for event, volume in self.input.events() or ():
            if event == PLAY:
                self.active = True
                self.buffer.reset()
            elif event == FLUSH:
                self.buffer.reset()
            elif event == STOP:
                self.active = False
                self.buffer.reset()
            elif event == VOLUME:
                self.manager.set_group_volume(self.group.speaker_ids, _volume_percent(volume))

    def mix_into(self, output: np.ndarray, playback_chunk: int, delay_ms: int, speaker_id: str) -> None:
        if self.active:
            route = self.channels.get(speaker_id)
            partner = self.manager.targets.get(route[1]) if route else None
            # ponytail: when one side disconnects, the surviving device gets full audio.
            channel = route[0] if partner is not None and partner.player is not None else None
            self.buffer.mix_into(output, playback_chunk, delay_ms, channel)

    async def close(self) -> None:
        await self.input.close()


class Target:
    """One player, its individual AirPlay input, and its Sendspin PushStream."""

    def __init__(self, manager: Manager, speaker: Speaker, groups: list[GroupTarget], *, paired: bool = False) -> None:
        self.manager = manager
        self.speaker = speaker
        self.groups = groups
        # Pair membership suspends only the individual AirPlay target; keep the stored preference for unpairing.
        self.input = AirPlayInput(manager, speaker.id, speaker.exposed_name, speaker.port) if speaker.exposed and not paired else None
        self.player = None
        self.stream = None
        self.playing = False
        self.delay_ms = speaker.delay_ms or 0
        self.volume = 100
        self.volume_ready_at = 0.0  # Until then the player may still report its connect-time volume.
        self.volume_quiet_until = 0.0  # Until then its reports are that handshake or echoes of our commands.
        self.remove_player_listener = None

    async def start(self) -> None:
        if self.input is not None:
            await self.input.start()
        LOG.info("AirPlay target %r (%s)", self.speaker.exposed_name, self.speaker.id)

    async def attach(self, player) -> None:
        if self.player is player:
            return
        self._remove_player_listener()
        self.player = player
        self.remove_player_listener = player.add_event_listener(self._on_player_event)
        self.manager.registry.set_client_id(self.speaker.id, player.client_id)
        self.delay_ms = self.manager.registry.delay(self.speaker.id)
        # Its connect-time client/state may already be applied (no event left to hear); later reports still update it.
        self.volume = player.roles_by_family("player")[0].volume
        self.volume_ready_at = self.volume_quiet_until = time.monotonic() + VOLUME_SETTLE_S
        asyncio.get_running_loop().call_later(VOLUME_SETTLE_S, self.manager.equalize_stereo, self.speaker.id)
        LOG.info("Sendspin player %r attached to %s", player.name, self.speaker.id)

    def detach(self, client_id: str) -> None:
        if self.player is None or self.player.client_id != client_id:
            return
        self._stop_stream()
        self._remove_player_listener()
        self.player = None

    def handle_events(self) -> None:
        if self.input is None:
            return
        for event, volume in self.input.events() or ():
            if event == PLAY:
                self.playing = True
            elif event == FLUSH:
                if self.stream is not None:
                    self.stream.clear()
            elif event == STOP:
                self.playing = False
                self._stop_stream()
            elif event == VOLUME:
                self.set_volume(_volume_percent(volume))

    @property
    def active(self) -> bool:
        return self.playing or any(group.active for group in self.groups)

    def render(self, playback_chunk: int) -> bytes:
        output = np.zeros(CHUNK_SAMPLES, dtype=np.int32)
        if self.playing and self.input is not None:
            output += chunk(self.input.read_pcm())
        for group in self.groups:
            group.mix_into(output, playback_chunk, self.delay_ms, self.speaker.id)
        return mixed_bytes(output)

    async def push(self, pcm: bytes, play_start_us: int) -> None:
        if self.player is None or not self.active:
            self._stop_stream()
            return
        if self.stream is None or self.stream.is_stopped:
            self.stream = self.player.group.start_stream()
            self.stream.set_live_source(True)
        try:
            self.stream.prepare_audio(pcm, AIRPLAY_FORMAT)
            await self.stream.commit_audio(play_start_us=play_start_us)
        except Exception:
            LOG.exception("Sendspin output for %s failed", self.speaker.id)
            self._stop_stream()

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, volume))
        if self.player is None:
            return
        role = self.player.group.group_role("player")
        if role is not None:
            self.volume_quiet_until = time.monotonic() + VOLUME_SETTLE_S
            role.set_volume(self.volume)

    async def close(self) -> None:
        self._stop_stream()
        self._remove_player_listener()
        if self.input is not None:
            await self.input.close()

    def _stop_stream(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream = None

    def _remove_player_listener(self) -> None:
        if self.remove_player_listener is not None:
            self.remove_player_listener()
            self.remove_player_listener = None

    def _on_player_event(self, _player, event) -> None:
        if isinstance(event, VolumeChangedEvent):
            self.volume = event.volume
            partner = self.manager.stereo_partner(self.speaker.id)
            # Outside the quiet window the device changed itself (e.g. its buttons): move its stereo partner along.
            if partner is not None and partner.volume != self.volume and time.monotonic() >= self.volume_quiet_until:
                partner.set_volume(self.volume)


class Manager:
    """Owns one Sendspin server, all RAOP inputs, and the shared 20 ms grid."""

    def __init__(self, config: Config) -> None:
        if config.port_range < 3:
            raise ValueError("AirPlay port range must contain at least three ports")
        if not 1 <= config.web_port <= 65535:
            raise ValueError("web port must be in 1..65535")
        self.config = config
        self.registry = Registry.load(config.config_path, config.port_base, config.port_range)
        self.address = ""
        self.server: SendspinServer | None = None
        self.web: ConfigWeb | None = None
        self.restart_requested = asyncio.Event()
        self.targets: dict[str, Target] = {}
        self.group_targets: dict[str, GroupTarget] = {}
        self.stereo_targets: dict[str, GroupTarget] = {}
        self.member_groups: dict[str, list[GroupTarget]] = {}
        self.remove_server_listener = None
        self.audio_task: asyncio.Task[None] | None = None
        self.playback_chunk = 0
        self.next_play_start_us: int | None = None

    async def start(self) -> None:
        self.address = lan_ipv4()
        state_dir = Path(self.config.config_path).parent
        identity = _load_identity(state_dir / ".sendspin-identity")
        pairing_store = await FileServerPairingStore.open(state_dir / ".sendspin-pairings.json")
        self.server = SendspinServer(
            asyncio.get_running_loop(),
            identity,
            self.config.server_name,
            pairing_store=pairing_store,
            allow_unencrypted=True,
        )
        self.remove_server_listener = self.server.add_event_listener(self._on_server_event)
        await self.server.start_server(
            port=self.config.server_port,
            advertise_addresses=[self.address],
            discover_clients=True,
        )
        # ponytail: aiosendspin leaves a reconnected player's old handler waiting and keeps aiohttp's 60 s
        # shutdown_timeout, so Save & restart stalled a minute. Private attributes (aiosendspin is pinned);
        # drop this once aiosendspin closes replaced connections or exposes the timeout.
        self.server._app_runner._shutdown_timeout = 1.0
        self.web = ConfigWeb(
            config_path=self.config.config_path,
            port_base=self.config.port_base,
            port_range=self.config.port_range,
            host=self.config.web_host,
            port=self.config.web_port,
            advertised_host=self.address,
            registry=lambda: self.registry,
            speaker_state=self.speaker_state,
            set_speaker_volume=self.set_speaker_volume,
            set_group_volume=self.set_group_volume,
            set_stereo_volume=self.set_stereo_volume,
            replace_registry=self.replace_registry,
            restart=self.request_restart,
        )
        await self.web.start()
        LOG.info("Configuration UI: %s", self.web.url)
        stereos = self.registry.stereos()
        for stereo in stereos:
            group = Group(stereo.id, stereo.exposed_name, stereo.port, [stereo.left_id, stereo.right_id])
            target = GroupTarget(self, group, [stereo], key=f"stereo:{stereo.id}")
            await target.start()
            self.stereo_targets[stereo.id] = target
            for speaker_id in group.speaker_ids:
                self.member_groups.setdefault(speaker_id, []).append(target)
        for group in self.registry.groups():
            target = GroupTarget(self, group, stereos)
            await target.start()
            self.group_targets[group.id] = target
            for speaker_id in group.speaker_ids:
                self.member_groups.setdefault(speaker_id, []).append(target)
        for speaker in self.registry.speakers():
            if speaker.direction == INBOUND or (speaker.endpoint.host and speaker.endpoint.port):
                await self._ensure_target(speaker)
            if speaker.direction == OUTBOUND and speaker.endpoint.host and speaker.endpoint.port:
                self.server.connect_to_client(
                    speaker.endpoint.url,
                    retry_initial_connection=True,
                    retry_indefinitely=True,
                )
        self.audio_task = asyncio.create_task(self._pump_audio(), name="sendspin-bridge-audio")
        LOG.info("Sendspin server listening on %s:%d", self.address, self.config.server_port)

    async def close(self) -> None:
        if self.web is not None:
            await self.web.close()
            self.web = None
        if self.audio_task is not None:
            self.audio_task.cancel()
            await asyncio.gather(self.audio_task, return_exceptions=True)
            self.audio_task = None
        if self.remove_server_listener is not None:
            self.remove_server_listener()
            self.remove_server_listener = None
        for target in list(self.targets.values()):
            await target.close()
        self.targets.clear()
        for target in (*self.group_targets.values(), *self.stereo_targets.values()):
            await target.close()
        self.group_targets.clear()
        self.stereo_targets.clear()
        if self.server is not None:
            await self.server.close()
            self.server = None

    def replace_registry(self, registry: Registry) -> None:
        self.registry = registry

    def speaker_state(self, speaker_id: str) -> dict:
        target = self.targets.get(speaker_id)
        return {"connected": target is not None and target.player is not None, "volume": target.volume if target is not None else 100}

    def set_speaker_volume(self, speaker_id: str, volume: int) -> dict[str, int] | None:
        target = self.targets.get(speaker_id)
        if target is None or target.player is None:
            return None
        stereo = self._stereo(speaker_id)
        if stereo is not None:  # Stereo halves share one volume.
            return self.set_stereo_volume(stereo.group.speaker_ids, volume, stereo.group.id)
        target.set_volume(volume)
        if target.playing and target.input is not None:
            target.input.send_volume(target.volume)
        return {speaker_id: target.volume}

    def stereo_partner(self, speaker_id: str) -> Target | None:
        """The connected other half of speaker_id's running stereo pair."""
        stereo = self._stereo(speaker_id)
        partner = self.targets.get(stereo.channels[speaker_id][1]) if stereo is not None else None
        return partner if partner is not None and partner.player is not None else None

    def equalize_stereo(self, speaker_id: str) -> None:
        """Once both halves have settled after connecting, both take the lower volume."""
        target, partner = self.targets.get(speaker_id), self.stereo_partner(speaker_id)
        if target is None or target.player is None or partner is None or partner.volume_ready_at > time.monotonic():
            return  # The later half's own timer equalizes the pair.
        volume = min(target.volume, partner.volume)
        for member in (target, partner):
            if member.volume != volume:
                member.set_volume(volume)

    def _stereo(self, speaker_id: str) -> GroupTarget | None:
        return next((stereo for stereo in self.stereo_targets.values() if speaker_id in stereo.channels), None)

    def request_restart(self) -> None:
        LOG.info("Restarting bridge to apply configuration changes")
        self.restart_requested.set()

    def set_group_volume(self, speaker_ids: list[str], volume: int, group_id: str | None = None) -> dict[str, int]:
        targets = [(speaker_id, self.targets[speaker_id]) for speaker_id in speaker_ids if speaker_id in self.targets and self.targets[speaker_id].player]
        levels = [float(target.volume) for _, target in targets]
        _spread_volume(levels, float(volume))
        index = {speaker_id: position for position, (speaker_id, _) in enumerate(targets)}
        for stereo in self.stereo_targets.values():
            halves = [index[speaker_id] for speaker_id in stereo.channels if speaker_id in index]
            if len(halves) == 2:  # Stereo halves share one volume; their average keeps the group's.
                levels[halves[0]] = levels[halves[1]] = (levels[halves[0]] + levels[halves[1]]) / 2
        for (_, target), level in zip(targets, levels, strict=True):
            target.set_volume(round(level))
        updated = {speaker_id: target.volume for speaker_id, target in targets}
        group = self.group_targets.get(group_id) if group_id is not None else None
        if group is not None and group.active and updated:
            group.input.send_volume(round(sum(updated.values()) / len(updated)))
        return updated

    def set_stereo_volume(self, speaker_ids: list[str], volume: int, stereo_id: str) -> dict[str, int]:
        updated = self.set_group_volume(speaker_ids, volume)
        stereo = self.stereo_targets.get(stereo_id)
        if stereo is not None and stereo.active and updated:
            stereo.input.send_volume(round(sum(updated.values()) / len(updated)))
        return updated

    def _on_server_event(self, _server: SendspinServer, event) -> None:
        if isinstance(event, (ClientAddedEvent, ClientConnectedEvent, ClientUpdatedEvent)):
            asyncio.create_task(self._attach_client(event.client_id))
        elif isinstance(event, ClientDisconnectedEvent):
            target = next((target for target in self.targets.values() if target.player and target.player.client_id == event.client_id), None)
            if target is not None:
                target.detach(event.client_id)

    async def _attach_client(self, client_id: str) -> None:
        if self.server is None:
            return
        player = self.server.get_client(client_id)
        if player is None or not player.roles_by_family("player"):
            return
        speaker = self.registry.speaker_for_client(client_id)
        if speaker is None:
            endpoint = self._outbound_endpoint(client_id)
            if endpoint is not None:
                speaker, added = self.registry.upsert_outbound(player.name, endpoint)
                if added:
                    LOG.info("Recorded discovered Sendspin player %r as %r", player.name, speaker.id)
            else:
                speaker, added = self.registry.upsert_inbound(client_id, player.name)
                if added:
                    LOG.info("Recorded inbound Sendspin player %r as %r", player.name, speaker.id)
        target = await self._ensure_target(speaker)
        await target.attach(player)

    async def _ensure_target(self, speaker: Speaker) -> Target:
        target = self.targets.get(speaker.id)
        if target is not None:
            return target
        # Keep the list shared: inbound players can connect before groups finish starting.
        paired = any(speaker.id in (pair.left_id, pair.right_id) for pair in self.registry.stereos())
        target = Target(self, speaker, self.member_groups.setdefault(speaker.id, []), paired=paired)
        self.targets[speaker.id] = target
        try:
            await target.start()
        except BaseException:
            self.targets.pop(speaker.id, None)
            await target.close()
            raise
        return target

    async def _pump_audio(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        try:
            while True:
                for target in (*self.stereo_targets.values(), *self.group_targets.values()):
                    target.handle_events()
                for target in self.targets.values():
                    target.handle_events()
                active = [target for target in self.targets.values() if target.player is not None and target.active]
                if not active:
                    self.next_play_start_us = None
                    next_tick = loop.time()
                    await asyncio.sleep(0.01)
                    continue
                assert self.server is not None
                now = self.server.clock.now_us()
                if self.next_play_start_us is None or self.next_play_start_us < now + 250_000:
                    self.next_play_start_us = now + 500_000
                play_start_us = self.next_play_start_us
                self.next_play_start_us += CHUNK_MS * 1_000
                payloads = [(target, target.render(self.playback_chunk)) for target in active]
                await asyncio.gather(*(target.push(pcm, play_start_us) for target, pcm in payloads))
                self.playback_chunk += 1
                next_tick += CHUNK_MS / 1_000
                await asyncio.sleep(max(0, next_tick - loop.time()))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("audio scheduler stopped")
            raise

    def _outbound_endpoint(self, client_id: str) -> Endpoint | None:
        if self.server is None:
            return None
        # ponytail: aiosendspin has no public outbound-peer URL; use its map until it exposes one.
        url = getattr(self.server, "_client_urls", {}).get(client_id)
        if not url:
            return None
        parts = urlsplit(url)
        return Endpoint(host=parts.hostname or "", port=parts.port or 0, path=parts.path).normalized()


def _volume_percent(volume: float) -> int:
    if math.isnan(volume) or volume <= 0:
        return 0
    return 100 if volume >= 1 else int(volume * 100 + 0.5)


def _spread_volume(levels: list[float], target: float) -> None:
    if not levels:
        return
    delta = target - sum(levels) / len(levels)
    active = list(range(len(levels)))
    while active:
        lost = 0.0
        next_active: list[int] = []
        for index in active:
            level = levels[index] + delta
            if level > 100:
                lost += level - 100
                levels[index] = 100
            elif level < 0:
                lost += level
                levels[index] = 0
            else:
                levels[index] = level
                next_active.append(index)
        if len(next_active) == len(active) or not next_active:
            return
        delta = lost / len(next_active)
        active = next_active


def _load_identity(path: Path) -> Identity:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        identity = Identity.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(identity.private_b64u + "\n", encoding="ascii")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        return identity
    return Identity.from_private_bytes(b64url_decode(raw))
