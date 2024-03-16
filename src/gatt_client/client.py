# BASED ON https://github.com/hbldh/bleak/blob/develop/examples/philips_hue.py

import sys
import argparse
import asyncio
from uuid import UUID
from bleak import BleakClient


class CustomArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(
            *args,
            description="GATT sample client.",
            formatter_class=argparse.RawTextHelpFormatter,
            **kwargs)
        self.add_argument(
            "--address",
            "-a",
            type=str,
            default=None,
            help="MAC address of target device\nDefault: None")
        self.add_argument(
            "--characteristic",
            "-c",
            type=UUID,
            default=None,
            help="Target GATT characteristic.\nDefault: disabled")
        self.add_argument(
            "value",
            type=str,
            help="Value to be written to the target characteristic. The value will be passed as an UTF-8 string")


class Arguments:

    def __init__(
        self,
        address: str,
        characteristic: UUID,
        value: str,
    ) -> None:
        self._address = address
        self._characteristic = characteristic
        self._value = value

    @property
    def address(self) -> str:
        return self._address

    @property
    def characteristic(self) -> UUID:
        return self._characteristic

    @property
    def value(self) -> str:
        return self._value


def parse_args() -> Arguments:
    x = sys.argv
    parser = CustomArgumentParser()
    args = parser.parse_args()

    # Check if no arguments were provided
    if len(sys.argv) == 1:
        sys.exit(1)

    return Arguments(
        address=args.address,
        characteristic=args.characteristic,
        value=args.value)


async def main(address: str, characteristic: UUID, value: str):
    async with BleakClient(address) as client:
        print(f"Connected to {address}: {client.is_connected}")

        paired = await client.pair(protection_level=2)
        print(f"Paired: {paired}")

        print(f"Writing value '{value}'")
        await client.write_gatt_char(characteristic, value.encode(encoding = 'UTF-8', errors = 'strict'), response=False)
        print(f"Value '{value}' written to characteristic '{characteristic}'")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.address, args.characteristic, args.value))