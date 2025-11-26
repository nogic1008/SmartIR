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
ENC_AEHA = 'Aeha'
ENC_SONY = 'Sony'
ENC_SHARP = 'Sharp'

PROTOCOL_BASED_ENCODING = [ENC_NEC, ENC_AEHA, ENC_SONY, ENC_SHARP]
BROADLINK_COMMANDS_ENCODING = [ENC_BASE64, ENC_HEX, ENC_PRONTO] + PROTOCOL_BASED_ENCODING
XIAOMI_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW] + PROTOCOL_BASED_ENCODING
MQTT_COMMANDS_ENCODING = [ENC_RAW]
LOOKIN_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW] + PROTOCOL_BASED_ENCODING
ESPHOME_COMMANDS_ENCODING = [ENC_RAW]


def get_controller(hass, controller, encoding, controller_data, delay):
    """Return a controller compatible with the specification provided."""
    controllers = {
        BROADLINK_CONTROLLER: BroadlinkController,
        XIAOMI_CONTROLLER: XiaomiController,
        MQTT_CONTROLLER: MQTTController,
        LOOKIN_CONTROLLER: LookinController,
        ESPHOME_CONTROLLER: ESPHomeController
    }
    try:
        return controllers[controller](hass, controller, encoding, controller_data, delay)
    except KeyError:
        raise Exception("The controller is not supported.")


class AbstractController(ABC):
    """Representation of a controller."""

    def __init__(self, hass, controller, encoding, controller_data, delay):
        self.check_encoding(encoding)
        self.hass = hass
        self._controller = controller
        self._encoding = encoding
        self._controller_data = controller_data
        self._delay = delay

    @abstractmethod
    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        pass

    @abstractmethod
    async def send(self, command):
        """Send a command."""
        pass

    @staticmethod
    def to_lirc(encoding: str, command: str) -> list[float]:
        """Convert command to LIRC pulses based on encoding."""
        if encoding == ENC_PRONTO:
            command = command.replace(" ", "")
            command_bytes = bytearray.fromhex(command)
            return Helper.pronto_to_lirc(command_bytes)

        if encoding == ENC_NEC:
            command_bytes = Helper.convert_to_hex(command)
            return Helper.nec_to_lirc(command_bytes)

        if encoding == ENC_AEHA:
            command_bytes = Helper.convert_to_hex(command)
            return Helper.aeha_to_lirc(command_bytes)

        if encoding == ENC_SONY:
            command_bytes = Helper.convert_to_hex(command)
            return Helper.sony_to_lirc(command_bytes)

        if encoding == ENC_SHARP:
            command_bytes = Helper.convert_to_hex(command)
            return Helper.sharp_to_lirc(command_bytes)

        raise ValueError(f"Encoding {encoding} cannot be converted to LIRC.")


class BroadlinkController(AbstractController):
    """Controls a Broadlink device."""

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
            try:
                if self._encoding == ENC_HEX:
                    _command = binascii.unhexlify(_command)
                    _command = b64encode(_command).decode("utf-8")

                if (
                    self._encoding in PROTOCOL_BASED_ENCODING
                    or self._encoding == ENC_PRONTO
                ):
                    # Convert to LIRC then Broadlink Base64
                    command_bytes = AbstractController.to_lirc(self._encoding, _command)
                    _command = b64encode(
                        BroadlinkController.lirc_to_broadlink(command_bytes)
                    ).decode("utf-8")
            except:
                raise Exception(
                    f"Error while converting {self._encoding} to Base64 encoding"
                )

            commands.append('b64:' + _command)

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            'command':  commands,
            'delay_secs': self._delay
        }

        await self.hass.services.async_call(
            'remote', 'send_command', service_data)


    @staticmethod
    def lirc_to_broadlink(pulses: list[float]) -> bytearray:
        """Convert LIRC pulses to Broadlink format."""
        array = bytearray()

        for pulse in pulses:
            pulse = int(pulse * 269 / 8192)

            if pulse < 256:
                array += bytearray(struct.pack('>B', pulse))
            else:
                array += bytearray([0x00])
                array += bytearray(struct.pack('>H', pulse))

        packet = bytearray([0x26, 0x00])
        packet += bytearray(struct.pack('<H', len(array)))
        packet += array
        packet += bytearray([0x0d, 0x05])

        # Add 0s to make ultimate packet size a multiple of 16 for 128-bit AES encryption.
        remainder = (len(packet) + 4) % 16
        if remainder:
            packet += bytearray(16 - remainder)
        return packet

class XiaomiController(AbstractController):
    """Controls a Xiaomi device."""

    def check_encoding(self, encoding):
        """Check if the encoding is supported by the controller."""
        if encoding not in XIAOMI_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported "
                            "by the Xiaomi controller.")

    async def send(self, command):
        """Send a command."""
        encoding = self._encoding.lower()

        if self._encoding in PROTOCOL_BASED_ENCODING:
            # Convert to Pronto
            command_bytes = AbstractController.to_lirc(self._encoding, command)
            command_bytes = Helper.lirc_to_pronto(
                command_bytes, 40.0 if self._encoding == ENC_SONY else 38.0
            )
            command = " ".join(f"{b:02X}" for b in command_bytes)
            encoding = ENC_PRONTO.lower()

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            "command": f"{encoding}:" + command,
        }

        await self.hass.services.async_call("remote", "send_command", service_data)


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
        encoding = self._encoding.lower().replace("pronto", "prontohex")
        if self._encoding in PROTOCOL_BASED_ENCODING:
            # Convert to Pronto
            command_bytes = AbstractController.to_lirc(self._encoding, command)
            command_bytes = Helper.lirc_to_pronto(
                command_bytes, 40.0 if self._encoding == ENC_SONY else 38.0
            )
            command = " ".join(f"{b:02X}" for b in command_bytes)
            encoding = "prontohex"

        url = f"http://{self._controller_data}/commands/ir/{encoding}/{command}"
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