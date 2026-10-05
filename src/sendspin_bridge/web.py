"""Small authenticated configuration UI for the bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from .registry import Endpoint, Group, Registry, Speaker, Stereo

LOG = logging.getLogger(__name__)
TOKEN_HEADER = "X-Config-Token"
# ponytail: internal-only UI; replace this before exposing it outside the trusted LAN.
CONFIG_TOKEN = "sendspin-bridge"


class ConfigWeb:
    """Serve the compiled UI and atomically save a replacement registry."""

    def __init__(
        self,
        *,
        config_path: str,
        port_base: int,
        port_range: int,
        host: str,
        port: int,
        advertised_host: str,
        registry: Callable[[], Registry],
        speaker_state: Callable[[str], dict],
        set_speaker_volume: Callable[[str, int], dict[str, int] | None],
        set_group_volume: Callable[[list[str], int, str], dict[str, int]],
        set_stereo_volume: Callable[[list[str], int, str], dict[str, int]],
        replace_registry: Callable[[Registry], None],
        restart: Callable[[], None],
    ) -> None:
        self._config_path = config_path
        self._port_base = port_base
        self._port_range = port_range
        self._host = host
        self._port = port
        self._advertised_host = advertised_host
        self._registry = registry
        self._speaker_state = speaker_state
        self._set_speaker_volume = set_speaker_volume
        self._set_group_volume = set_group_volume
        self._set_stereo_volume = set_stereo_volume
        self._replace_registry = replace_registry
        self._restart = restart
        self._token = CONFIG_TOKEN
        self._runner: web.AppRunner | None = None
        self._save_lock = asyncio.Lock()
        self._restart_scheduled = False

    @property
    def url(self) -> str:
        return f"http://{self._advertised_host}:{self._port}/"

    async def start(self) -> None:
        static_root = Path(__file__).with_name("web")
        if not (static_root / "index.html").is_file():
            raise RuntimeError("web UI assets are missing; rebuild the image")
        app = web.Application(middlewares=[self._authenticate])
        app.router.add_get("/", self._index)
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/api/config", self._get_config)
        app.router.add_put("/api/config", self._put_config)
        app.router.add_put("/api/speakers/{speaker_id}/volume", self._put_volume)
        app.router.add_put("/api/groups/{group_id}/volume", self._put_group_volume)
        app.router.add_put("/api/stereos/{stereo_id}/volume", self._put_stereo_volume)
        app.router.add_static("/", static_root, show_index=False)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, self._host, self._port).start()
        except BaseException:
            await self._runner.cleanup()
            self._runner = None
            raise

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _authenticate(self, request: web.Request, handler):
        if request.path.startswith("/api/"):
            supplied = request.headers.get(TOKEN_HEADER, "")
            if not secrets.compare_digest(supplied, self._token):
                return web.json_response({"error": "configuration token required"}, status=401)
        return await handler(request)

    async def _index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).with_name("web") / "index.html")

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _get_config(self, _request: web.Request) -> web.Response:
        return web.json_response(self._payload())

    async def _put_config(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            registry = registry_from_payload(payload, self._config_path, self._port_base, self._port_range)
            async with self._save_lock:
                registry.save()
                self._replace_registry(registry)
        except (ValueError, json.JSONDecodeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        if not self._restart_scheduled:
            self._restart_scheduled = True
            asyncio.get_running_loop().call_later(0.1, self._restart)
        return web.json_response({"config": self._payload(), "restarting": True})

    async def _put_volume(self, request: web.Request) -> web.Response:
        try:
            volume = _integer(_object(await request.json(), "volume").get("volume"), "volume", 0, 100)
        except (ValueError, json.JSONDecodeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        speaker_id = request.match_info["speaker_id"]
        updated = self._set_speaker_volume(speaker_id, volume)
        if updated is None:
            return web.json_response({"error": "speaker is not connected"}, status=409)
        # A stereo half moves its partner too, so return every level that changed.
        return web.json_response({"volume": updated[speaker_id], "speakers": updated})

    async def _put_group_volume(self, request: web.Request) -> web.Response:
        try:
            payload = _object(await request.json(), "request")
            volume = _integer(payload.get("volume"), "volume", 0, 100)
            members = payload.get("speaker_ids")
            if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
                raise ValueError("speaker_ids must be an array of speaker IDs")
        except (ValueError, json.JSONDecodeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        group = next((group for group in self._registry().groups() if group.id == request.match_info["group_id"]), None)
        if group is None:
            return web.json_response({"error": "group not found; save and restart first"}, status=404)
        if group.speaker_ids != members:
            return web.json_response({"error": "group members changed; save and restart first"}, status=409)
        updated = self._set_group_volume(group.speaker_ids, volume, group.id)
        if not updated:
            return web.json_response({"error": "no connected speakers in group"}, status=409)
        return web.json_response({"volume": round(sum(updated.values()) / len(updated)), "speakers": updated})

    async def _put_stereo_volume(self, request: web.Request) -> web.Response:
        try:
            payload = _object(await request.json(), "request")
            volume = _integer(payload.get("volume"), "volume", 0, 100)
            members = payload.get("speaker_ids")
            if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
                raise ValueError("speaker_ids must be an array of speaker IDs")
        except (ValueError, json.JSONDecodeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        stereo = next((item for item in self._registry().stereos() if item.id == request.match_info["stereo_id"]), None)
        if stereo is None:
            return web.json_response({"error": "stereo not found; save and restart first"}, status=404)
        if members != [stereo.left_id, stereo.right_id]:
            return web.json_response({"error": "stereo speakers changed; save and restart first"}, status=409)
        updated = self._set_stereo_volume(members, volume, stereo.id)
        if not updated:
            return web.json_response({"error": "no connected speakers in stereo"}, status=409)
        return web.json_response({"volume": round(sum(updated.values()) / len(updated)), "speakers": updated})

    def _payload(self) -> dict:
        payload = registry_to_payload(self._registry())
        for speaker in payload["speakers"]:
            speaker.update(self._speaker_state(speaker["id"]))
        return payload


def registry_to_payload(registry: Registry) -> dict:
    return {
        "exposed_suffix": registry.exposed_suffix,
        "speakers": [
            {
                "id": speaker.id,
                "exposed_name": speaker.exposed_name,
                "direction": speaker.direction,
                "port": speaker.port,
                "client_id": speaker.client_id,
                "exposed": speaker.exposed,
                "delay_ms": speaker.delay_ms,
                "endpoint": {
                    "instance": speaker.endpoint.instance,
                    "host": speaker.endpoint.host,
                    "port": speaker.endpoint.port,
                    "path": speaker.endpoint.path,
                },
            }
            for speaker in registry.speakers()
        ],
        "stereos": [
            {
                "id": stereo.id,
                "exposed_name": stereo.exposed_name,
                "port": stereo.port,
                "left_id": stereo.left_id,
                "right_id": stereo.right_id,
                "exposed": stereo.exposed,
            }
            for stereo in registry.stereos()
        ],
        "groups": [
            {
                "id": group.id,
                "exposed_name": group.exposed_name,
                "port": group.port,
                "speaker_ids": group.speaker_ids,
            }
            for group in registry.groups()
        ],
    }


def registry_from_payload(payload: object, path: str, port_base: int, port_range: int) -> Registry:
    if not isinstance(payload, dict):
        raise ValueError("configuration must be an object")
    speakers_data = _list(payload, "speakers")
    groups_data = _list(payload, "groups")
    stereos_data = _list(payload, "stereos")
    speakers = [_speaker(item, index) for index, item in enumerate(speakers_data, 1)]
    stereos = [_stereo(item, index) for index, item in enumerate(stereos_data, 1)]
    groups = [_group(item, index) for index, item in enumerate(groups_data, 1)]
    registry = Registry(
        path,
        port_base,
        port_range,
        exposed_suffix=_text(payload, "exposed_suffix", strip=False),
        speakers=speakers,
        groups=groups,
        stereos=stereos,
    )
    registry._normalize()
    return registry


def _speaker(value: object, index: int) -> Speaker:
    data = _object(value, f"speaker {index}")
    endpoint = _object(data.get("endpoint", {}), f"speaker {index} endpoint")
    delay = data.get("delay_ms")
    if delay is not None:
        delay = _integer(delay, f"speaker {index} delay_ms", -500, 500)
    return Speaker(
        id=_required_text(data, "id", f"speaker {index}"),
        exposed_name=_text(data, "exposed_name"),
        direction=_text(data, "direction", "outbound"),
        port=_integer(data.get("port", 0), f"speaker {index} port", 0, 65535),
        client_id=_text(data, "client_id"),
        exposed=_boolean(data.get("exposed", True), f"speaker {index} exposed"),
        delay_ms=delay,
        endpoint=Endpoint(
            instance=_text(endpoint, "instance"),
            host=_text(endpoint, "host"),
            port=_integer(endpoint.get("port", 0), f"speaker {index} endpoint port", 0, 65535),
            path=_text(endpoint, "path", "/sendspin"),
        ),
    )


def _stereo(value: object, index: int) -> Stereo:
    data = _object(value, f"stereo {index}")
    return Stereo(
        id=_required_text(data, "id", f"stereo {index}"),
        left_id=_required_text(data, "left_id", f"stereo {index}"),
        right_id=_required_text(data, "right_id", f"stereo {index}"),
        exposed_name=_text(data, "exposed_name"),
        port=_integer(data.get("port", 0), f"stereo {index} port", 0, 65535),
        exposed=_boolean(data.get("exposed", True), f"stereo {index} exposed"),
    )


def _group(value: object, index: int) -> Group:
    data = _object(value, f"group {index}")
    members = _list(data, "speaker_ids")
    if not all(isinstance(member, str) for member in members):
        raise ValueError(f"group {index} speaker_ids must contain strings")
    return Group(
        id=_required_text(data, "id", f"group {index}"),
        exposed_name=_text(data, "exposed_name"),
        port=_integer(data.get("port", 0), f"group {index} port", 0, 65535),
        speaker_ids=members,
    )


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def _text(data: dict, key: str, default: str = "", *, strip: bool = True) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() if strip else value


def _required_text(data: dict, key: str, label: str) -> str:
    value = _text(data, key)
    if not value:
        raise ValueError(f"{label} needs {key}")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value
