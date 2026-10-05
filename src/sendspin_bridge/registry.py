"""Persistent, hand-editable Sendspin Bridge registry."""

from __future__ import annotations

import copy
import os
import tempfile
import threading
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.xml"
DEFAULT_EXPOSED_SUFFIX = " (Sendspin)"
INBOUND = "inbound"
OUTBOUND = "outbound"
MAX_DELAY_MS = 500


@dataclass
class Endpoint:
    instance: str = ""
    host: str = ""
    port: int = 0
    path: str = "/sendspin"

    def normalized(self) -> Endpoint:
        return Endpoint(self.instance, self.host, self.port, normalize_path(self.path))

    @property
    def url(self) -> str:
        if not self.host or not self.port:
            raise ValueError("Sendspin endpoint host and port are required")
        return f"ws://{self.host}:{self.port}{normalize_path(self.path)}"


@dataclass
class Speaker:
    id: str
    exposed_name: str = ""
    direction: str = OUTBOUND
    port: int = 0
    client_id: str = ""
    exposed: bool = True
    delay_ms: int | None = None
    endpoint: Endpoint = field(default_factory=Endpoint)


@dataclass
class Group:
    id: str
    exposed_name: str = ""
    port: int = 0
    speaker_ids: list[str] = field(default_factory=list)


@dataclass
class Stereo:
    id: str
    left_id: str
    right_id: str
    exposed_name: str = ""
    port: int = 0
    exposed: bool = True


class Registry:
    """Validates and atomically writes config.xml under one process lock."""

    def __init__(
        self,
        path: str | Path = DEFAULT_CONFIG_PATH,
        port_base: int = 7000,
        port_range: int = 10,
        *,
        exposed_suffix: str = DEFAULT_EXPOSED_SUFFIX,
        speakers: list[Speaker] | None = None,
        groups: list[Group] | None = None,
        stereos: list[Stereo] | None = None,
    ) -> None:
        if not 0 < port_base <= 65535 or port_range < 3:
            raise ValueError("AirPlay port base and range are invalid")
        self.path = Path(path or DEFAULT_CONFIG_PATH)
        self.port_base = port_base
        self.port_range = port_range
        self.exposed_suffix = exposed_suffix
        self._speakers = speakers or []
        self._groups = groups or []
        self._stereos = stereos or []
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: str | Path, port_base: int = 7000, port_range: int = 10) -> Registry:
        path = Path(path or DEFAULT_CONFIG_PATH)
        if not path.exists():
            return cls(path, port_base, port_range)
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            raise ValueError(f"parse config {path}: {error}") from error
        if root.tag != "sendspin-bridge":
            raise ValueError(f"config {path} has root <{root.tag}>, want <sendspin-bridge>")

        speakers: list[Speaker] = []
        for node in root.findall("./speakers/speaker"):
            endpoint = node.find("endpoint")
            speakers.append(
                Speaker(
                    id=node.get("id", ""),
                    client_id=node.get("client_id", ""),
                    exposed_name=node.get("exposed_name", ""),
                    direction=node.get("direction", OUTBOUND),
                    port=_integer(node.get("port"), "speaker port"),
                    exposed=_boolean(node.get("exposed", "true"), "exposed"),
                    delay_ms=_optional_delay(node.get("delay_ms")),
                    endpoint=Endpoint(
                        instance=endpoint.get("instance", "") if endpoint is not None else "",
                        host=endpoint.get("host", "") if endpoint is not None else "",
                        port=_integer(endpoint.get("port") if endpoint is not None else None, "endpoint port"),
                        path=endpoint.get("path", "") if endpoint is not None else "",
                    ),
                )
            )
        stereos = [
            Stereo(
                id=node.get("id", ""),
                left_id=node.get("left_id", ""),
                right_id=node.get("right_id", ""),
                exposed_name=node.get("exposed_name", ""),
                port=_integer(node.get("port"), "stereo port"),
                exposed=_boolean(node.get("exposed", "true"), "stereo exposed"),
            )
            for node in root.findall("./stereos/stereo")
        ]
        groups: list[Group] = []
        for node in root.findall("./groups/group"):
            groups.append(
                Group(
                    id=node.get("id", ""),
                    exposed_name=node.get("exposed_name", ""),
                    port=_integer(node.get("port"), "group port"),
                    speaker_ids=[member.get("id", "") for member in node.findall("speaker")],
                )
            )
        registry = cls(
            path,
            port_base,
            port_range,
            exposed_suffix=root.get("exposed_suffix", DEFAULT_EXPOSED_SUFFIX),
            speakers=speakers,
            groups=groups,
            stereos=stereos,
        )
        registry._normalize()
        registry.save()
        return registry

    def speakers(self) -> list[Speaker]:
        with self._lock:
            return copy.deepcopy(self._speakers)

    def groups(self) -> list[Group]:
        with self._lock:
            return copy.deepcopy(self._groups)

    def stereos(self) -> list[Stereo]:
        with self._lock:
            return copy.deepcopy(self._stereos)

    def speaker(self, speaker_id: str) -> Speaker | None:
        with self._lock:
            return copy.deepcopy(next((s for s in self._speakers if s.id == speaker_id), None))

    def speaker_for_client(self, client_id: str) -> Speaker | None:
        with self._lock:
            return copy.deepcopy(next((s for s in self._speakers if s.client_id == client_id), None))

    def delay(self, speaker_id: str) -> int:
        with self._lock:
            speaker = self._require_speaker(speaker_id)
            if speaker.delay_ms is None:
                speaker.delay_ms = 0
                self.save()
            return speaker.delay_ms

    def set_client_id(self, speaker_id: str, client_id: str) -> None:
        if not client_id:
            raise ValueError("Sendspin client_id is required")
        with self._lock:
            owner = self.speaker_for_client(client_id)
            if owner is not None and owner.id != speaker_id:
                raise ValueError(f"Sendspin client_id {client_id!r} already belongs to {owner.id}")
            speaker = self._require_speaker(speaker_id)
            if speaker.client_id != client_id:
                speaker.client_id = client_id
                self.save()

    def upsert_outbound(self, name: str, endpoint: Endpoint) -> tuple[Speaker, bool]:
        endpoint = endpoint.normalized()
        if not name or not endpoint.host or endpoint.port < 1:
            raise ValueError("invalid Sendspin player discovery")
        with self._lock:
            for speaker in self._speakers:
                if not same_endpoint(speaker.endpoint, endpoint):
                    continue
                if speaker.direction != OUTBOUND:
                    return copy.deepcopy(speaker), False
                changed = speaker.endpoint != endpoint or not speaker.exposed_name
                speaker.endpoint = endpoint
                if not speaker.exposed_name:
                    speaker.exposed_name = name
                if changed:
                    self.save()
                return copy.deepcopy(speaker), False
            speaker = Speaker(
                id=self._next_id(name),
                exposed_name=name,
                direction=OUTBOUND,
                port=self._next_port(),
                endpoint=endpoint,
            )
            self._speakers.append(speaker)
            self.save()
            return copy.deepcopy(speaker), True

    def upsert_inbound(self, client_id: str, name: str) -> tuple[Speaker, bool]:
        if not client_id:
            raise ValueError("Sendspin client_id is required")
        with self._lock:
            existing = self.speaker_for_client(client_id)
            if existing is not None:
                if existing.direction != INBOUND:
                    raise ValueError(f"speaker {existing.id!r} is configured for outbound Sendspin")
                return existing, False
            speaker = Speaker(
                id=self._next_id(name or client_id),
                client_id=client_id,
                exposed_name=name or client_id,
                direction=INBOUND,
                port=self._next_port(),
            )
            self._speakers.append(speaker)
            self.save()
            return copy.deepcopy(speaker), True

    def save(self) -> None:
        with self._lock:
            self._normalize()
            root = ET.Element("sendspin-bridge", {"version": "1", "exposed_suffix": self.exposed_suffix})
            speakers = ET.SubElement(root, "speakers")
            for speaker in self._speakers:
                attrs = {
                    "id": speaker.id,
                    "exposed_name": speaker.exposed_name,
                    "direction": speaker.direction,
                    "port": str(speaker.port),
                    "exposed": str(speaker.exposed).lower(),
                }
                if speaker.client_id:
                    attrs["client_id"] = speaker.client_id
                if speaker.delay_ms is not None:
                    attrs["delay_ms"] = str(speaker.delay_ms)
                node = ET.SubElement(speakers, "speaker", attrs)
                endpoint = speaker.endpoint.normalized()
                endpoint_attrs = {"path": endpoint.path}
                if endpoint.instance:
                    endpoint_attrs["instance"] = endpoint.instance
                if endpoint.host:
                    endpoint_attrs["host"] = endpoint.host
                if endpoint.port:
                    endpoint_attrs["port"] = str(endpoint.port)
                ET.SubElement(node, "endpoint", endpoint_attrs)
            if self._stereos:
                stereos = ET.SubElement(root, "stereos")
                for stereo in self._stereos:
                    ET.SubElement(stereos, "stereo", {
                        "id": stereo.id, "exposed_name": stereo.exposed_name,
                        "left_id": stereo.left_id, "right_id": stereo.right_id,
                        "port": str(stereo.port), "exposed": str(stereo.exposed).lower(),
                    })
            groups = ET.SubElement(root, "groups")
            for group in self._groups:
                node = ET.SubElement(
                    groups,
                    "group",
                    {"id": group.id, "exposed_name": group.exposed_name, "port": str(group.port)},
                )
                for speaker_id in group.speaker_ids:
                    ET.SubElement(node, "speaker", {"id": speaker_id})
            ET.indent(root, space="  ")
            payload = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".config.xml-", delete=False) as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
                name = tmp.name
            os.chmod(name, 0o644)
            os.replace(name, self.path)

    def _normalize(self) -> None:
        ids: set[str] = set()
        client_ids: set[str] = set()
        for speaker in self._speakers:
            if not speaker.id or speaker.id in ids:
                raise ValueError("config has missing or duplicate speaker id")
            ids.add(speaker.id)
            if speaker.client_id:
                if speaker.client_id in client_ids:
                    raise ValueError(f"config has duplicate Sendspin client_id {speaker.client_id!r}")
                client_ids.add(speaker.client_id)
            if speaker.direction not in (INBOUND, OUTBOUND):
                raise ValueError(f"speaker {speaker.id!r} has invalid direction {speaker.direction!r}")
            speaker.exposed_name = speaker.exposed_name or speaker.id
            speaker.port = speaker.port or self._next_port()
            if not -MAX_DELAY_MS <= (speaker.delay_ms or 0) <= MAX_DELAY_MS:
                raise ValueError(f"speaker {speaker.id!r} has delay_ms {speaker.delay_ms}, want -500..500")
            speaker.endpoint = speaker.endpoint.normalized()

        paired: set[str] = set()
        stereo_ids: set[str] = set()
        for stereo in self._stereos:
            if not stereo.id or stereo.id in stereo_ids:
                raise ValueError("config has missing or duplicate stereo id")
            stereo_ids.add(stereo.id)
            if (stereo.left_id not in ids or stereo.right_id not in ids
                    or stereo.left_id == stereo.right_id):
                raise ValueError(f"stereo {stereo.id!r} needs two distinct existing speakers")
            if stereo.left_id in paired or stereo.right_id in paired:
                raise ValueError(f"stereo {stereo.id!r} reuses a paired speaker")
            paired.update((stereo.left_id, stereo.right_id))
            stereo.exposed_name = stereo.exposed_name or stereo.id
            stereo.port = stereo.port or self._next_port()

        group_ids: set[str] = set()
        for group in self._groups:
            if not group.id or group.id in group_ids:
                raise ValueError("config has missing or duplicate group id")
            group_ids.add(group.id)
            group.exposed_name = group.exposed_name or group.id
            group.port = group.port or self._next_port()
            seen_members: set[str] = set()
            for speaker_id in group.speaker_ids:
                if speaker_id not in ids or speaker_id in seen_members:
                    raise ValueError(f"group {group.id!r} has unknown or duplicate speaker {speaker_id!r}")
                seen_members.add(speaker_id)
            for stereo in self._stereos:
                if (stereo.left_id in seen_members) != (stereo.right_id in seen_members):
                    raise ValueError(f"group {group.id!r} must include both speakers of stereo {stereo.id!r}")

    def _next_port(self) -> int:
        used = ({speaker.port for speaker in self._speakers}
                | {group.port for group in self._groups}
                | {stereo.port for stereo in self._stereos})
        for port in range(self.port_base, 65536 - self.port_range + 1, self.port_range):
            if port not in used:
                return port
        raise ValueError("no AirPlay port range left")

    def _next_id(self, name: str) -> str:
        used = {speaker.id for speaker in self._speakers}
        base = speaker_id(name) or "speaker"
        if base not in used:
            return base
        number = 2
        while f"{base}-{number}" in used:
            number += 1
        return f"{base}-{number}"

    def _require_speaker(self, speaker_id: str) -> Speaker:
        for speaker in self._speakers:
            if speaker.id == speaker_id:
                return speaker
        raise ValueError(f"unknown speaker {speaker_id!r}")


def normalize_path(path: str) -> str:
    return "/sendspin" if not path else path if path.startswith("/") else f"/{path}"


def same_endpoint(left: Endpoint, right: Endpoint) -> bool:
    left, right = left.normalized(), right.normalized()
    if left.instance and right.instance:
        return left.instance == right.instance
    return (left.host, left.port, left.path) == (right.host, right.port, right.path)


def speaker_id(name: str) -> str:
    words: list[str] = []
    dash = True
    for char in unicodedata.normalize("NFKC", name).casefold():
        if char.isalnum():
            words.append(char)
            dash = False
        elif not dash:
            words.append("-")
            dash = True
    return "".join(words).strip("-")


def _integer(value: str | None, name: str) -> int:
    if not value:
        return 0
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"invalid {name} {value!r}") from error
    if result < 0 or result > 65535:
        raise ValueError(f"invalid {name} {value!r}")
    return result


def _optional_delay(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"invalid delay_ms {value!r}") from error


def _boolean(value: str, name: str) -> bool:
    if value.lower() in ("1", "true", "yes"):
        return True
    if value.lower() in ("0", "false", "no", ""):
        return False
    raise ValueError(f"invalid {name} {value!r}")
