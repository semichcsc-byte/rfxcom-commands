"""Keep published documentation and translations aligned with the package."""

import json
import re
import struct
from pathlib import Path
from string import Formatter
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "rfxcom_commands"


def leaves(value, prefix=()):
    if isinstance(value, dict):
        return {
            path: text
            for key, child in value.items()
            for path, text in leaves(child, (*prefix, key)).items()
        }
    return {prefix: value}


def test_english_translation_matches_source():
    source = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations/en.json").read_text())
    assert english == source


def test_flow_titles_follow_current_translation_schema():
    source = json.loads((COMPONENT / "strings.json").read_text())
    assert "title" not in source["config"]
    for flow in source["config_subentries"].values():
        assert "title" not in flow
        assert flow["entry_type"]
        assert flow["initiate_flow"]["user"]


def test_portuguese_keys_and_placeholders_match_source():
    source = leaves(json.loads((COMPONENT / "strings.json").read_text()))
    translated = leaves(json.loads((COMPONENT / "translations/pt.json").read_text()))
    assert translated.keys() == source.keys()
    formatter = Formatter()
    for path, text in source.items():
        expected = {field for _, field, _, _ in formatter.parse(text) if field}
        actual = {field for _, field, _, _ in formatter.parse(translated[path]) if field}
        assert actual == expected, path


def test_documented_home_assistant_baseline_matches_hacs():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    baseline = ".".join(hacs["homeassistant"].split(".")[:2])
    assert f"Home Assistant **{baseline} or newer**" in (ROOT / "README.md").read_text()
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert list(manifest)[:2] == ["domain", "name"]
    assert list(manifest)[2:] == sorted(list(manifest)[2:])


@pytest.mark.parametrize("relative", ["README.md", "docs/PROTOCOL.md"])
def test_manual_links_and_headings(relative):
    document = ROOT / relative
    text = document.read_text()
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert len(headings) == len(set(headings))
    anchors = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)
    }
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.path:
            assert (document.parent / unquote(parsed.path)).exists(), target
        elif parsed.fragment:
            assert parsed.fragment in anchors, target


@pytest.mark.parametrize("name", ["commands.png", "learn-command.png", "scanner.png"])
def test_demo_screenshots_are_publishable_pngs(name):
    image = (ROOT / "docs" / "images" / name).read_bytes()
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert image[12:16] == b"IHDR"
    width, height = struct.unpack(">II", image[16:24])
    assert 500 <= width <= 2000
    assert 500 <= height <= 1600
    assert 10000 <= len(image) <= 1000000