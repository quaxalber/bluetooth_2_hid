import asyncio
from asyncio import CancelledError, Task, TaskGroup
from pathlib import Path
import re
from typing import Optional, Union, Any, Type

from adafruit_hid.consumer_control import ConsumerControl
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.mouse import Mouse
from evdev import InputDevice, InputEvent, KeyEvent, RelEvent, categorize, list_devices
import pyudev
import usb_hid
from usb_hid import Device

from .evdev import (
    evdev_to_usb_hid,
    find_key_name,
    get_mouse_movement,
    is_consumer_key,
    is_mouse_button,
)
from .logging import get_logger

_logger = get_logger()

PATH = "path"
MAC = "MAC"
NAME = "name"

PATH_REGEX = r"^/dev/input/event.*$"
MAC_REGEX = r"^([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})$"


class GadgetManager:
    """
    Manages enabling, disabling, and referencing USB HID gadget devices.
    """

    def __init__(self) -> None:
        """
        USB devices (keyboard, mouse, consumer control) are uninitialized until
        :meth:`enable_gadgets` is called.
        """
        self._gadgets = {
            "keyboard": None,
            "mouse": None,
            "consumer": None,
        }
        self._enabled = False

    def enable_gadgets(self) -> None:
        """
        Disables and re-enables usb_hid devices to attach as mouse, keyboard,
        and consumer control. Populates local references to those objects.
        """
        try:
            usb_hid.disable()
        except Exception as ex:
            _logger.debug(f"usb_hid.disable() failed or was already disabled: {ex}")

        usb_hid.enable(
            [
                Device.BOOT_MOUSE,
                Device.KEYBOARD,
                Device.CONSUMER_CONTROL,
            ]  # type: ignore
        )
        enabled_devices = list(usb_hid.devices)  # type: ignore

        self._gadgets["keyboard"] = Keyboard(enabled_devices)
        self._gadgets["mouse"] = Mouse(enabled_devices)
        self._gadgets["consumer"] = ConsumerControl(enabled_devices)
        self._enabled = True

        _logger.debug(f"USB HID gadgets re-initialized: {enabled_devices}")

    def is_enabled(self) -> bool:
        """
        Indicates whether USB HID gadgets have been enabled.

        :return: True if enabled, otherwise False.
        """
        return self._enabled

    def get_keyboard(self) -> Optional[Keyboard]:
        return self._gadgets["keyboard"]

    def get_mouse(self) -> Optional[Mouse]:
        return self._gadgets["mouse"]

    def get_consumer(self) -> Optional[ConsumerControl]:
        return self._gadgets["consumer"]


class ShortcutToggler:
    """
    Tracks a user-defined keyboard shortcut that toggles relaying on/off
    when the entire shortcut is pressed simultaneously.
    """

    def __init__(
        self,
        shortcut_keys: set[str],
        relaying_active: asyncio.Event,
        gadget_manager: "GadgetManager",
    ) -> None:
        """
        :param shortcut_keys: Set of evdev-style key names
          (e.g. {"KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_Q"}).
        :param relaying_active: Event controlling whether relaying is active.
          If set, relaying is ON; if cleared, relaying is OFF.
        :param gadget_manager: Reference to GadgetManager to release keys on toggle-off.
        """
        self.shortcut_keys = shortcut_keys
        self.relaying_active = relaying_active
        self.gadget_manager = gadget_manager
        self.currently_pressed: set[str] = set()

    def handle_key_event(self, event: KeyEvent) -> None:
        """
        Processes an evdev KeyEvent to update pressed keys and toggle relaying
        if all shortcut keys are pressed together.

        :param event: KeyEvent from evdev.
        """
        key_name = find_key_name(event)
        if key_name is None:
            return

        if event.keystate == KeyEvent.key_down:
            self.currently_pressed.add(key_name)
        elif event.keystate == KeyEvent.key_up:
            self.currently_pressed.discard(key_name)

        if self.shortcut_keys and self.shortcut_keys.issubset(self.currently_pressed):
            self.toggle_relaying()

    def toggle_relaying(self) -> None:
        """
        Toggles the global relaying state: if ON, turn OFF; if OFF, turn ON.
        """
        if self.relaying_active.is_set():
            kb = self.gadget_manager.get_keyboard()
            ms = self.gadget_manager.get_mouse()
            if kb:
                kb.release_all()
            if ms:
                ms.release_all()

            self.currently_pressed.clear()
            self.relaying_active.clear()
            _logger.info("ShortcutToggler: Relaying is now OFF.")
        else:
            self.relaying_active.set()
            _logger.info("ShortcutToggler: Relaying is now ON.")


class RelayController:
    """
    Manages the device relays by creating tasks for each relevant input device,
    responding to device add/remove events, etc.
    """

    def __init__(
        self,
        gadget_manager: GadgetManager,
        device_identifiers: Optional[list[str]] = None,
        auto_discover: bool = False,
        skip_name_prefixes: Optional[list[str]] = None,
        grab_devices: bool = False,
        max_blockingio_retries: int = 2,
        blockingio_retry_delay: float = 0.01,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
    ) -> None:
        """
        :param gadget_manager: Must be enabled before relays begin.
        :param device_identifiers: List of strings identifying devices by path, MAC, or name substring.
        :param auto_discover: If True, automatically relay devices not skipped by name prefix.
        :param skip_name_prefixes: If auto_discover is True, skip these device-name prefixes.
        :param grab_devices: If True, attempt to grab exclusive access to each device.
        :param max_blockingio_retries: Times to retry a HID write if it raises BlockingIOError.
        :param blockingio_retry_delay: Delay (seconds) between retries of a blocked HID write.
        :param relaying_active: An asyncio.Event controlling whether we are relaying events.
        :param shortcut_toggler: An optional ShortcutToggler instance for a global toggle shortcut.
        """
        self._gadget_manager = gadget_manager
        self._device_ids = [DeviceIdentifier(id_) for id_ in (device_identifiers or [])]
        self._auto_discover = auto_discover
        self._skip_name_prefixes = skip_name_prefixes or ["vc4-hdmi"]
        self._grab_devices = grab_devices
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler

        self._max_blockingio_retries = max_blockingio_retries
        self._blockingio_retry_delay = blockingio_retry_delay

        self._active_tasks: dict[str, Task] = {}
        self._task_group: Optional[TaskGroup] = None

    def bind_task_group(self, task_group: TaskGroup) -> None:
        """
        Binds a TaskGroup to this controller so that newly added device tasks
        are created within that TaskGroup.

        :param task_group: The Python 3.11 TaskGroup to store tasks.
        """
        self._task_group = task_group

    async def load_initial_devices(self) -> None:
        """
        Scans for existing /dev/input/event* devices and adds them if they match
        the configured rules (explicit IDs or auto-discovery).
        """
        devices = await async_list_input_devices()
        for device in devices:
            if self._should_relay(device):
                self.add_device(device.path)

    def add_device(self, device_path: str) -> None:
        """
        Adds a newly discovered device if it passes checks and isn't already tracked.

        :param device_path: Path string, e.g. "/dev/input/event5".
        """
        if not Path(device_path).exists():
            _logger.debug(f"{device_path} does not exist. Skipping.")
            return

        try:
            device = InputDevice(device_path)
        except (OSError, FileNotFoundError):
            _logger.debug(f"{device_path} vanished before opening.")
            return

        if self._task_group is None:
            _logger.critical(f"No TaskGroup available; ignoring device {device}.")
            return

        if device.path in self._active_tasks:
            _logger.debug(f"Device {device} is already active.")
            return

        task = self._task_group.create_task(
            self._async_relay_events(device), name=device.path
        )
        self._active_tasks[device.path] = task
        _logger.info(f"Relay task created for device {device}.")

    def remove_device(self, device_path: str) -> None:
        """
        Removes a device, canceling its relay task if active.

        :param device_path: String path to the input device, e.g. "/dev/input/event5".
        """
        task = self._active_tasks.pop(device_path, None)
        if task and not task.done():
            _logger.debug(f"Cancelling relay for {device_path}.")
            task.cancel()
        else:
            _logger.debug(f"No active relay task found for {device_path}.")

    async def _async_relay_events(self, device: InputDevice) -> None:
        """
        Main loop for reading events from a single InputDevice and forwarding them
        to the USB HID gadgets.

        :param device: The evdev InputDevice to relay.
        """
        try:
            async with DeviceRelay(
                device=device,
                gadget_manager=self._gadget_manager,
                grab_device=self._grab_devices,
                max_blockingio_retries=self._max_blockingio_retries,
                blockingio_retry_delay=self._blockingio_retry_delay,
                relaying_active=self._relaying_active,
                shortcut_toggler=self._shortcut_toggler,
            ) as relay:
                _logger.info(f"Activated {relay}")
                await relay.async_relay_events_loop()

        except CancelledError:
            _logger.debug(f"Relay cancelled for device {device}.")
            raise
        except (OSError, FileNotFoundError):
            _logger.info(f"Lost connection to {device}.")
        except Exception as ex:
            _logger.exception(
                f"Unhandled exception in relay for {device}.", exc_info=ex
            )
        finally:
            self.remove_device(device.path)

    def _should_relay(self, device: InputDevice) -> bool:
        """
        Determines whether a device should be relayed based on auto-discovery,
        skip prefix rules, or explicit device identifiers.

        :param device: evdev InputDevice.
        :return: True if it should be relayed, False otherwise.
        """
        name_lower = device.name.lower()
        if self._auto_discover:
            for prefix in self._skip_name_prefixes:
                if name_lower.startswith(prefix.lower()):
                    return False
            return True

        return any(identifier.matches(device) for identifier in self._device_ids)


class DeviceRelay:
    """
    A per-device relay that forwards evdev input events to the appropriate
    USB HID gadgets. When the device is grabbed, no other process receives
    its events.
    """

    def __init__(
        self,
        device: InputDevice,
        gadget_manager: GadgetManager,
        grab_device: bool = False,
        max_blockingio_retries: int = 2,
        blockingio_retry_delay: float = 0.01,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
    ) -> None:
        """
        :param device: evdev InputDevice to read events from.
        :param gadget_manager: Provides references to HID gadgets.
        :param grab_device: If True, attempt exclusive access.
        :param max_blockingio_retries: Times to retry if HID writes block.
        :param blockingio_retry_delay: Delay between blocking write retries.
        :param relaying_active: Controls whether events are forwarded.
        :param shortcut_toggler: Optional ShortcutToggler for toggling relay on/off.
        """
        self._input_device = device
        self._gadget_manager = gadget_manager
        self._grab_device = grab_device
        self._max_blockingio_retries = max_blockingio_retries
        self._blockingio_retry_delay = blockingio_retry_delay
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler
        self._currently_grabbed = False

    async def __aenter__(self) -> "DeviceRelay":
        """
        Enters the DeviceRelay context, optionally grabbing exclusive access
        to the device.

        :return: The DeviceRelay instance.
        """
        if self._grab_device:
            try:
                self._input_device.grab()
                self._currently_grabbed = True
            except Exception as ex:
                _logger.warning(f"Could not grab {self._input_device.path}: {ex}")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> bool:
        """
        Exits the DeviceRelay context, ungrabbing the device if necessary.
        Returns False so that any exception is not suppressed.

        :return: Always False.
        """
        if self._grab_device:
            try:
                self._input_device.ungrab()
                self._currently_grabbed = False
            except Exception as ex:
                _logger.warning(f"Unable to ungrab {self._input_device.path}: {ex}")
        return False

    def __str__(self) -> str:
        return f"Relay for {self._input_device}"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._input_device!r}, grab={self._grab_device})"

    async def async_relay_events_loop(self) -> None:
        """
        Continuously reads events from the device and relays them to HID.
        Suspends relaying if ``relaying_active`` is cleared.
        """
        async for input_event in self._input_device.async_read_loop():
            event = categorize(input_event)

            if self._shortcut_toggler and isinstance(event, KeyEvent):
                self._shortcut_toggler.handle_key_event(event)

            active = self._relaying_active.is_set() if self._relaying_active else True

            # Grab/ungrab device if relaying state changes
            if self._grab_device and active and not self._currently_grabbed:
                try:
                    self._input_device.grab()
                    self._currently_grabbed = True
                    _logger.debug(f"Grabbed {self._input_device}")
                except Exception as ex:
                    _logger.warning(f"Could not grab {self._input_device}: {ex}")

            elif self._grab_device and not active and self._currently_grabbed:
                try:
                    self._input_device.ungrab()
                    self._currently_grabbed = False
                    _logger.debug(f"Ungrabbed {self._input_device}")
                except Exception as ex:
                    _logger.warning(f"Could not ungrab {self._input_device}: {ex}")

            if not active:
                continue

            _logger.debug(f"Received {event} from {self._input_device.name}")

            await self._process_event_with_retry(event)

    async def _process_event_with_retry(self, event: InputEvent) -> None:
        """
        Attempts to relay an event. Retries on BlockingIOError up to
        ``_max_blockingio_retries`` times, then drops the event.

        Catches other exceptions to prevent relay-task crashes.

        :param event: The evdev InputEvent to relay.
        """
        for attempt in range(self._max_blockingio_retries):
            try:
                relay_event(event, self._gadget_manager)
                return
            except BlockingIOError:
                if attempt < self._max_blockingio_retries - 1:
                    _logger.debug(
                        f"HID write blocked (attempt {attempt+1}); retrying after "
                        f"{self._blockingio_retry_delay}s."
                    )
                    await asyncio.sleep(self._blockingio_retry_delay)
                else:
                    _logger.warning(
                        f"HID write blocked on final retry—skipping event {event}."
                    )
                    return
            except BrokenPipeError:
                _logger.warning(
                    "BrokenPipeError: Possibly disconnected USB or power-only cable. Pausing relay."
                )
                if self._relaying_active:
                    self._relaying_active.clear()
                return
            except Exception as ex:
                _logger.exception(f"Unexpected error processing event {event}: {ex}")
                return


class DeviceIdentifier:
    """
    Identifies an input device by path, MAC address, or substring of the device name.
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
        return f"{self.__class__.__name__}({self.value!r})"

    def _determine_identifier_type(self) -> str:
        if re.match(PATH_REGEX, self._value):
            return PATH
        if re.match(MAC_REGEX, self._value):
            return MAC
        return NAME

    def _normalize_identifier(self) -> str:
        if self.type == PATH:
            return self._value
        if self.type == MAC:
            return self._value.lower().replace("-", ":")
        return self._value.lower()

    def matches(self, device: InputDevice) -> bool:
        """
        Checks if this identifier matches the given device by:

        - Path (exact match)
        - MAC (comparing device.uniq)
        - Name (substring match)

        :param device: The evdev device to check.
        :return: True if a match is found.
        """
        if self.type == PATH:
            return self.value == device.path
        if self.type == MAC:
            return self.normalized_value == (device.uniq or "").lower()
        return self.normalized_value in device.name.lower()


async def async_list_input_devices() -> list[InputDevice]:
    """
    Returns a list of /dev/input/event* devices, ignoring any that
    cannot be opened.

    :return: List of InputDevice objects.
    """
    try:
        return [InputDevice(path) for path in list_devices()]
    except (OSError, FileNotFoundError) as ex:
        _logger.critical(f"Failed listing devices: {ex}")
        return []
    except Exception as ex:
        _logger.exception(f"Unexpected error listing devices: {ex}")
        return []


def relay_event(event: InputEvent, gadget_manager: GadgetManager) -> None:
    """
    Routes an evdev InputEvent to the appropriate USB HID device
    (mouse movement, mouse buttons, keyboard keys, consumer control).

    :param event: The evdev InputEvent (rel or key).
    :param gadget_manager: Provides references to the USB HID devices.
    """
    if isinstance(event, RelEvent):
        move_mouse(event, gadget_manager)
    elif isinstance(event, KeyEvent):
        send_key_event(event, gadget_manager)


def move_mouse(event: RelEvent, gadget_manager: GadgetManager) -> None:
    """
    Sends mouse movement or wheel scroll to the USB HID Mouse gadget.

    :param event: A rel-event describing mouse movement or wheel scroll.
    :param gadget_manager: Access to the Mouse gadget instance.
    """
    mouse = gadget_manager.get_mouse()
    if mouse is None:
        raise RuntimeError("Mouse gadget not initialized or manager not enabled.")

    x, y, mwheel = get_mouse_movement(event)
    mouse.move(x, y, mwheel)


def send_key_event(event: KeyEvent, gadget_manager: GadgetManager) -> None:
    """
    Sends a key-press or release event to Keyboard, Mouse (buttons),
    or ConsumerControl.

    :param event: A KeyEvent from evdev.
    :param gadget_manager: Access to HID gadgets.
    """
    key_id, key_name = evdev_to_usb_hid(event)
    if key_id is None or key_name is None:
        return

    output_gadget = get_output_device(event, gadget_manager)
    if output_gadget is None:
        raise RuntimeError("No appropriate USB gadget found.")

    if event.keystate == KeyEvent.key_down:
        _logger.debug(f"Pressing {key_name} (0x{key_id:02X})")
        output_gadget.press(key_id)
    elif event.keystate == KeyEvent.key_up:
        _logger.debug(f"Releasing {key_name} (0x{key_id:02X})")
        output_gadget.release(key_id)


def get_output_device(
    event: KeyEvent, gadget_manager: GadgetManager
) -> Union[ConsumerControl, Keyboard, Mouse, None]:
    """
    Chooses which HID gadget to use based on the KeyEvent type:
    - Consumer keys go to ConsumerControl.
    - Mouse buttons go to Mouse.
    - All others go to Keyboard.

    :param event: KeyEvent from evdev.
    :param gadget_manager: Access to the relevant gadget objects.
    :return: The appropriate gadget or None if none available.
    """
    if is_consumer_key(event):
        return gadget_manager.get_consumer()
    elif is_mouse_button(event):
        return gadget_manager.get_mouse()
    return gadget_manager.get_keyboard()


class UdcStateMonitor:
    """
    Monitors the UDC 'state' file in /sys/class/udc/<controller>/state.
    When it reads 'configured', it sets the relaying_active event, otherwise clears it.
    """

    def __init__(
        self,
        relaying_active: asyncio.Event,
        udc_path: Path = Path("/sys/class/udc/20980000.usb/state"),
        poll_interval: float = 0.5,
    ):
        """
        :param relaying_active: Event controlling relay on/off.
        :param udc_path: Path to the 'state' file for your UDC.
        :param poll_interval: How often to check the file for changes, in seconds.
        """
        self._relaying_active = relaying_active
        self.udc_path = udc_path
        self.poll_interval = poll_interval

        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._last_state: str | None = None

        if not self.udc_path.is_file():
            _logger.warning(
                f"UDC state file {self.udc_path} not found. UDC monitoring may not work."
            )

    async def __aenter__(self) -> "UdcStateMonitor":
        """
        Starts periodic polling of the UDC state file.

        :return: This UdcStateMonitor instance.
        """
        self._stop = False
        self._task = asyncio.create_task(self._poll_state_loop())
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> bool:
        """
        Stops the background polling task.

        :return: False to not suppress exceptions.
        """
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return False

    async def _poll_state_loop(self) -> None:
        """
        Periodically reads the UDC state file and sets/clears relaying_active
        based on whether the host is 'configured'.
        """
        while not self._stop:
            new_state = self._read_udc_state()
            if new_state != self._last_state:
                self._handle_state_change(new_state)
                self._last_state = new_state
            await asyncio.sleep(self.poll_interval)

    def _read_udc_state(self) -> str:
        """
        Reads the UDC state file (e.g. 'configured', 'not attached', etc.)
        If the file is not found, returns 'not_attached'.

        :return: Current state string.
        """
        try:
            with open(self.udc_path, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            return "not_attached"

    def _handle_state_change(self, new_state: str):
        """
        Sets or clears the relaying_active event based on 'configured' state.

        :param new_state: UDC state from the sysfs file.
        """
        _logger.debug(f"UDC state changed to '{new_state}'.")
        if new_state == "configured":
            _logger.debug("Host connected. Relaying enabled.")
            self._relaying_active.set()
        else:
            _logger.debug("Host disconnected or unconfigured. Relaying paused.")
            self._relaying_active.clear()


class UdevEventMonitor:
    """
    Monitors udev for add/remove events on /dev/input/event* devices
    and notifies the provided RelayController.
    """

    def __init__(self, relay_controller: "RelayController") -> None:
        """
        :param relay_controller: The RelayController to notify on add/remove.
        """
        self.relay_controller = relay_controller

        self.context = pyudev.Context()
        self.monitor_input = pyudev.Monitor.from_netlink(self.context)
        self.monitor_input.filter_by("input")

        self.observer_input = pyudev.MonitorObserver(
            self.monitor_input, self._udev_event_callback_input
        )

        _logger.debug("UdevEventMonitor initialized.")

    async def __aenter__(self) -> "UdevEventMonitor":
        """
        Starts the MonitorObserver for /dev/input events.
        """
        self.observer_input.start()
        _logger.debug("UdevEventMonitor started observer.")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> bool:
        """
        Stops the MonitorObserver.
        """
        self.observer_input.stop()
        _logger.debug("UdevEventMonitor stopped observer.")
        return False

    def _udev_event_callback_input(self, action: str, device: pyudev.Device) -> None:
        """
        Handles udev add/remove events for input devices.

        :param action: 'add' or 'remove'.
        :param device: pyudev.Device object with metadata.
        """
        device_node = device.device_node
        if not device_node or not device_node.startswith("/dev/input/event"):
            return

        if action == "add":
            _logger.debug(f"UdevEventMonitor: Added input => {device_node}")
            self.relay_controller.add_device(device_node)
        elif action == "remove":
            _logger.debug(f"UdevEventMonitor: Removed input => {device_node}")
            self.relay_controller.remove_device(device_node)
