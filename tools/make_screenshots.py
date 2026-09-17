"""Render the real frontend against an isolated HA test instance.

Install requirements_test.txt, home-assistant-frontend==20260826.4 and
playwright==1.55.0, then run `python -m playwright install chromium` and
`python -m pytest tools/make_screenshots.py -s` from the repository root.
No production connection or RF transport is used.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from playwright.async_api import async_playwright
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_config_flow import FakeListener, setup_integration, start_learning
from test_gateway import make_rfx
from rfx_capture import packets_from_log, raw_bursts
from custom_components.rfxcom_commands import config_flow
from custom_components.rfxcom_commands import scanner as scanner_module

pytest_plugins = "pytest_homeassistant_custom_component"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_documentation_screenshots(hass, hass_client, hass_access_token, monkeypatch):
    monkeypatch.setattr(
        "homeassistant.components.onboarding.async_is_onboarded", lambda hass: True
    )
    assert await async_setup_component(hass, "frontend", {})
    rfx_entry = MockConfigEntry(domain="rfxtrx", title="Demo receiver", data={}, options={})
    rfx_entry.add_to_hass(hass)
    rfx_entry.mock_state(hass, ConfigEntryState.LOADED)
    receiver = make_rfx(["arc", "x10"])
    receiver._status.device.type_string = "433.92MHz"
    hass.data["rfxtrx"] = {"rfxobject": receiver}
    bursts = raw_bursts(packets_from_log(ROOT / "tests" / "fan_remote_capture.txt"))
    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: FakeListener(hass, packets=bursts[1])
    )
    entry = await setup_integration(hass)
    learned = await start_learning(hass, entry)
    await hass.config_entries.subentries.async_configure(
        learned["flow_id"], {"name": "Fan OFF", "kind": "button", "test": False}
    )
    await hass.async_block_till_done()
    capture_ready = asyncio.Event()

    class DemoListener(FakeListener):
        async def next_packet(self, timeout):
            await capture_ready.wait()
            return await super().next_packet(timeout)

    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: DemoListener(hass, packets=bursts[0])
    )
    client = await hass_client()
    base_url = str(client.make_url("/"))
    output = ROOT / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1050}, device_scale_factor=1,
            locale="en-US", color_scheme="light",
        )
        tokens = {
            "hassUrl": base_url.rstrip("/"), "clientId": base_url,
            "access_token": hass_access_token, "token_type": "Bearer",
            "expires_in": 1800, "expires": int(time.time() * 1000) + 1800000,
            "refresh_token": "demo-only",
        }
        await context.add_init_script(
            "localStorage.setItem('hassTokens', " + json.dumps(json.dumps(tokens)) + ");"
            "localStorage.setItem('selectedLanguage', '\"en\"');"
        )

        async def local_only(route):
            if route.request.url.startswith(base_url):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", local_only)
        page = await context.new_page()
        try:
            await page.goto(
                base_url + "config/integrations/integration/rfxcom_commands",
                wait_until="domcontentloaded",
            )
            await page.get_by_text("RFXCOM Commands", exact=True).first.wait_for(timeout=60000)
            await page.get_by_text("Loading...", exact=True).wait_for(state="hidden")
            await page.get_by_role("button", name="Learn a command").click()
            await page.get_by_role("button", name="Submit", exact=True).click()
            await page.get_by_text("Press the button on your remote now.", exact=True).wait_for()
            capture_ready.set()
            await page.get_by_text("Name the command", exact=True).wait_for()
            await page.get_by_role("textbox", name="Name*", exact=True).fill("Fan ON")
            await page.get_by_role("dialog").screenshot(path=str(output / "learn-command.png"))
            await page.get_by_role("button", name="Submit", exact=True).click()
            await page.get_by_text("Fan ON", exact=True).first.wait_for()
            await page.get_by_role("button", name="Finish", exact=True).click()
            await page.get_by_role("dialog").wait_for(state="hidden")
            await page.locator("ha-panel-config").screenshot(path=str(output / "commands.png"))

            await hass.async_block_till_done()
            monkeypatch.setattr(scanner_module, "MAX_SCAN_SECONDS", 0.03)
            monkeypatch.setattr(
                scanner_module, "RawListener",
                lambda hass, band=None: FakeListener(hass, packets=bursts[0] + bursts[1], band=band),
            )
            await entry.runtime_data.scanner._run()
            await hass.async_block_till_done()
            device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
            await page.goto(base_url + "config/devices/device/" + device.id)
            await page.get_by_text("Signals heard", exact=True).wait_for()
            await page.get_by_text("Loading...", exact=True).wait_for(state="hidden")
            await page.locator("ha-panel-config").screenshot(path=str(output / "scanner.png"))
            print("Generated three real Home Assistant demo screenshots.")
        except Exception:
            await page.screenshot(path="/tmp/rfxcom-demo-error.png")
            print((await page.locator("body").inner_text())[-3500:])
            raise
        finally:
            await browser.close()