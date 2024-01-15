import re
from typing import Dict
from adafruit_hid.keycode import Keycode

class ParsedShortcut:
    def __init__(self, keycodes: list[int], description: str):
        self.keycodes = keycodes
        self.description = description

    def __str__(self) -> str:
        return self.description

    def __repr__(self) -> str:
        return self.description

class ShortcutParser:
    _command_split_regex = re.compile("[,;+']|\\s")
    _key_codes: Dict = dict[str, int]()
    _key_names: Dict = dict[int, str]()

    def __init__(self) -> None:
        for field in dir(Keycode):
            if (not field.startswith("__")):
                field_value = getattr(Keycode, field)
                if (isinstance(field_value, int)):
                    self._key_codes[field] = field_value
                    self._key_names[field_value] = field

    def parse(self, value: str) -> list[ParsedShortcut]:
        shortcuts = []

        for shortcut_candidate in filter(None, map(str.strip, self._command_split_regex.split(value))):
            keycodes = []
            keynames = []
            for key_candidate in filter(None, map(str.upper, map(str.strip, shortcut_candidate.split("-")))):
                if (key_candidate in self._key_codes):
                    keycode = self._key_codes[key_candidate]
                    keycodes.append(keycode)
                    keynames.append(self._key_names[keycode])
            if len(keycodes) > 0:
                shortcut = ParsedShortcut(keycodes, "-".join(keynames))
                shortcuts.append(shortcut)

        return shortcuts