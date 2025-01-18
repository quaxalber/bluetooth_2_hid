import asyncio
from asyncio import CancelledError, Task, TaskGroup
from pathlib import Path
import re
from typing import NoReturn, Optional, Union, Any, Type

from adafruit_hid.consumer_control import ConsumerControl
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.mouse import Mouse
from evdev import InputDevice, InputEvent, KeyEvent, RelEvent, categorize, list_devices
import pyudev
import usb_hid
from usb_hid import Device

from .evdev import (
    evdev_to_usb_hid,
    get_mouse_movement,
    is_consumer_key,
    is_mouse_button,
)
from .logging import get_logger

_logger = get_logger()
_keyboard_gadget: Optional[Keyboard] = None
_mouse_gadget: Optional[Mouse] = None
_consumer_gadget: Optional[ConsumerControl] = None

PATH = "path"
MAC = "MAC"
NAME = "name"
PATH_REGEX = r"^\/dev\/input\/event.*$"
MAC_REGEX = r"^([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})$"


async def async_list_input_devices() -> list[InputDevice]:
    """
    Return a list of available /dev/input/event* devices.
    """
    devices = []
    try:
        devices = [InputDevice(path) for path in list_devices()]
    except Exception:
        _logger.exception("Failed listing devices")
        await asyncio.sleep(1)
    return devices


def init_usb_gadgets() -> None:
    """
    Disable and re-enable the usb_hid devices so that we can
    attach as a mouse, keyboard, and consumer control.
    """
    _logger.debug("Initializing USB gadgets...")
    try:
        usb_hid.disable()
    except:
        pass
    usb_hid.enable(
        [
            Device.BOOT_MOUSE,
            Device.KEYBOARD,
            Device.CONSUMER_CONTROL,
        ]  # type: ignore
    )

    global _keyboard_gadget, _mouse_gadget, _consumer_gadget
    enabled_devices: list[Device] = list(usb_hid.devices)  # type: ignore

    _keyboard_gadget = Keyboard(enabled_devices)
    _mouse_gadget = Mouse(enabled_devices)
    _consumer_gadget = ConsumerControl(enabled_devices)
    _logger.debug(f"Enabled USB gadgets: {enabled_devices}")


class DeviceIdentifier:
    """
    Identifies an input device by either:
    - Path (/dev/input/eventX)
    - MAC address (XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX)
    - Name substring
    """

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

    def __str__(self) -> str:
        return f'{self.type} "{self.value}"'

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.value})"

    def _determine_identifier_type(self) -> str:
        if re.match(PATH_REGEX, self.value):
            return PATH
        if re.match(MAC_REGEX, self.value):
            return MAC
        return NAME

    def _normalize_identifier(self) -> str:
        if self.type == PATH:
            return self.value
        if self.type == MAC:
            return self.value.lower().replace("-", ":")
        return self.value.lower()

    def matches(self, device: InputDevice) -> bool:
        """
        Return True if this DeviceIdentifier matches the given evdev InputDevice.
        """
        if self.type == PATH:
            return self.value == device.path
        if self.type == MAC:
            # device.uniq is the MAC address from evdev
            return self.normalized_value == device.uniq
        return self.normalized_value in device.name.lower()


class DeviceRelay:
    """
    A relay for a single InputDevice, forwarding events to the USB HID gadgets.
    Uses a context manager to grab/ungrab the device if grab_device=True.
    """

    def __init__(self, input_device: InputDevice, grab_device: bool = False) -> None:
        self._input_device = input_device
        self._grab_device = grab_device

    async def __aenter__(self) -> "DeviceRelay":
        if self._grab_device:
            self._input_device.grab()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> bool:
        if self._grab_device:
            try:
                self._input_device.ungrab()
            except Exception:
                _logger.debug(f"Unable to ungrab {self._input_device.path}")
        return False  # don't suppress exceptions

    @property
    def input_device(self) -> InputDevice:
        return self._input_device

    def __str__(self) -> str:
        return f"relay for {self.input_device}"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.input_device!r}, {self._grab_device})"

    async def async_relay_events_loop(self) -> NoReturn:
        """
        Continuously read events from the device and relay them
        to the corresponding USB HID gadget.
        This method never returns unless an error or cancellation occurs.
        """
        async for input_event in self._input_device.async_read_loop():
            event = categorize(input_event)
            _logger.debug(f"Received {event} from {self._input_device.name}")
            await self._process_event_with_retry(event)

    async def _process_event_with_retry(self, event: InputEvent) -> None:
        """
        Attempt to relay the given event up to two times if a BlockingIOError occurs.
        """
        for attempt in range(2):
            try:
                _relay_event(event)
                return
            except BlockingIOError:
                if attempt == 0:
                    _logger.debug("HID write blocked, retrying...")
                    await asyncio.sleep(0.01)
                else:
                    _logger.debug(f"HID write blocked again — skipping {event}.")
                    return


def _move_mouse(event: RelEvent) -> None:
    """
    Relay relative movement events to the USB HID Mouse gadget.
    Raises BlockingIOError if the HID write cannot be completed.
    """
    if _mouse_gadget is None:
        raise RuntimeError("Mouse gadget not initialized")

    x, y, mwheel = get_mouse_movement(event)
    coordinates = f"(x={x}, y={y}, mwheel={mwheel})"

    try:
        _logger.debug(f"Moving {_mouse_gadget} {coordinates}")
        _mouse_gadget.move(x, y, mwheel)
    except BlockingIOError:
        raise
    except Exception:
        _logger.exception(f"Failed moving {_mouse_gadget} {coordinates}")


def _send_key(event: KeyEvent) -> None:
    """
    Relay key press/release events to the appropriate USB HID gadget
    (keyboard, mouse-button, or consumer control).
    Raises BlockingIOError if the HID write cannot be completed.
    """
    key_id, key_name = evdev_to_usb_hid(event)
    if key_id is None or key_name is None:
        return

    device_out = _get_output_device(event)
    if device_out is None:
        raise RuntimeError("USB gadget not initialized")

    try:
        if event.keystate == KeyEvent.key_down:
            _logger.debug(f"Pressing {key_name} (0x{key_id:02X}) on {device_out}")
            device_out.press(key_id)
        elif event.keystate == KeyEvent.key_up:
            _logger.debug(f"Releasing {key_name} (0x{key_id:02X}) on {device_out}")
            device_out.release(key_id)
    except BlockingIOError:
        raise
    except Exception:
        _logger.exception(f"Failed sending 0x{key_id:02X} to {device_out}")


def _get_output_device(
    event: KeyEvent,
) -> Union[ConsumerControl, Keyboard, Mouse, None]:
    """
    Decide which HID gadget to use based on event type:
    - Consumer keys: ConsumerControl
    - Mouse buttons: Mouse
    - Otherwise: Keyboard
    """
    if is_consumer_key(event):
        return _consumer_gadget
    elif is_mouse_button(event):
        return _mouse_gadget
    return _keyboard_gadget


def _relay_event(event: InputEvent) -> None:
    """
    Relay an event to the correct USB HID function.
    This function may raise BlockingIOError if the HID device is busy.
    """
    if isinstance(event, RelEvent):
        _move_mouse(event)
    elif isinstance(event, KeyEvent):
        _send_key(event)
    # Otherwise, ignore the event.


class RelayController:
    """
    Manages the TaskGroup of all active DeviceRelay tasks and handles
    add/remove events from UdevEventMonitor.
    """

    def __init__(
        self,
        device_identifiers: Optional[list[str]] = None,
        auto_discover: bool = False,
        grab_devices: bool = False,
    ) -> None:
        if not device_identifiers:
            device_identifiers = []
        self._device_ids = [DeviceIdentifier(id) for id in device_identifiers]
        self._auto_discover = auto_discover
        self._grab_devices = grab_devices
        self._active_tasks: dict[str, Task] = {}
        self._task_group: Optional[TaskGroup] = None
        self._cancelled = False

        init_usb_gadgets()

    async def async_relay_devices(self) -> None:
        """
        Main method that opens a TaskGroup and waits forever,
        while device add/remove is handled dynamically.
        """
        try:
            async with TaskGroup() as task_group:
                self._task_group = task_group
                _logger.debug("RelayController: TaskGroup started.")

                for device in await async_list_input_devices():
                    if self._should_relay(device):
                        self.add_device(device.path)

                while not self._cancelled:
                    await asyncio.sleep(0.1)
        except* Exception as exc_grp:
            _logger.exception(
                "RelayController: Exception in TaskGroup", exc_info=exc_grp
            )
        finally:
            self._task_group = None
            _logger.info("RelayController: TaskGroup exited.")

    def add_device(self, device_path: str) -> None:
        """
        Called when a new device is detected. Schedules a new relay task if
        the device passes the _should_relay() check and isn't already tracked.
        """
        if not Path(device_path).exists():
            _logger.debug(f"{device_path} does not exist.")
            return

        # Attempt to open the device
        try:
            device = InputDevice(device_path)
        except Exception:
            _logger.debug(f"{device_path} vanished before we could open it.")
            return

        if self._task_group is None:
            _logger.critical(f"No TaskGroup available; ignoring {device}.")
            return

        if device.path in self._active_tasks:
            _logger.debug(f"Device {device} is already active.")
            return

        task = self._task_group.create_task(
            self._async_relay_events(device), name=device.path
        )
        self._active_tasks[device.path] = task
        _logger.debug(f"Created task for {device}.")

    def remove_device(self, device_path: str) -> None:
        """
        Called when a device is removed. Cancels the associated relay task if running.
        """
        task = self._active_tasks.pop(device_path, None)
        if task and not task.done():
            _logger.debug(f"Cancelling relay for {device_path}.")
            task.cancel()
        else:
            _logger.debug(f"No active task found for {device_path} to remove.")

    async def _async_relay_events(self, device: InputDevice) -> None:
        """
        Creates a DeviceRelay in a context manager, then loops forever reading events.
        """
        try:
            async with DeviceRelay(device, self._grab_devices) as relay:
                _logger.info(f"Activated {relay}")
                await relay.async_relay_events_loop()
        except CancelledError:
            _logger.debug(f"Relay cancelled for device {device}.")
            raise
        except (OSError, FileNotFoundError) as ex:
            _logger.critical(f"Lost connection to {device} [{ex!r}].")
        except Exception:
            _logger.exception(f"Unhandled exception in relay for {device}.")
        finally:
            self.remove_device(device.path)

    def _should_relay(self, device: InputDevice) -> bool:
        """
        Return True if we should relay this device.

        If auto_discover is True, skip only devices that start with "vc4-hdmi".
        Otherwise, check if any configured DeviceIdentifier matches.
        """
        if self._auto_discover and not device.name.startswith("vc4-hdmi"):
            return True
        return any(identifier.matches(device) for identifier in self._device_ids)


class UdevEventMonitor:
    """
    Watches for new/removed /dev/input/event* devices and notifies RelayController.
    """

    def __init__(
        self, relay_controller: RelayController, loop: asyncio.AbstractEventLoop
    ):
        self.relay_controller = relay_controller
        self.loop = loop
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by(subsystem="input")

        # Create an observer that calls _udev_event_callback on add/remove
        self.observer = pyudev.MonitorObserver(self.monitor, self._udev_event_callback)
        self.observer.start()
        _logger.debug("UdevEventMonitor started.")

    def _udev_event_callback(self, action: str, device: pyudev.Device) -> None:
        """pyudev callback for device add/remove events."""
        device_node = device.device_node
        if not device_node or not device_node.startswith("/dev/input/event"):
            return

        if action == "add":
            _logger.debug(f"UdevEventMonitor: Added => {device_node}")
            self.relay_controller.add_device(device_node)
        elif action == "remove":
            _logger.debug(f"UdevEventMonitor: Removed => {device_node}")
            self.relay_controller.remove_device(device_node)
