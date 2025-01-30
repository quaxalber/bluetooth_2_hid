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

shutdown_event = asyncio.Event()


def signal_handler(sig, frame):
    """
    Signal handler that triggers graceful shutdown.
    """
    sig_name = signal.Signals(sig).name
    logger.debug(f"Received signal: {sig_name}. Requesting graceful shutdown.")
    shutdown_event.set()


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(sig, signal_handler)


async def main() -> None:
    """
    Entry point for Bluetooth 2 USB.

    This function:

    1. Parses command-line arguments.
    2. Sets up logging.
    3. Configures and enables USB HID gadgets.
    4. Initiates asynchronous monitoring of input devices (udev) and USB state (UDC).
    5. Manages overall relay activation via two conditions:
       - At least one input device is present.
       - The USB gadget is “configured” by the host.
    6. Waits for a shutdown signal and then gracefully stops.
    """
    args = parse_args()

    if args.debug:
        logger.setLevel(DEBUG)

    if args.version:
        print_version()

    if args.list_devices:
        await async_list_devices()

    _setup_logging(args)
    logger.info(f"Launching {VERSIONED_NAME}")

    # We'll coordinate relaying via three Events:
    #   - devices_found_event: set when at least one device is active
    #   - udc_configured_event: set when /sys/class/udc/... says "configured"
    #   - relaying_active: combined event that is set only if both the above are set
    devices_found_event = asyncio.Event()
    udc_configured_event = asyncio.Event()
    relaying_active = asyncio.Event()

    def update_relaying():
        """
        Sets or clears `relaying_active` based on the states of
        `devices_found_event` and `udc_configured_event`.
        """
        if devices_found_event.is_set() and udc_configured_event.is_set():
            relaying_active.set()
        else:
            relaying_active.clear()

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
        devices_found_event=devices_found_event,
        update_relaying_callback=update_relaying,
        shortcut_toggler=shortcut_toggler,
    )

    udc_path = get_udc_path()
    if udc_path is None:
        logger.error("No UDC detected! USB Gadget mode may not be enabled.")
        return

    logger.debug(f"Detected UDC state file: {udc_path}")

    # Create tasks within a single TaskGroup, then simultaneously monitor:
    #   - Udev events for input add/remove
    #   - UDC state transitions
    #   - The actual device-relay tasks
    async with (
        asyncio.TaskGroup() as task_group,
        UdevEventMonitor(relay_controller),
        UdcStateMonitor(
            udc_configured_event=udc_configured_event,
            update_relaying_callback=update_relaying,
            udc_path=udc_path,
        ),
    ):
        relay_controller.start_relaying(task_group)

        await shutdown_event.wait()
        logger.debug("Shutdown event triggered. Letting TaskGroup exit...")

    logger.debug("Main function exit. All tasks cancelled or completed.")


async def async_list_devices():
    """
    Prints a list of available input devices and exits.

    :raises SystemExit: Always exits after listing devices.
    """
    all_devices = await async_list_input_devices()
    for dev in all_devices:
        descriptor = dev.uniq if dev.uniq else dev.phys
        print(f"{dev.name}\t{descriptor}\t{dev.path}")
    exit_safely()


def print_version():
    """
    Prints the version of Bluetooth 2 USB and exits.

    :raises SystemExit: Always exits after printing version.
    """
    print(VERSIONED_NAME)
    exit_safely()


def exit_safely():
    """
    Unregisters usb_hid.disable() from atexit and exits the script.

    This avoids an exception if the script is already running
    (e.g., as a systemd service) and usb_hid.disable() is called again.
    """
    atexit.unregister(usb_hid.disable)
    sys.exit(0)


def _setup_logging(args):
    """
    Sets up logging destinations based on CLI arguments.

    :param args: Parsed CLI arguments.
    :type args: argparse.Namespace
    """
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


def validate_shortcut(shortcut: list[str]) -> set[str]:
    """
    Converts a list of raw key strings (e.g. ["SHIFT", "CTRL", "Q"]) into
    a set of valid evdev-style names (e.g. {"KEY_LEFTSHIFT", "KEY_LEFTCTRL", "KEY_Q"}).

    :param shortcut: List of shortcut key strings.
    :type shortcut: list[str]
    :return: A set of normalized evdev key names, or empty set if none were valid.
    :rtype: set[str]
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
        key_name = (
            key_upper
            if key_upper.startswith("KEY_") or key_upper.startswith("BTN_")
            else f"KEY_{key_upper}"
        )
        valid_keys.add(key_name)

    return valid_keys


def get_udc_path() -> Path | None:
    """
    Dynamically finds the UDC state file for the USB Device Controller,
    returning the full path to the "state" file or None if no UDC is found.

    :return: Path to the UDC state file, or None if not found.
    :rtype: pathlib.Path | None
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
    CLI entry point for the script.
    """
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Unhandled exception encountered. Aborting.")
        sys.exit(1)
