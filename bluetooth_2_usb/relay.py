import asyncio
from asyncio import CancelledError, TaskGroup, Task
import re
from typing import NoReturn

from adafruit_hid.consumer_control import ConsumerControl
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.mouse import Mouse
from bluetooth_2_usb.input_device_user_config import InputDeviceUserConfig

from evdev import (
    InputDevice,
    InputEvent,
    KeyEvent,
    RelEvent,
    categorize,
    list_devices,
)
import usb_hid
from usb_hid import Device

from bluetooth_2_usb.evdev import (
    get_mouse_movement,
    is_consumer_key,
    is_mouse_button,
    evdev_to_usb_hid,
)
from bluetooth_2_usb.logging import get_logger


_logger = get_logger()


class IdentifierType:
    PATH = "path"
    MAC = "MAC"
    NAME = "name"


class InputDeviceIdentifier:
    def __init__(self, device_identifier: str) -> None:
        self._value = device_identifier
        self._type = self._determine_identifier_type()
        self._normalized_value = self._normalize_identifier()

    @property
    def value(self) -> str:
        return self._value

    @property
    def normalized_value(self) -> str:
        return self._normalized_value

    @property
    def type(self) -> str:
        return self._type

    def __str__(self):
        return self.value

    def __repr__(self):
        return f"identifier {self.value} (type: {self.type})"

    def _determine_identifier_type(self) -> str:
        mac_regex = r"^([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})$"
        path_regex = r"^\/dev\/input\/event.*$"
        if re.match(mac_regex, self.value):
            return IdentifierType.MAC
        if re.match(path_regex, self.value):
            return IdentifierType.PATH
        return IdentifierType.NAME

    def _normalize_identifier(self) -> str:
        if self.type == IdentifierType.PATH:
            return self.value
        if self.type == IdentifierType.NAME:
            return self.value.lower()
        if self.type == IdentifierType.MAC:
            return self.value.lower().replace("-", ":")

    def matches(self, device: InputDevice) -> bool:
        if self.type == IdentifierType.PATH:
            return self.value == device.path
        if self.type == IdentifierType.NAME:
            return self.normalized_value in f"{device.name}".lower()
        if self.type == IdentifierType.MAC:
            return self.normalized_value == device.uniq


class InputDeviceRelay:
    def __init__(self, input_device: InputDevice):
        self._input_device = input_device
        self._keyboard_gadget = Keyboard(usb_hid.devices)
        self._mouse_gadget = Mouse(usb_hid.devices)
        self._consumer_gadget = ConsumerControl(usb_hid.devices)

    @property
    def input_device(self) -> InputDevice:
        return self._input_device

    def __str__(self):
        return self.input_device.name

    def __repr__(self):
        return str(self.input_device)

    async def async_relay_events_loop(self):
        async for event in self.input_device.async_read_loop():
            await self._async_relay_single_event(event)

    async def _async_relay_single_event(self, event: InputEvent) -> None:
        categorized_event = categorize(event)
        _logger.debug(f"Received event: [{categorized_event}]")
        if isinstance(categorized_event, KeyEvent):
            await self._async_send_key(categorized_event)
        elif isinstance(categorized_event, RelEvent):
            await self._async_move_mouse(categorized_event)

    async def _async_send_key(self, event: KeyEvent) -> None:
        key_id, key_name = evdev_to_usb_hid(event)
        if key_id is None:
            return
        device_out = self._get_output_device(event)
        try:
            if event.keystate == KeyEvent.key_down:
                _logger.debug(f"Pressing {key_name} (0x{key_id:02X}) on {device_out}")
                device_out.press(key_id)
            elif event.keystate == KeyEvent.key_up:
                _logger.debug(f"Releasing {key_name} (0x{key_id:02X}) on {device_out}")
                device_out.release(key_id)
        except Exception:
            _logger.exception(f"Failed sending 0x{key_id:02X} to {device_out}")

    def _get_output_device(self, event: KeyEvent) -> ConsumerControl | Keyboard | Mouse:
        if is_consumer_key(event):
            return self._consumer_gadget
        elif is_mouse_button(event):
            return self._mouse_gadget
        return self._keyboard_gadget

    async def _async_move_mouse(self, event: RelEvent) -> None:
        x, y, mwheel = get_mouse_movement(event)
        coordinates = f"(x={x}, y={y}, mwheel={mwheel})"
        try:
            _logger.debug(f"Moving mouse {self._mouse_gadget} {coordinates}")
            self._mouse_gadget.move(x, y, mwheel)
        except Exception:
            _logger.exception(f"Failed moving mouse {self._mouse_gadget} {coordinates}")


class RelayController:
    """
    This class serves as a HID relay to handle Bluetooth keyboard and mouse events from multiple input devices and translate them to USB.
    """

    def __init__(
        self, input_device_user_config: InputDeviceUserConfig, device_identifiers: list[str] = None
    ) -> None:
        _enable_usb_devices()
        if not device_identifiers:
            device_identifiers = []
        self._device_identifiers = [
            InputDeviceIdentifier(id) for id in device_identifiers
        ]
        self._auto_discover = input_device_user_config['autodiscover']
        self._grab_device = input_device_user_config['grab_device']
        self._task_group: TaskGroup = None
        self._discovery_task: Task = None
        self._relay_tasks: dict[str, Task] = {}

    async def async_relay_devices(self) -> NoReturn:
        try:
            async with TaskGroup() as self._task_group:
                self._create_discovery_task()
            _logger.critical("Event loop closed.")
        except* Exception:
            _logger.exception("Error(s) in TaskGroup")

    def _create_discovery_task(self) -> None:
        task = self._task_group.create_task(
            self._async_discover_devices_loop(), name="Device discovery"
        )
        self._discovery_task = task

    async def _async_discover_devices_loop(self) -> None:
        _logger.info(f"Discovering input devices...")
        while True:
            self._discover_devices()
            await asyncio.sleep(1)

    def _discover_devices(self) -> None:
        for device in list_readable_devices():
            if self._should_relay(device):
                if(self._grab_device == True):
                    device.grab()
                self._create_relay_task(device)

    def _should_relay(self, device: InputDevice) -> bool:
        return not self._has_task(device) and self._matches_discovery_criteria(device)

    def _matches_discovery_criteria(self, device: InputDevice) -> bool:
        return self._auto_discover or self._matches_identifier(device)

    def _matches_identifier(self, device: InputDevice) -> bool:
        return any(id.matches(device) for id in self._device_identifiers)

    def _has_task(self, device: InputDevice) -> bool:
        return device.path in self._relay_tasks

    def _create_relay_task(self, device: InputDevice) -> None:
        task = self._task_group.create_task(
            self._async_relay_events(device), name=device.path
        )
        self._relay_tasks[device.path] = task

    async def _async_relay_events(self, device: InputDevice) -> None:
        relay = InputDeviceRelay(device)
        try:
            _logger.info(f"Relaying {str(device)}")
            await relay.async_relay_events_loop()
        except CancelledError:
            _logger.critical(f"{device} received a cancellation request.")
        except (OSError, FileNotFoundError) as ex:
            _logger.critical(f"Connection lost to {device}... [{repr(ex)}]")
        except Exception:
            _logger.exception(f"{device} failed!")
            await asyncio.sleep(2)
        finally:
            self._cancel_device_task(device)

    def _cancel_device_task(self, device: InputDevice) -> None:
        task = self._relay_tasks.pop(device.path, None)
        if task:
            task.cancel()


def list_readable_devices() -> list[InputDevice]:
    return [InputDevice(path) for path in list_devices()]


def _enable_usb_devices():
    usb_hid.enable(
        [
            Device.MOUSE,
            Device.KEYBOARD,
            Device.CONSUMER_CONTROL,
        ]
    )
    _logger.debug(f"Available USB devices: {usb_hid.devices}")
