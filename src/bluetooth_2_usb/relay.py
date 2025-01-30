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

PATH_REGEX = r"^/dev/input/event.*$"
MAC_REGEX = r"^([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})$"


class GadgetManager:
    """
    Manages enabling, disabling, and referencing USB HID gadget devices (keyboard/mouse/consumer).
    """

    def __init__(self) -> None:
        """
        The actual HID gadgets remain uninitialized until :meth:`enable_gadgets` is called.
        """
        self._gadgets = {
            "keyboard": None,
            "mouse": None,
            "consumer": None,
        }
        self._enabled = False

    def enable_gadgets(self) -> None:
        """
        Disables and then re-enables usb_hid gadget devices, attaching
        as mouse, keyboard, and consumer control.
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
        :return: True if devices have been enabled, False otherwise.
        :rtype: bool
        """
        return self._enabled

    def get_keyboard(self) -> Optional[Keyboard]:
        """
        :return: The enabled Keyboard gadget, or None if not available.
        :rtype: Optional[Keyboard]
        """
        return self._gadgets["keyboard"]

    def get_mouse(self) -> Optional[Mouse]:
        """
        :return: The enabled Mouse gadget, or None if not available.
        :rtype: Optional[Mouse]
        """
        return self._gadgets["mouse"]

    def get_consumer(self) -> Optional[ConsumerControl]:
        """
        :return: The enabled ConsumerControl gadget, or None if not available.
        :rtype: Optional[ConsumerControl]
        """
        return self._gadgets["consumer"]


class ShortcutToggler:
    """
    Tracks a user-defined shortcut and toggles relaying on/off when
    that shortcut is fully pressed.
    """

    def __init__(
        self,
        shortcut_keys: set[str],
        relaying_active: asyncio.Event,
        gadget_manager: "GadgetManager",
    ) -> None:
        """
        :param shortcut_keys:
            Set of evdev-style key names, e.g. {"KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_Q"}.
        :type shortcut_keys: set[str]
        :param relaying_active:
            An asyncio.Event controlling whether relaying is active.
            If .is_set(), relaying is ON; if .is_clear(), relaying is OFF.
        :type relaying_active: asyncio.Event
        :param gadget_manager:
            The gadget manager for forcibly releasing keys/buttons on toggle-off.
        :type gadget_manager: GadgetManager
        """
        self.shortcut_keys = shortcut_keys
        self.relaying_active = relaying_active
        self.gadget_manager = gadget_manager
        self.currently_pressed: set[str] = set()

    def handle_key_event(self, event: KeyEvent) -> None:
        """
        Handles an evdev KeyEvent by updating pressed-key tracking.

        :param event: A categorized evdev KeyEvent.
        :type event: KeyEvent
        """
        key_name = find_key_name(event)
        if key_name is None:
            return

        if event.keystate == KeyEvent.key_down:
            self.currently_pressed.add(key_name)
        elif event.keystate == KeyEvent.key_up:
            self.currently_pressed.discard(key_name)

        if self.shortcut_keys.issubset(self.currently_pressed):
            self.toggle_relaying()

    def toggle_relaying(self) -> None:
        """
        Toggles the global relaying state: if it was on, turn it off; otherwise turn it on.
        """
        if self.relaying_active.is_set():
            keyboard = self.gadget_manager.get_keyboard()
            mouse = self.gadget_manager.get_mouse()
            if keyboard:
                keyboard.release_all()
            if mouse:
                mouse.release_all()
            self.currently_pressed.clear()

            self.relaying_active.clear()
            _logger.info("ShortcutToggler: Relaying is now OFF.")
        else:
            self.relaying_active.set()
            _logger.info("ShortcutToggler: Relaying is now ON.")


class RelayController:
    """
    Manages relaying of multiple input devices to USB HID gadgets.

    If auto_discover is True, it attempts to relay all valid input devices
    except those specifically skipped. Devices can also be explicitly added by path/MAC/name.
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
        shortcut_toggler: Optional["ShortcutToggler"] = None,
        relaying_active: Optional[asyncio.Event] = None,
        devices_found_event: Optional[asyncio.Event] = None,
        update_relaying_callback: Optional[callable] = None,
    ) -> None:
        """
        :param gadget_manager:
            A :class:`GadgetManager` instance that should already be enabled.
        :type gadget_manager: GadgetManager
        :param device_identifiers:
            A list of path, MAC, or name fragments to identify devices to relay.
        :type device_identifiers: Optional[list[str]]
        :param auto_discover:
            If True, automatically relay any device whose name does not start with skip_name_prefixes.
        :type auto_discover: bool
        :param skip_name_prefixes:
            A list of device.name prefixes to skip if auto_discover is True.
        :type skip_name_prefixes: Optional[list[str]]
        :param grab_devices:
            If True, tries to grab exclusive access to each device.
        :type grab_devices: bool
        :param max_blockingio_retries:
            How many times to retry a blocked HID write.
        :type max_blockingio_retries: int
        :param blockingio_retry_delay:
            Delay between retries (seconds).
        :type blockingio_retry_delay: float
        :param shortcut_toggler:
            Optional toggler for user-defined “interrupt” shortcuts.
        :type shortcut_toggler: ShortcutToggler
        :param relaying_active:
            Event signaling whether relaying is active (set) or not (clear).
        :type relaying_active: asyncio.Event
        :param devices_found_event:
            Event to set if at least one device is active, else clear.
        :type devices_found_event: asyncio.Event
        :param update_relaying_callback:
            Function to be called whenever the set of active devices changes
            (so external code can decide whether to enable/disable relaying).
        :type update_relaying_callback: callable
        """
        self._gadget_manager = gadget_manager
        self._device_ids = [DeviceIdentifier(id) for id in (device_identifiers or [])]
        self._auto_discover = auto_discover
        self._skip_name_prefixes = skip_name_prefixes or ["vc4-hdmi"]
        self._grab_devices = grab_devices
        self._shortcut_toggler = shortcut_toggler
        self._max_blockingio_retries = max_blockingio_retries
        self._blockingio_retry_delay = blockingio_retry_delay
        self._relaying_active = relaying_active
        self._devices_found_event = devices_found_event
        self._update_relaying_callback = update_relaying_callback

        self._active_tasks: dict[str, Task] = {}
        self._task_group: Optional[TaskGroup] = None
        self._cancelled = False

    def start_relaying(self, task_group: TaskGroup) -> None:
        """
        Kicks off the asynchronous device-relay process by creating
        a task in the given TaskGroup.

        :param task_group: An asyncio.TaskGroup in which to schedule the relaying.
        :type task_group: TaskGroup
        """
        self._task_group = task_group
        task_group.create_task(self._async_relay_devices(), name="RelayControllerMain")

    async def _async_relay_devices(self) -> None:
        """
        Discovers existing input devices, spawns relay tasks for them,
        and then idles while device add/remove is handled by external calls.
        """
        try:
            initial_devices = await async_list_input_devices()
            for dev in initial_devices:
                if self._should_relay(dev):
                    self.add_device(dev.path)

            while not self._cancelled:
                await asyncio.sleep(0.2)
        except* Exception:
            _logger.exception("RelayController: Exception in TaskGroup")
        finally:
            self._task_group = None
            _logger.debug("RelayController: Exiting main relay loop.")

    def add_device(self, device_path: str) -> None:
        """
        Called externally (e.g. by UdevEventMonitor) when a new device is detected.

        :param device_path: The filesystem path, e.g. "/dev/input/eventX".
        :type device_path: str
        """
        if not Path(device_path).exists():
            return

        if device_path in self._active_tasks:
            _logger.debug(f"Device {device_path} is already active.")
            return

        try:
            device = InputDevice(device_path)
        except (OSError, FileNotFoundError):
            _logger.debug(f"{device_path} vanished before we could open it.")
            return

        if not self._task_group:
            _logger.critical(f"No TaskGroup available; ignoring add_device({device}).")
            return

        task = self._task_group.create_task(
            self._async_relay_events(device), name=f"DeviceRelay({device_path})"
        )
        self._active_tasks[device_path] = task

        _logger.info(f"Added relay for device: {device.name} ({device_path})")
        self._update_devices_found_event()

    def remove_device(self, device_path: str) -> None:
        """
        Called externally (e.g. by UdevEventMonitor) when a device is removed.

        :param device_path: Filesystem path of the removed device.
        :type device_path: str
        """
        task = self._active_tasks.pop(device_path, None)
        if task and not task.done():
            _logger.info(f"Removing relay for {device_path}")
            task.cancel()
        self._update_devices_found_event()

    async def _async_relay_events(self, device: InputDevice) -> None:
        """
        Creates a DeviceRelay in a context manager, then loops forever reading events.

        :param device: The input device to relay.
        :type device: InputDevice
        """
        try:
            async with DeviceRelay(
                device,
                self._gadget_manager,
                grab_device=self._grab_devices,
                max_blockingio_retries=self._max_blockingio_retries,
                blockingio_retry_delay=self._blockingio_retry_delay,
                relaying_active=self._relaying_active,
                shortcut_toggler=self._shortcut_toggler,
            ) as relay:
                _logger.info(f"Activated {relay}")
                await relay.async_relay_events_loop()
        except CancelledError:
            _logger.debug(f"Relay cancelled for {device}.")
            raise
        except (OSError, FileNotFoundError):
            _logger.info(f"Lost connection to {device}.")
        except Exception:
            _logger.exception(f"Unhandled exception in relay for {device}.")
        finally:
            self.remove_device(device.path)

    def _should_relay(self, device: InputDevice) -> bool:
        """
        Determines whether this device should be relayed.

        :param device: The input device to evaluate.
        :type device: InputDevice
        :return: True if device should be relayed, False otherwise.
        :rtype: bool
        """
        name_lower = device.name.lower()
        if self._auto_discover:
            for prefix in self._skip_name_prefixes:
                if name_lower.startswith(prefix.lower()):
                    return False
            return True
        return any(identifier.matches(device) for identifier in self._device_ids)

    def _update_devices_found_event(self):
        """
        Sets or clears the devices_found_event based on the current active task count.
        Also invokes the relay-activation callback if provided.
        """
        if not self._devices_found_event:
            return

        if self._active_tasks:
            self._devices_found_event.set()
        else:
            self._devices_found_event.clear()

        if self._update_relaying_callback:
            self._update_relaying_callback()


class DeviceRelay:
    """
    A relay for a single InputDevice, forwarding events to USB HID gadgets.
    """

    def __init__(
        self,
        input_device: InputDevice,
        gadget_manager: GadgetManager,
        grab_device: bool = False,
        max_blockingio_retries: int = 2,
        blockingio_retry_delay: float = 0.01,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
    ) -> None:
        """
        :param input_device: The evdev input device to be relayed.
        :type input_device: InputDevice
        :param gadget_manager: Provides access to keyboard/mouse/consumer HID gadgets.
        :type gadget_manager: GadgetManager
        :param grab_device: If True, grabs exclusive access to input_device.
        :type grab_device: bool
        :param max_blockingio_retries: How many times to retry a blocked HID write.
        :type max_blockingio_retries: int
        :param blockingio_retry_delay: Delay between retries in seconds.
        :type blockingio_retry_delay: float
        :param shortcut_toggler: Optional toggler for user-defined “interrupt” shortcuts.
        :type shortcut_toggler: ShortcutToggler
        """
        self._input_device = input_device
        self._gadget_manager = gadget_manager
        self._grab_device = grab_device
        self._max_blockingio_retries = max_blockingio_retries
        self._blockingio_retry_delay = blockingio_retry_delay
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler

        self._currently_grabbed = False

    async def __aenter__(self) -> "DeviceRelay":
        """
        Asynchronously enters the DeviceRelay context, optionally grabbing the device.

        :return: Self, for usage in an async context.
        :rtype: DeviceRelay
        """
        if self._grab_device:
            try:
                self._input_device.grab()
                _logger.debug(f"Grabbed {self._input_device}")
            except Exception as ex:
                _logger.warning(f"Could not grab {self._input_device}: {ex}")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> bool:
        """
        Asynchronously exits the DeviceRelay context, optionally ungrabbing the device.

        :return: Always False to propagate exceptions.
        :rtype: bool
        """
        if self._grab_device:
            try:
                self._input_device.ungrab()
                _logger.debug(f"Ungrabbed {self._input_device}")
            except Exception as ex:
                _logger.warning(f"Unable to ungrab {self._input_device.path}: {ex}")
        return False

    def __str__(self) -> str:
        return f"relay for {self._input_device.path} ({self._input_device.name})"

    async def async_relay_events_loop(self) -> None:
        """
        Continuously reads events from the device (async evdev loop)
        and relays them to the USB HID gadgets.
        Ends if an error or cancellation occurs.
        """
        async for input_event in self._input_device.async_read_loop():
            event = categorize(input_event)

            if self._shortcut_toggler and isinstance(event, KeyEvent):
                self._shortcut_toggler.handle_key_event(event)

            active = self._relaying_active and self._relaying_active.is_set()

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

            if any(
                isinstance(event, event_type) for event_type in [KeyEvent, RelEvent]
            ):
                _logger.debug(
                    f"Received {event} from {self._input_device.name} ({self._input_device.path})"
                )

            await self._process_event_with_retry(event)

    async def _process_event_with_retry(self, event: InputEvent) -> None:
        """
        Processes an input event and relays it to the USB HID gadget,
        retrying if a BlockingIOError occurs.

        :param event: The input event to relay.
        :type event: InputEvent
        """
        for attempt in range(self._max_blockingio_retries):
            try:
                relay_event(event, self._gadget_manager)
                return
            except BlockingIOError:
                if attempt < self._max_blockingio_retries - 1:
                    _logger.debug(
                        f"HID write blocked on attempt {attempt+1}; retrying after "
                        f"{self._blockingio_retry_delay}s."
                    )
                    await asyncio.sleep(self._blockingio_retry_delay)
                else:
                    _logger.warning(
                        f"HID write still blocked on final retry. Skipping {event}."
                    )
                    return
            except BrokenPipeError:
                _logger.warning(
                    "BrokenPipeError: USB cable possibly disconnected or power-only."
                )
                return
            except Exception:
                _logger.exception(f"Unexpected error while processing {event}")
                return


class DeviceIdentifier:
    """
    Identifies an input device by either:
      * Path (/dev/input/eventX)
      * MAC address (XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX)
      * Name substring
    """

    def __init__(self, device_identifier: str) -> None:
        """
        :param device_identifier: Path, MAC, or name fragment.
        :type device_identifier: str
        """
        self._value = device_identifier
        self._type = self._determine_identifier_type()
        self._normalized_value = self._normalize_identifier()

    def _determine_identifier_type(self) -> str:
        if re.match(PATH_REGEX, self._value):
            return "path"
        if re.match(MAC_REGEX, self._value):
            return "mac"
        return "name"

    def _normalize_identifier(self) -> str:
        if self._type == "path":
            return self._value
        if self._type == "mac":
            return self._value.lower().replace("-", ":")
        return self._value.lower()

    def matches(self, device: InputDevice) -> bool:
        """
        Checks if this identifier matches the given InputDevice.

        :param device: The input device to check.
        :type device: InputDevice
        :return: True if it matches, False otherwise.
        :rtype: bool
        """
        if self._type == "path":
            return device.path == self._value
        if self._type == "mac":
            return (device.uniq or "").lower() == self._normalized_value
        return self._normalized_value in device.name.lower()


async def async_list_input_devices() -> list[InputDevice]:
    """
    Returns a list of available `/dev/input/event*` devices.

    :return: A list of InputDevice objects.
    :rtype: list[InputDevice]
    """
    try:
        return [InputDevice(path) for path in list_devices()]
    except (OSError, FileNotFoundError) as ex:
        _logger.critical(f"Failed listing devices: {ex}")
        return []
    except Exception:
        _logger.exception("Unexpected error listing devices.")
        return []


def relay_event(event: InputEvent, gadget_manager: GadgetManager) -> None:
    """
    Relays an event to the correct USB HID gadget.

    :param event: The input event to relay.
    :type event: InputEvent
    :param gadget_manager: Manages references to keyboard/mouse/consumer HID gadgets.
    :type gadget_manager: GadgetManager
    :raises BlockingIOError: If the HID write is blocked (retried externally).
    """
    if isinstance(event, RelEvent):
        move_mouse(event, gadget_manager)
    elif isinstance(event, KeyEvent):
        send_key_event(event, gadget_manager)


def move_mouse(event: RelEvent, gadget_manager: GadgetManager) -> None:
    """
    Sends relative mouse movement events to the USB HID Mouse gadget.

    :param event: An evdev RelEvent (e.g., REL_X, REL_Y, REL_WHEEL).
    :type event: RelEvent
    :raises RuntimeError: If the mouse gadget is not initialized.
    """
    mouse = gadget_manager.get_mouse()
    if mouse is None:
        raise RuntimeError("Mouse gadget not initialized or manager not enabled.")
    x, y, mwheel = get_mouse_movement(event)
    mouse.move(x, y, mwheel)


def send_key_event(event: KeyEvent, gadget_manager: GadgetManager) -> None:
    """
    Sends key press/release events to the appropriate USB HID gadget.

    :param event: A categorized evdev KeyEvent.
    :type event: KeyEvent
    :param gadget_manager: Manages references to keyboard/mouse/consumer HID gadgets.
    :type gadget_manager: GadgetManager
    :raises RuntimeError: If no appropriate USB gadget is found.
    """
    key_id, key_name = evdev_to_usb_hid(event)
    if key_id is None or key_name is None:
        return

    output_gadget = get_output_device(event, gadget_manager)
    if output_gadget is None:
        raise RuntimeError("No appropriate USB gadget found or manager not enabled.")

    if event.keystate == KeyEvent.key_down:
        output_gadget.press(key_id)
    elif event.keystate == KeyEvent.key_up:
        output_gadget.release(key_id)


def get_output_device(
    event: KeyEvent, gadget_manager: GadgetManager
) -> Union[ConsumerControl, Keyboard, Mouse, None]:
    """
    Decides which HID gadget to use based on the event type:
      * Consumer keys -> ConsumerControl
      * Mouse buttons -> Mouse
      * Otherwise -> Keyboard

    :param event: A categorized KeyEvent.
    :type event: KeyEvent
    :param gadget_manager: The GadgetManager instance.
    :type gadget_manager: GadgetManager
    :return: The appropriate HID gadget or None if none is available.
    :rtype: Union[ConsumerControl, Keyboard, Mouse, None]
    """
    if is_consumer_key(event):
        return gadget_manager.get_consumer()
    elif is_mouse_button(event):
        return gadget_manager.get_mouse()
    return gadget_manager.get_keyboard()


class UdcStateMonitor:
    """
    Periodically checks /sys/class/udc/<controller>/state to detect whether
    the USB is "configured" by the host. On state change:
      - If "configured": sets udc_configured_event
      - Otherwise: clears udc_configured_event
    """

    def __init__(
        self,
        udc_configured_event: asyncio.Event,
        update_relaying_callback: callable,
        udc_path: Path,
        poll_interval: float = 0.5,
    ):
        """
        :param udc_configured_event:
            Event that is set when the UDC is "configured", cleared otherwise.
        :type udc_configured_event: asyncio.Event
        :param update_relaying_callback:
            A function to invoke after changing the event.
        :type update_relaying_callback: callable
        :param udc_path:
            Path to the UDC "state" file, e.g. /sys/class/udc/<controller>/state
        :type udc_path: pathlib.Path
        :param poll_interval:
            How often (in seconds) to poll the state file.
        :type poll_interval: float
        """
        self._udc_configured_event = udc_configured_event
        self._update_relaying_callback = update_relaying_callback
        self._udc_path = udc_path
        self._poll_interval = poll_interval

        self._stop = False
        self._task: asyncio.Task | None = None
        self._last_state: str | None = None

        if not udc_path.is_file():
            _logger.warning(f"UDC state file {udc_path} not found.")

    async def __aenter__(self):
        """
        Starts the async polling when entering the context.

        :return: This UdcStateMonitor instance.
        :rtype: UdcStateMonitor
        """
        self._stop = False
        self._task = asyncio.create_task(self._poll_state(), name="UdcStateMonitor")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Cancels polling when exiting the context.

        :return: Always False to propagate exceptions.
        :rtype: bool
        """
        self._stop = True
        if self._task:
            self._task.cancel()
        return False

    async def _poll_state(self):
        """
        Periodically reads the UDC "state" file, detects transitions to/from "configured",
        and sets/clears the event.
        """
        while not self._stop:
            new_state = self._read_udc_state()
            if new_state != self._last_state:
                _logger.debug(f"UDC state changed to '{new_state}'")
                self._handle_state_change(new_state)
                self._last_state = new_state
            await asyncio.sleep(self._poll_interval)

    def _read_udc_state(self) -> str:
        try:
            with open(self._udc_path, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            return "not_attached"

    def _handle_state_change(self, new_state: str):
        _logger.debug(f"UDC state changed to '{new_state}'")
        if new_state == "configured":
            self._udc_configured_event.set()
        else:
            self._udc_configured_event.clear()
        self._update_relaying_callback()


class UdevEventMonitor:
    """
    Watches for new/removed /dev/input/event* devices and notifies the RelayController.
    """

    def __init__(self, relay_controller: "RelayController") -> None:
        """
        :param relay_controller: The RelayController instance to notify.
        :type relay_controller: RelayController
        """
        self._relay_controller = relay_controller
        monitor = pyudev.Monitor.from_netlink(pyudev.Context())
        monitor.filter_by("input")
        self._observer = pyudev.MonitorObserver(monitor, self._udev_event_callback)

        _logger.debug("UdevEventMonitor initialized.")

    async def __aenter__(self):
        """
        Asynchronously starts the pyudev observer.

        :return: This UdevEventMonitor instance.
        :rtype: UdevEventMonitor
        """
        self._observer.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Asynchronously stops the pyudev observer.

        :return: Always False to propagate exceptions.
        :rtype: bool
        """
        self._observer.stop()
        return False

    def _udev_event_callback(self, action: str, device: pyudev.Device) -> None:
        device_node = device.device_node
        if not device_node or not device_node.startswith("/dev/input/event"):
            return

        if action == "add":
            _logger.debug(f"Udev: Added input => {device_node}")
            self._relay_controller.add_device(device_node)
        elif action == "remove":
            _logger.debug(f"Udev: Removed input => {device_node}")
            self._relay_controller.remove_device(device_node)
