import asyncio
import atexit
from logging import DEBUG
from pathlib import Path
import signal
import sys

import usb_hid

from src.bluetooth_2_usb.args import parse_args
from src.bluetooth_2_usb.logging import add_file_handler, get_logger
from src.bluetooth_2_usb.relay import (
    GadgetManager,
    RelayController,
    ShortcutToggler,
    UdcStateMonitor,
    UdevEventMonitor,
    async_list_input_devices,
)

logger = get_logger()
VERSION = "0.9.0"
VERSIONED_NAME = f"Bluetooth 2 USB v{VERSION}"


async def main() -> None:
    """
    Entrypoint that:

    - Parses command-line arguments.
    - Sets up logging.
    - Enables USB HID gadgets.
    - Configures a global ``relaying_active`` event that is
      toggled by ``UdcStateMonitor`` when the UDC is configured.
    - Creates a TaskGroup to manage device-relay tasks.
    - Registers monitors for udev and UDC state changes.
    """
    args = parse_args()

    if args.debug:
        logger.setLevel(DEBUG)

    if args.version:
        print_version()

    if args.list_devices:
        await async_list_devices()

    log_handlers_message = "Logging to stdout"
    if args.log_to_file:
        try:
            add_file_handler(args.log_path)
        except OSError as e:
            logger.error(f"Could not open log file '{args.log_path}' for writing: {e}")
            sys.exit(1)
        log_handlers_message += f" and to {args.log_path}"

    logger.debug(f"CLI args: {args}")
    logger.debug(log_handlers_message)
    logger.info(f"Launching {VERSIONED_NAME}")

    # This event is only set once the UDC is "configured" (via UdcStateMonitor)
    relaying_active = asyncio.Event()

    gadget_manager = GadgetManager()
    gadget_manager.enable_gadgets()

    shortcut_toggler = None
    if args.interrupt_shortcut:
        shortcut_keys = validate_shortcut(args.interrupt_shortcut)
        if shortcut_keys:
            logger.debug(f"Configuring global interrupt shortcut: {shortcut_keys}")
            shortcut_toggler = ShortcutToggler(
                shortcut_keys=shortcut_keys,
                relaying_active=relaying_active,
                gadget_manager=gadget_manager,
            )

    relay_controller = RelayController(
        gadget_manager=gadget_manager,
        device_identifiers=args.device_ids,
        auto_discover=args.auto_discover,
        grab_devices=args.grab_devices,
        relaying_active=relaying_active,
        shortcut_toggler=shortcut_toggler,
    )

    udc_path = get_udc_path()
    if udc_path is None:
        logger.error("No UDC detected! USB Gadget mode may not be enabled.")
        return

    logger.debug(f"Detected UDC state file: {udc_path}")

    try:
        async with asyncio.TaskGroup() as tg:
            relay_controller.bind_task_group(tg)

            async with (
                UdevEventMonitor(relay_controller) as udev_monitor,
                UdcStateMonitor(tg, relaying_active, udc_path=udc_path) as udc_monitor,
            ):
                await relay_controller.load_initial_devices()

    except Exception as exc:
        logger.exception(
            "Unhandled exception encountered. Aborting mission.", exc_info=exc
        )
        sys.exit(1)


async def async_list_devices():
    """
    Prints a list of available input devices for the ``--list-devices`` CLI argument.

    :return: None. Prints and exits.
    """
    devices = await async_list_input_devices()
    for dev in devices:
        uniq_or_phys = dev.uniq if dev.uniq else dev.phys
        print(f"{dev.name}\t{uniq_or_phys}\t{dev.path}")
    exit_safely()


def print_version():
    """
    Prints the version of Bluetooth 2 USB.

    :return: None. Prints and exits.
    """
    print(VERSIONED_NAME)
    exit_safely()


def exit_safely():
    """
    Unregisters the ``usb_hid.disable()`` atexit handler and exits,
    to avoid errors if the script is invoked while already running (e.g. as a service).

    :return: None. Exits the Python process.
    """
    atexit.unregister(usb_hid.disable)
    sys.exit(0)


def validate_shortcut(shortcut: list[str]) -> set[str]:
    """
    Converts a list of raw key strings (e.g. ``["SHIFT", "CTRL", "Q"]``)
    into a set of valid evdev-style names (e.g. ``{"KEY_LEFTSHIFT", "KEY_LEFTCTRL", "KEY_Q"}``).

    - Uppercases each entry.
    - Maps certain known aliases (e.g. SHIFT -> LEFTSHIFT).
    - Ensures the final string starts with ``KEY_``.

    :param shortcut: List of string key names.
    :return: A set of normalized key names suitable for evdev -> USB HID mapping.
    """
    ALIAS_MAP = {
        "SHIFT": "LEFTSHIFT",
        "LSHIFT": "LEFTSHIFT",
        "RSHIFT": "RIGHTSHIFT",
        "CTRL": "LEFTCTRL",
        "LCTRL": "LEFTCTRL",
        "RCTRL": "RIGHTCTRL",
        "ALT": "LEFTALT",
        "LALT": "LEFTALT",
        "RALT": "RIGHTALT",
        "GUI": "LEFTMETA",
        "LMETA": "LEFTMETA",
        "RMETA": "RIGHTMETA",
    }

    valid_keys = set()
    for raw_key in shortcut:
        key_upper = raw_key.strip().upper()
        if key_upper in ALIAS_MAP:
            key_upper = ALIAS_MAP[key_upper]
        key_name = key_upper if key_upper.startswith("KEY_") else f"KEY_{key_upper}"
        valid_keys.add(key_name)

    return valid_keys


def get_udc_path() -> Path | None:
    """
    Dynamically finds the UDC state file for the USB Device Controller.
    Returns the full path to the ``state`` file or None if no UDC is found.

    :return: Path to the UDC's ``state`` file, or None if unavailable.
    """
    udc_root = Path("/sys/class/udc")

    if not udc_root.exists() or not udc_root.is_dir():
        return None

    controllers = [entry for entry in udc_root.iterdir() if entry.is_dir()]
    if not controllers:
        return None

    return controllers[0] / "state"


if __name__ == "__main__":
    """
    Entry point for the script when run directly.
    """
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Unhandled exception during startup.")
        sys.exit(1)
