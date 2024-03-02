# HACK: the main module code import some modules that are not available on windows and are not needed for tests.
# the modules are not inclided into requirements.client.txt list and we ldo ignore them on import
# BASED ON: https://stackoverflow.com/a/6077117/318263
import builtins
import types
from types import ModuleType
from typing import Mapping, Sequence

class DummyModule(ModuleType):
    def __getattr__(self, key):
        return None
    __all__ = []   # support wildcard imports

_bad_modules = { 'evdev' }

def tryimport(
    name: str,
    globals: Mapping[str, object] | None = None,
    locals: Mapping[str, object] | None = None,
    fromlist: Sequence[str] = (),
    level: int = 0,
) -> types.ModuleType:
    try:
        return realimport(name, globals, locals, fromlist, level)
    except ImportError:
        if (name in _bad_modules):
            return DummyModule(name)
        raise

realimport, builtins.__import__ = builtins.__import__, tryimport

import sys
import os
sys.path.append(os.path.abspath('.'))

import unittest
from parameterized import parameterized
from adafruit_hid.keycode import Keycode
from src.bluetooth_2_usb.shortcut_parser import ShortcutParser, ParsedShortcut

class TestShortcutParser(unittest.TestCase):

    @parameterized.expand([
            ('A', [Keycode.A], "A"),
            ('CONTROL-C', [Keycode.CONTROL, Keycode.C], "Ctrl-C"),
            ('CONTROL+C', [Keycode.CONTROL, Keycode.C], "Ctrl-C"),
            ('GUI-Two', [Keycode.GUI, Keycode.TWO], "Win-2"),
            ('control-+-ins', [Keycode.CONTROL, Keycode.INSERT], "Ctrl-Ins"),
            ('meta-shift-pause', [Keycode.WINDOWS, Keycode.SHIFT, Keycode.PAUSE], "Win-Shift-Break"),
            ('CONTROL+ALT+DELETE', [Keycode.CONTROL, Keycode.ALT, Keycode.DELETE], "Ctrl-Alt-Del"),
            ('CTRL+ALT+DEL', [Keycode.CONTROL, Keycode.ALT, Keycode.DELETE], "Ctrl-Alt-Del")
        ])
    def test_parse_shortcut(self, input: str, expected_keycodes: list[int], expected_description: str):
        parser = ShortcutParser()
        shortcut = parser.parse_shortcut(input)
        self.assertEqual(shortcut.keycodes, expected_keycodes)
        self.assertEqual(shortcut.description, expected_description)


    @parameterized.expand([
            ('A', [[Keycode.A]], ["A"]),
            ('A B', [[Keycode.A], [Keycode.B]], ["A", "B"]),
            ('A\tB', [[Keycode.A], [Keycode.B]], ["A", "B"]),
            ('A;B', [[Keycode.A], [Keycode.B]], ["A", "B"]),
            ('A,B', [[Keycode.A], [Keycode.B]], ["A", "B"]),
            ('control-ins; ;Del', [[Keycode.CONTROL, Keycode.INSERT], [Keycode.DELETE]], ["Ctrl-Ins", "Del"]),
            ('H  I  Shift-ONE', [[Keycode.H], [Keycode.I], [Keycode.SHIFT, Keycode.ONE]], ["H", "I", "Shift-1"]),
            ('Ctrl+A,;,Delete', [[Keycode.CONTROL, Keycode.A], [Keycode.DELETE]], ["Ctrl-A", "Del"])
        ])
    def test_parse_command(self, input: str, expected_keycodes: list[list[int]], expected_description: list[str]):
        parser = ShortcutParser()
        shortcuts = parser.parse_command(input)
        for i in range(0, len(shortcuts)):
            self.assertEqual(shortcuts[i].keycodes, expected_keycodes[i])
            self.assertEqual(shortcuts[i].description, expected_description[i])


    @parameterized.expand([
            ('WWW', None, None),
            ('A+B=C', [Keycode.A], "A"),
            ('control-aaaa', [Keycode.CONTROL], "Ctrl"),
            ('me-me-me-2', [Keycode.TWO], "2")
        ])
    def test_parse_bad_shortcut_silent(self, input: str, expected_keycodes: list[int], expected_description: str):
        parser = ShortcutParser()
        shortcut = parser.parse_shortcut(input, raise_error=False)

        if (expected_keycodes is None):
            self.assertIsNone(shortcut)
        else:
            self.assertEqual(shortcut.keycodes, expected_keycodes)

        if (expected_description is None):
            self.assertIsNone(shortcut)
        else:
            self.assertEqual(shortcut.description, expected_description)


    @parameterized.expand([
            ('WWW'),
            ('A+B=C'),
            ('control-aaaa'),
            ('me-me-me-2')
        ])
    def test_parse_bad_shortcut_fails(self, input: str):
        parser = ShortcutParser()
        self.assertRaises(ValueError, parser.parse_shortcut, input, True)


    @parameterized.expand([
            ('AA', [], []),
            ('control=ins; Del', [[Keycode.DELETE]], ["Del"]),
            ('H: I Shift-ONE', [[Keycode.I], [Keycode.SHIFT, Keycode.ONE]], ["I", "Shift-1"]),
            ('Ctrl+Aa,Delet', [[Keycode.CONTROL]], ["Ctrl"])
        ])
    def test_parse_bad_command_silent(self, input: str, expected_keycodes: list[list[int]], expected_description: list[str]):
        parser = ShortcutParser()
        shortcuts = parser.parse_command(input, raise_error=False)
        for i in range(0, len(shortcuts)):
            self.assertEqual(shortcuts[i].keycodes, expected_keycodes[i])
            self.assertEqual(shortcuts[i].description, expected_description[i])


    @parameterized.expand([
            ('AA'),
            ('control=ins; Del'),
            ('H: I Shift-ONE'),
            ('Ctrl+Aa,Delet')
        ])
    def test_parse_bad_command_fails(self, input: str):
        parser = ShortcutParser()
        self.assertRaises(ValueError, parser.parse_command, input, True)

if __name__ == '__main__':
    unittest.main()