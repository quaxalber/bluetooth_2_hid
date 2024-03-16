import asyncio
from enum import Flag
from typing import NoReturn, Dict
from adafruit_hid.keyboard import Keyboard
import usb_hid
from usb_hid import Device

from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTAttributePermissions
)
from bleak.backends.bluezdbus.characteristic import ( # type: ignore
    _GattCharacteristicsFlagsEnum
)

from .logging import get_logger

from src.bluetooth_2_usb.shortcut_parser import ShortcutParser
from src.bluetooth_2_usb.relay import (all_gadgets_ready, init_usb_gadgets)


class CustomGATTCharacteristicProperties(Flag):
    broadcast = 0x00001
    read = 0x00002
    write_without_response = 0x00004
    write = 0x00008
    notify = 0x00010
    indicate = 0x00020
    authenticated_signed_writes = 0x00040
    extended_properties = 0x00080
    reliable_write = 0x00100
    writable_auxiliaries = 0x00200
    encrypt_read = 0x00400
    encrypt_write = 0x00800
    encrypt_authenticated_read = 0x01000
    encrypt_authenticated_write = 0x02000
    secure_read = 0x04000 #(Server only)
    secure_write = 0x08000 #(Server only)
    authorize = 0x10000


# HACK: redefine disabled characteristic mapping for bless to bluezdbus backend
# see https://github.com/hbldh/bleak/blob/master/bleak/backends/bluezdbus/characteristic.py#L20-L26
_AddCustomGattCharacteristicsFlagsEnum: dict[int, str] = {
    0x00400: "encrypt-read",
    0x00800: "encrypt-write",
    0x01000: "encrypt-authenticated-read",
    0x02000: "encrypt-authenticated-write",
    0x04000: "secure-read", #(Server only)
    0x08000: "secure-write", #(Server only)
    0x10000: "authorize",
}
for key in _AddCustomGattCharacteristicsFlagsEnum:
    _GattCharacteristicsFlagsEnum[key] = _AddCustomGattCharacteristicsFlagsEnum[key]

_logger = get_logger()

GATT_SERVER_NAME = f"Bluetooth 2 USB"
GATT_SERVICE_ID = "00000000-6907-4437-8539-9218a9d54e29"
GATT_CHARACTERISTIC_ID = "00000001-6907-4437-8539-9218a9d54e29"


class BleRelay:

    def __init__(
            self,
            accept_non_trusted: bool = False,
            partial_parse: bool = False) -> None:
        self._accept_non_trusted = accept_non_trusted
        self._partial_parse = partial_parse
        self._shortcut_parser = ShortcutParser()
        if not all_gadgets_ready():
            init_usb_gadgets()
        enabled_devices: list[Device] = list(usb_hid.devices) # type: ignore
        self._keyboard_gadget = Keyboard(enabled_devices)

    def __str__(self) -> str:
        return "BLE TO HID relay"

    async def async_relay_events_loop(self) -> NoReturn:
        gatt_properties = (CustomGATTCharacteristicProperties.encrypt_authenticated_read
            | CustomGATTCharacteristicProperties.encrypt_authenticated_write)
        gatt_permissions = (GATTAttributePermissions.read_encryption_required
            | GATTAttributePermissions.write_encryption_required)

        if (self._accept_non_trusted):
            gatt_properties =  CustomGATTCharacteristicProperties.read | CustomGATTCharacteristicProperties.write
            gatt_permissions = GATTAttributePermissions.readable | GATTAttributePermissions.writeable

        # Instantiate the server
        gatt: Dict = {
            GATT_SERVICE_ID: {
                GATT_CHARACTERISTIC_ID: {
                    "Properties": gatt_properties,
                    "Permissions": gatt_permissions,
                    "Value": None
                },
            }
        }
        server = BlessServer(name=GATT_SERVER_NAME)
        server.read_request_func = self._read_request
        server.write_request_func = self._write_request

        _logger.debug("Starting GATT server")
        await server.add_gatt(gatt)
        await server.start()
        _logger.debug("GATT server started")

        try:
            while True:
                await asyncio.sleep(0.5)
        except* Exception:
            _logger.debug("GATT server stopping")
            await server.stop()
            _logger.debug("GATT server stopped")

    def _read_request(
            self,
            characteristic: BlessGATTCharacteristic,
            **kwargs
            ) -> bytearray:
        if characteristic.uuid != GATT_CHARACTERISTIC_ID:
            raise RuntimeError(f"Invalid characteristic '{characteristic.uuid}'")
        _logger.debug(f"Read last input value '{characteristic.value}' for '{characteristic.uuid}'")
        return characteristic.value

    def _write_request(
            self,
            characteristic: BlessGATTCharacteristic,
            value: bytearray,
            **kwargs
            ):
        if characteristic.uuid != GATT_CHARACTERISTIC_ID:
            raise RuntimeError(f"Invalid characteristic '{characteristic.uuid}'")
        input = value.decode()
        _logger.debug(f"Received input '{input}' for '{characteristic.uuid}'")
        parsed_input = self._shortcut_parser.parse_command(input, raise_error=not self._partial_parse)
        if len(parsed_input) == 0:
            _logger.debug(f"Ignoring invalid input '{input}'.")
            return

        _logger.debug(f"Keys to send: {parsed_input}")
        for shortcut in parsed_input:
            self._keyboard_gadget.send(*shortcut.keycodes)
        characteristic.value = value

        _logger.debug(f"Processed input '{input}' for '{characteristic.uuid}'/")


class RelayBleController:
    """
    This class serves as a BLE HID relay to handle Bluetooth GATT characteristic write events and translate them to USB.
    """

    def __init__(
            self,
            accept_non_trusted: bool = False,
            partial_parse: bool = False) -> None:
        self._partial_parse = partial_parse
        self._accept_non_trusted = accept_non_trusted

    async def async_relay_ble(self) -> NoReturn:
        try:
            relay = BleRelay(self._accept_non_trusted, self._partial_parse)
            _logger.info(f"Activated {relay}. Pairing required: {not self._accept_non_trusted}. Allows invalid input: {self._partial_parse}")
            _logger.info(f"Use {GATT_SERVICE_ID} service / {GATT_CHARACTERISTIC_ID} characteristic to send keystrokes.")
            await relay.async_relay_events_loop()
        except* Exception:
            _logger.exception("Error(s) in relay")