import sys
import asyncio
from uuid import UUID

from bleak import BleakClient


ADDRESS = "B8:27:EB:9C:F6:4C"
HID_INPUT_SERVICE = UUID("00000000-6907-4437-8539-9218a9d54e29")
HID_INPUT_CHARACTERISTIC = UUID("00000001-6907-4437-8539-9218a9d54e29")

async def main(address: str, characteristic: UUID, value: str):
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}")

        # paired = await client.pair(protection_level=2)
        # print(f"Paired: {paired}")

        service = client.services.get_service(HID_INPUT_SERVICE)
        char_obj = service.get_characteristic(HID_INPUT_CHARACTERISTIC)
        print(f"Writing value {value}")
        await client.write_gatt_char(char_obj, value.encode(encoding = 'UTF-8', errors = 'strict'), response=False)
        print(f"Value {value} written to characteristic {characteristic}")

if __name__ == "__main__":
    asyncio.run(main(ADDRESS, HID_INPUT_CHARACTERISTIC, "Win"))