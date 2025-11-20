from abc import ABC, abstractmethod
from base64 import b64encode
import binascii
import struct
import requests
import logging
import json

from homeassistant.const import ATTR_ENTITY_ID
from . import Helper

_LOGGER = logging.getLogger(__name__)

BROADLINK_CONTROLLER = 'Broadlink'
XIAOMI_CONTROLLER = 'Xiaomi'
MQTT_CONTROLLER = 'MQTT'
LOOKIN_CONTROLLER = 'LOOKin'
ESPHOME_CONTROLLER = 'ESPHome'

ENC_BASE64 = 'Base64'
ENC_HEX = 'Hex'
ENC_PRONTO = 'Pronto'
ENC_RAW = 'Raw'
ENC_NEC = 'Nec'

BROADLINK_COMMANDS_ENCODING = [ENC_BASE64, ENC_HEX, ENC_PRONTO, ENC_NEC]
XIAOMI_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW, ENC_NEC]
MQTT_COMMANDS_ENCODING = [ENC_RAW]
LOOKIN_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW, ENC_NEC]
ESPHOME_COMMANDS_ENCODING = [ENC_RAW]


def get_controller(
    hass, controller, encoding, controller_data, delay, header_code=None
):
    """Return a controller compatible with the specification provided."""
    controllers = {
        BROADLINK_CONTROLLER: BroadlinkController,
        XIAOMI_CONTROLLER: XiaomiController,
        MQTT_CONTROLLER: MQTTController,
        LOOKIN_CONTROLLER: LookinController,
        ESPHOME_CONTROLLER: ESPHomeController,
    }
    try:
        return controllers[controller](
            hass, controller, encoding, controller_data, delay, header_code
        )
    except KeyError:
        raise Exception("The controller is not supported.")


class AbstractController(ABC):
    """Representation of a controller."""

    def __init__(
        self, hass, controller, encoding, controller_data, delay, header_code=None
    ):
        self.check_encoding(encoding)
        self.hass = hass
        self._controller = controller
        self._encoding = encoding
        self._controller_data = controller_data
        self._delay = delay
        self._header_code = header_code

    @abstractmethod
    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        pass

    @abstractmethod
    async def send(self, command):
        """Send a command."""
        pass


class BroadlinkController(AbstractController):
    """Controls a Broadlink device."""

    @staticmethod
    def lirc2broadlink(pulses: list[float]) -> str:
        """Convert IR pulse timings to Broadlink base64-encoded command.

        Parameters
        ----------
        pulses : list[float]
            Infrared pulse sequence (in microseconds). ON/OFF alternate.

        Returns
        -------
        str
            Broadlink base64-encoded IR command.
        """
        array: bytearray = bytearray()

        for pulse in pulses:
            # Quantize to Broadlink tick unit (≈ 30.45 µs) and convert
            # Broadlink tick unit ≈ (8192 / 269) µs
            pulse = round(pulse * 269 / 8192)

            if pulse < 256:
                array += bytearray(struct.pack(">B", pulse))
            else:
                array += bytearray([0x00])
                array += bytearray(struct.pack(">H", pulse))

        packet: bytearray = bytearray([0x26, 0x00])
        packet += bytearray(struct.pack("<H", len(array)))
        packet += array
        packet += bytearray([0x0D, 0x05])

        # Add 0s to make ultimate packet size a multiple of 16 for 128-bit AES encryption.
        remainder: int = (len(packet) + 4) % 16
        if remainder:
            packet += bytearray(16 - remainder)

        return b64encode(packet).decode("utf-8")

    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        if encoding not in BROADLINK_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported "
                            "by the Broadlink controller.")

    async def send(self, command):
        """Send a command."""
        commands = []

        if not isinstance(command, list): 
            command = [command]

        for _command in command:
            if self._encoding == ENC_HEX:
                try:
                    _command = binascii.unhexlify(_command)
                    _command = b64encode(_command).decode('utf-8')
                except:
                    raise Exception("Error while converting "
                                    "Hex to Base64 encoding")

            if self._encoding == ENC_PRONTO:
                try:
                    _command = _command.replace(" ", "")
                    pronto_bytes = bytearray.fromhex(_command)
                    pulses = Helper.pronto_to_lirc(pronto_bytes)
                    _command = self.lirc2broadlink(pulses)
                except:
                    raise Exception("Error while converting Pronto to Base64 encoding")

            if self._encoding == ENC_NEC:
                try:
                    header_bytes = Helper.convert_to_hex(self._header_code)
                    command_bytes = Helper.convert_to_hex(_command)
                    if command_bytes is None:
                        raise ValueError("NEC command hex string is None")
                    pulses = Helper.nec_to_lirc(header_bytes, command_bytes)
                    _command = self.lirc2broadlink(pulses)
                except Exception:
                    raise Exception("Error while converting NEC to Base64 encoding")

            commands.append('b64:' + _command)

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            'command':  commands,
            'delay_secs': self._delay
        }

        await self.hass.services.async_call(
            'remote', 'send_command', service_data)


class XiaomiController(AbstractController):
    """Controls a Xiaomi device."""

    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        if encoding not in XIAOMI_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported "
                            "by the Xiaomi controller.")

    async def send(self, command):
        """Send a command."""

        if self._encoding == ENC_NEC:
            # Convert NEC to Pronto
            header_bytes = Helper.convert_to_hex(self._header_code)
            command_bytes = Helper.convert_to_hex(command)
            if command_bytes is None:
                raise ValueError("NEC command hex string is None")
            pulses = Helper.nec_to_lirc(header_bytes, command_bytes)
            pronto_bytes = Helper.lirc_to_pronto(pulses)

            encoding = ENC_PRONTO.lower()
            send_command = " ".join(f"{b:02X}" for b in pronto_bytes)
        else:
            encoding = self._encoding.lower()
            send_command = command

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            "command": encoding + ":" + send_command,
        }

        await self.hass.services.async_call(
            'remote', 'send_command', service_data)


class MQTTController(AbstractController):
    """Controls a MQTT device."""

    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        if encoding not in MQTT_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported "
                            "by the mqtt controller.")

    async def send(self, command):
        """Send a command."""
        service_data = {
            'topic': self._controller_data,
            'payload': command
        }

        await self.hass.services.async_call(
            'mqtt', 'publish', service_data)


class LookinController(AbstractController):
    """Controls a Lookin device."""

    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        if encoding not in LOOKIN_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported "
                            "by the LOOKin controller.")

    async def send(self, command):
        """Send a command."""

        if self._encoding == ENC_NEC:
            # Convert NEC to Pronto
            header_bytes = Helper.convert_to_hex(self._header_code)
            command_bytes = Helper.convert_to_hex(command)
            if command_bytes is None:
                raise ValueError("NEC command hex string is None")
            pulses = Helper.nec_to_lirc(header_bytes, command_bytes)
            pronto_bytes = Helper.lirc_to_pronto(pulses)

            encoding = 'prontohex'
            send_command = " ".join(f"{b:02X}" for b in pronto_bytes)
        else:
            encoding = self._encoding.lower().replace('pronto', 'prontohex')
            send_command = command

        url = f"http://{self._controller_data}/commands/ir/" \
                f"{encoding}/{send_command}"
        await self.hass.async_add_executor_job(requests.get, url)


class ESPHomeController(AbstractController):
    """Controls a ESPHome device."""

    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        if encoding not in ESPHOME_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported "
                            "by the ESPHome controller.")
    
    async def send(self, command):
        """Send a command."""
        service_data = {'command':  json.loads(command)}

        await self.hass.services.async_call(
            'esphome', self._controller_data, service_data)