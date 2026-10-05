from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sendspin_bridge.registry import Endpoint, Group, Registry, Speaker, Stereo


class RegistryTests(unittest.TestCase):
    def test_outbound_is_persisted_once_and_gets_a_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            registry = Registry.load(path)
            speaker, added = registry.upsert_outbound(
                "Kitchen", Endpoint(instance="kitchen._sendspin._tcp.local.", host="192.0.2.10", port=8928)
            )
            self.assertTrue(added)
            self.assertEqual((speaker.id, speaker.port, speaker.endpoint.path), ("kitchen", 7000, "/sendspin"))
            again, added = registry.upsert_outbound(
                "Renamed", Endpoint(instance="kitchen._sendspin._tcp.local.", host="192.0.2.11", port=8928)
            )
            self.assertFalse(added)
            self.assertEqual(again.id, "kitchen")
            self.assertEqual(again.endpoint.host, "192.0.2.11")
            self.assertIn('exposed_suffix=" (Sendspin)"', path.read_text())

    def test_signed_delay_is_inclusive(self) -> None:
        template = """<sendspin-bridge version=\"1\"><speakers><speaker id=\"kitchen\" direction=\"inbound\" delay_ms=\"{delay}\"><endpoint/></speaker></speakers><groups/></sendspin-bridge>"""
        for delay in (-500, 500):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.xml"
                path.write_text(template.format(delay=delay))
                self.assertEqual(Registry.load(path).speaker("kitchen").delay_ms, delay)
        for delay in (-501, 501):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.xml"
                path.write_text(template.format(delay=delay))
                with self.assertRaisesRegex(ValueError, "delay_ms"):
                    Registry.load(path)

    def test_exposure_is_persisted_and_defaults_to_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            path.write_text('<sendspin-bridge exposed_suffix=" (Bridge)"><speakers>'
                            '<speaker id="kitchen" exposed_name="Kitchen" exposed="false"><endpoint/></speaker>'
                            '<speaker id="bedroom"><endpoint/></speaker>'
                            '</speakers><groups><group id="home" exposed_name="Home">'
                            '<speaker id="kitchen"/></group></groups></sendspin-bridge>')
            registry = Registry.load(path)
            self.assertEqual(registry.exposed_suffix, " (Bridge)")
            self.assertEqual((registry.speaker("kitchen").exposed_name, registry.speaker("kitchen").exposed), ("Kitchen", False))
            self.assertTrue(registry.speaker("bedroom").exposed)
            self.assertEqual(registry.groups()[0].exposed_name, "Home")
            xml = path.read_text()
            self.assertIn('exposed="false"', xml)
            self.assertNotIn("airplay", xml.lower())

    def test_stereo_round_trip_preserves_existing_speakers_and_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            path.write_text('<sendspin-bridge version="1"><speakers>'
                            '<speaker id="left"><endpoint/></speaker>'
                            '<speaker id="right"><endpoint/></speaker>'
                            '<speaker id="kitchen"><endpoint/></speaker>'
                            '</speakers><groups><group id="home">'
                            '<speaker id="left"/><speaker id="right"/><speaker id="kitchen"/>'
                            '</group></groups></sendspin-bridge>')
            registry = Registry.load(path)
            self.assertEqual(registry.stereos(), [])
            Registry(path, speakers=registry.speakers(), groups=registry.groups(),
                     stereos=[Stereo("pair", "left", "right", "Living room", exposed=False)]).save()
            restored = Registry.load(path)
            self.assertFalse(restored.stereos()[0].exposed)
            path.write_text(path.read_text().replace(' exposed="false"', ""))  # Pairs saved before the flag.
            self.assertTrue(Registry.load(path).stereos()[0].exposed)
            self.assertEqual(restored.stereos()[0].port, 7040)
            self.assertEqual(restored.stereos()[0].exposed_name, "Living room")
            self.assertEqual(restored.groups()[0].speaker_ids, ["left", "right", "kitchen"])
            self.assertIn('<stereos>', path.read_text())

    def test_stereo_rejects_reused_speakers_and_half_group_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.xml"
            speakers = [Speaker(id=id_) for id_ in ("left", "right", "kitchen")]
            for stereos, groups, error in (
                ([Stereo("one", "left", "left")], [], "distinct"),
                ([Stereo("one", "left", "missing")], [], "existing"),
                ([Stereo("one", "left", "right"), Stereo("two", "left", "kitchen")], [], "reuses"),
                ([Stereo("one", "left", "right")], [Group("home", speaker_ids=["left"])], "both speakers"),
            ):
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                    Registry(path, speakers=speakers, stereos=stereos, groups=groups).save()

    def test_inbound_client_cannot_claim_an_outbound_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry.load(Path(directory) / "config.xml")
            speaker, _ = registry.upsert_outbound("Kitchen", Endpoint(host="192.0.2.10", port=8928))
            registry.set_client_id(speaker.id, "client-a")
            with self.assertRaisesRegex(ValueError, "outbound"):
                registry.upsert_inbound("client-a", "Kitchen")


if __name__ == "__main__":
    unittest.main()
