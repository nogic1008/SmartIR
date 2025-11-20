import aiofiles
import aiohttp
import asyncio
import binascii
from distutils.version import StrictVersion
import json
import logging
import os.path
import requests
import struct
import voluptuous as vol

from aiohttp import ClientSession
from homeassistant.const import (
    ATTR_FRIENDLY_NAME, __version__ as current_ha_version)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'smartir'
VERSION = '1.18.1'
MANIFEST_URL = (
    "https://raw.githubusercontent.com/"
    "smartHomeHub/SmartIR/{}/"
    "custom_components/smartir/manifest.json")
REMOTE_BASE_URL = (
    "https://raw.githubusercontent.com/"
    "smartHomeHub/SmartIR/{}/"
    "custom_components/smartir/")
COMPONENT_ABS_DIR = os.path.dirname(
    os.path.abspath(__file__))

CONF_CHECK_UPDATES = 'check_updates'
CONF_UPDATE_BRANCH = 'update_branch'

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_CHECK_UPDATES, default=True): cv.boolean,
        vol.Optional(CONF_UPDATE_BRANCH, default='master'): vol.In(
            ['master', 'rc'])
    })
}, extra=vol.ALLOW_EXTRA)

async def async_setup(hass, config):
    """Set up the SmartIR component."""
    conf = config.get(DOMAIN)

    if conf is None:
        return True

    check_updates = conf[CONF_CHECK_UPDATES]
    update_branch = conf[CONF_UPDATE_BRANCH]

    async def _check_updates(service):
        await _update(hass, update_branch)

    async def _update_component(service):
        await _update(hass, update_branch, True)

    hass.services.async_register(DOMAIN, 'check_updates', _check_updates)
    hass.services.async_register(DOMAIN, 'update_component', _update_component)

    if check_updates:
        await _update(hass, update_branch, False, False)

    return True

async def _update(hass, branch, do_update=False, notify_if_latest=True):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(MANIFEST_URL.format(branch)) as response:
                if response.status == 200:
                    
                    data = await response.json(content_type='text/plain')
                    min_ha_version = data['homeassistant']
                    last_version = data['updater']['version']
                    release_notes = data['updater']['releaseNotes']

                    if StrictVersion(last_version) <= StrictVersion(VERSION):
                        if notify_if_latest:
                            hass.components.persistent_notification.async_create(
                                "You're already using the latest version!", 
                                title='SmartIR')
                        return

                    if StrictVersion(current_ha_version) < StrictVersion(min_ha_version):
                        hass.components.persistent_notification.async_create(
                            "There is a new version of SmartIR integration, but it is **incompatible** "
                            "with your system. Please first update Home Assistant.", title='SmartIR')
                        return

                    if do_update is False:
                        hass.components.persistent_notification.async_create(
                            "A new version of SmartIR integration is available ({}). "
                            "Call the ``smartir.update_component`` service to update "
                            "the integration. \n\n **Release notes:** \n{}"
                            .format(last_version, release_notes), title='SmartIR')
                        return

                    # Begin update
                    files = data['updater']['files']
                    has_errors = False

                    for file in files:
                        try:
                            source = REMOTE_BASE_URL.format(branch) + file
                            dest = os.path.join(COMPONENT_ABS_DIR, file)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            await Helper.downloader(source, dest)
                        except Exception:
                            has_errors = True
                            _LOGGER.error("Error updating %s. Please update the file manually.", file)

                    if has_errors:
                        hass.components.persistent_notification.async_create(
                            "There was an error updating one or more files of SmartIR. "
                            "Please check the logs for more information.", title='SmartIR')
                    else:
                        hass.components.persistent_notification.async_create(
                            "Successfully updated to {}. Please restart Home Assistant."
                            .format(last_version), title='SmartIR')
    except Exception:
       _LOGGER.error("An error occurred while checking for updates.")

class Helper():

    @staticmethod
    def convert_to_hex(s: str | None) -> bytes | None:
        """
        Remove spaces and 0x/0X prefix from hex string, and return bytes. If None, return None.
        """
        if s is None:
            return None
        s_clean = s.replace(" ", "").replace("0X", "").replace("0x", "")
        return bytes.fromhex(s_clean)

    @staticmethod
    async def downloader(source, dest):
        async with aiohttp.ClientSession() as session:
            async with session.get(source) as response:
                if response.status == 200:
                    async with aiofiles.open(dest, mode='wb') as f:
                        await f.write(await response.read())
                else:
                    raise Exception("File not found")

    @staticmethod
    def pronto_to_lirc(pronto: bytes) -> list[float]:
        """Convert Pronto Hex format to raw IR pulse timings (microseconds).

        The Pronto Hex format encodes infrared remote control signals as a series
        of burst pairs, where each pair represents ON/OFF durations for the IR emitter.
        This function decodes the format and converts it to microsecond timings.

        Pronto Format Structure
        -----------------------
        Word 0: Code type (0x0000 = learned/raw data)
        Word 1: Carrier frequency (freq_khz = 1000000 / (N * 0.241246))
        Word 2: Number of burst pairs in sequence 1 (once sequence)
        Word 3: Number of burst pairs in sequence 2 (repeat sequence)
        Words 4+: Burst pair data (each pair = ON duration, OFF duration)

        Each burst pair value represents the number of carrier cycles for which
        the IR emitter should be ON or OFF. The timing calculation follows the
        official specification:

            freq_hz   = 1_000_000 / (N * 0.241246)
            period_us = N * 0.241246
            time_us   = cycles * period_us = cycles * N * 0.241246

        Using the period directly (rather than dividing by frequency) reduces
        floating point error. No rounding is performed; rounding is deferred to
        the final device-specific quantization step.

        Parameters
        ----------
        pronto : bytes
            Pronto Hex format as bytes (big-endian 16-bit words).

        Returns
        -------
        list[float]
            IR pulse sequence in microseconds. ON/OFF durations alternate.

        Raises
        ------
        ValueError
            If the format is invalid (doesn't start with 0x0000, or length mismatch).
        """
        codes: list[int] = [
            int(binascii.hexlify(pronto[i : i + 2]), 16)
            for i in range(0, len(pronto), 2)
        ]

        if codes[0]:
            raise ValueError("Pronto code should start with 0000")

        # Length should be: 4 (preamble) + 2 * (seq1_pairs + seq2_pairs)
        if len(codes) != 4 + 2 * (codes[2] + codes[3]):
            raise ValueError("Number of pulse widths does not match the preamble")

        period_us: float = codes[1] * 0.241246
        return [code * period_us for code in codes[4:]]

    @staticmethod
    def nec_to_lirc(
        header_bytes: bytes | None,
        command_bytes: bytes,
    ) -> list[float]:
        """Convert NEC frame data to raw IR pulse timings (microseconds).

        Parameters
        ----------
        header_bytes : bytes | None
            Customer code bytes (1 or 2 bytes). If length 1, an inverted byte is
            appended automatically (extended to 16-bit). If None, command_bytes is
            treated as the full frame (already composed).
        command_bytes : bytes
            Command payload bytes (MSB-first)

        Returns
        -------
        list[float]
            Infrared pulse sequence (microseconds). Alternating ON/OFF durations.

        Raises
        ------
        ValueError
            If header_bytes length is invalid.
        """
        if header_bytes is not None:
            if len(header_bytes) == 1:
                # 8bit: append inverted byte to extend to 16bit
                header_bytes = bytes([header_bytes[0], (~header_bytes[0]) & 0xFF])
            elif len(header_bytes) == 2:
                # 16bit: use as is
                pass
            else:
                raise ValueError("NEC header_bytes must be 1 or 2 bytes")

            inv_cmd_bytes: bytes = bytes([(b ^ 0xFF) for b in command_bytes])
            data_bytes: bytes = header_bytes + command_bytes + inv_cmd_bytes
        else:
            # Full frame already provided in command_bytes
            data_bytes = command_bytes

        def t(multiple: int) -> float:
            """Return microseconds for (multiple * T)."""
            # Base T derivation:
            #   Remote IC clock = 455 kHz, divided by 256 -> = 256 / 455000 s
            t_us: float = (256 / 455_000) * 1_000_000  # ≈ 562.637 µs
            return t_us * multiple

        pulses: list[float] = [t(16), t(8)]  # Leader code (ON: 16T, OFF: 8T)
        for byte in data_bytes:
            for bit_pos in range(8):  # LSB first
                bit: int = (byte >> bit_pos) & 0x01
                pulses += (
                    [t(1), t(3)] if bit else [t(1), t(1)]
                )  # 1: ON(1T), OFF(3T); 0: ON(1T), OFF(1T)

        # Trailer code: ON(1T) -> OFF(nnT) where total becomes 192T
        pulses.append(t(1))
        trailer_off: float = t(192) - sum(pulses)
        if trailer_off < 0:
            raise ValueError("NEC frame is too long to fit in standard timing")
        pulses.append(trailer_off)

        return pulses

    @staticmethod
    def lirc_to_pronto(pulses: list[float], carrier_khz: float = 38.0) -> bytes:
        """
        Convert raw IR pulse timings (microseconds) to Pronto Hex format (bytes).

        Parameters
        ----------
        pulses : list[float]
            IR pulse sequence in microseconds. ON/OFF durations alternate.
        carrier_khz : float, optional
            Carrier frequency in kHz (default: 38.0)

        Returns
        -------
        bytes
            Pronto Hex format as bytes (big-endian 16-bit words)
        """
        if len(pulses) % 2 != 0:
            raise ValueError("Pulse list must have even length (ON/OFF pairs)")

        # Pronto carrier word: N = 1_000_000 / (carrier_khz * 0.241246)
        n = round(1_000_000 / (carrier_khz * 0.241246))
        period_us = n * 0.241246

        # Convert each pulse to number of carrier periods (rounded)
        burst_pairs = [round(p / period_us) for p in pulses]

        # Pronto header:
        # Word0: 0x0000 (raw/learned)
        # Word1: carrier
        # Word2: burst pair count (once sequence)
        # Word3: 0 (no repeat sequence)
        word0 = 0x0000
        word1 = n
        word2 = len(burst_pairs) // 2
        word3 = 0x0000

        words = [word0, word1, word2, word3] + burst_pairs

        # Convert to bytes (big-endian 16bit)
        pronto_bytes = b''.join(struct.pack('>H', w) for w in words)
        return pronto_bytes
