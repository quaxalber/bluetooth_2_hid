import asyncio
import atexit
from logging import DEBUG
import signal
import sys

import usb_hid

from src.bluetooth_2_usb.args import parse_args
from src.bluetooth_2_usb.logging import add_file_handler, get_logger
from src.bluetooth_2_usb.relay import (
    RelayController,
    UdevEventMonitor,
    UsbHidManager,
    async_list_input_devices,
)


logger = get_logger()
VERSION = "0.8.3"
VERSIONED_NAME = f"Bluetooth 2 USB v{VERSION}"


def signal_handler(sig, frame):
    sig_name = signal.Signals(sig).name
    logger.info(f"Received signal: {sig_name}. Requesting graceful shutdown.")
    # Raising KeyboardInterrupt stops asyncio.run(main()) gracefully,
    # triggering the exception block where we can do cleanup if needed.
    raise KeyboardInterrupt


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(sig, signal_handler)


async def main() -> None:
    """
    Parses command-line arguments, sets up logging and starts the event loop which
    reads events from the input devices and forwards them to the corresponding USB
    gadget device.

    Returns:
        None: The function runs indefinitely unless a signal or exception forces
              an exit.
    """
    args = parse_args()

    # Debug-level logging if requested
    if args.debug:
        logger.setLevel(DEBUG)

    # Show version and exit, if requested
    if args.version:
        print_version()

    # List devices and exit, if requested
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

    usb_manager = UsbHidManager()
    usb_manager.enable_devices()

    relay_controller = RelayController(
        usb_manager=usb_manager,
        device_identifiers=args.device_ids,
        auto_discover=args.auto_discover,
        grab_devices=args.grab_devices,
    )

    event_loop = asyncio.get_event_loop()

    # UdevEventMonitor listens for device changes and notifies the relay_controller.
    with UdevEventMonitor(relay_controller, event_loop):
        # Forward events indefinitely or until a KeyboardInterrupt/signal stops us
        await relay_controller.async_relay_devices()


async def async_list_devices():
    """
    Prints a list of available input devices. This is a helper function for
    the --list-devices CLI argument.
    """
    for dev in await async_list_input_devices():
        print(f"{dev.name}\t{dev.uniq if dev.uniq else dev.phys}\t{dev.path}")
    exit_safely()


def print_version():
    """
    Prints the version of Bluetooth 2 USB and exits.
    """
    print(VERSIONED_NAME)
    exit_safely()


def exit_safely():
    """
    When the script is run with help or version flag, we need to unregister usb_hid.disable() from atexit
    because else an exception occurs if the script is already running, e.g. as service.
    """
    atexit.unregister(usb_hid.disable)
    sys.exit(0)


if __name__ == "__main__":
    """
    Entry point for the script.
    We catch KeyboardInterrupt gracefully to perform any needed cleanup.
    """
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown requested. Exiting.")
    except Exception:
        logger.exception("Unhandled exception encountered. Aborting mission.")
        raise
