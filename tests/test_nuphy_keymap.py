"""Tests for nuphy-keymap that need neither a keyboard nor Qt.

Run them with:  python3 -m unittest discover -s tests
"""
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(tempfile.mkdtemp(prefix="nuphy-keymap-test-"))
os.environ["NUPHY_KEYMAP_HOME"] = str(DATA)  # set before loading: the tool reads it at import


def load_tool():
    loader = importlib.machinery.SourceFileLoader("nuphy_keymap", str(ROOT / "bin" / "nuphy-keymap"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    sys.modules[loader.name] = module  # dataclasses look the module up while it loads
    loader.exec_module(module)
    return module


nk = load_tool()

# A toy board in VIA's definition format: Q W E and a 2u Shift on top; Ctrl, two
# modifiers and Fn below. Like NuPhy's boards, layers 0-2 are the Mac set and 3-5
# the Windows set; the switch on the keyboard's side picks one.
DEFINITION = {
    "name": "Toy Board",
    "vendorId": "0x19F5",
    "productId": "0x3255",
    "matrix": {"rows": 2, "cols": 4},
    "layouts": {"keymap": [
        ["0,0", "0,1", "0,2", {"w": 2}, "0,3"],
        [{"w": 1.25}, "1,0", "1,1", "1,2", {"w": 1.75}, "1,3"],
    ]},
    "customKeycodes": [{"name": "Link\nBLE_1", "title": "Link BLE_1"}],
}
T = "KC_TRNS"
LAYERS = [
    ["KC_Q", "KC_W", "KC_E", "KC_LSFT", "KC_LCTL", "KC_LALT", "KC_LGUI", "MO(1)"],  # Mac
    ["CUSTOM(0)", "KC_VOLU", T, "MO(2)", T, T, T, T],                              # Mac Fn
    ["KC_F1", T, T, T, T, T, T, T],                                                # Mac Fn + Shift
    ["KC_Q", "KC_W", "KC_E", "KC_LSFT", "KC_LCTL", "KC_LGUI", "KC_LALT", "MO(4)"],  # Windows
    ["CUSTOM(0)", "KC_MUTE", T, "MO(5)", T, T, T, T],                              # Windows Fn
    ["KC_F1", T, T, T, T, T, T, T],                                                # Windows Fn + Shift
    [T] * 8,                                                                       # unused
]


def make_board():
    path = DATA / "via" / "definition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFINITION))
    return nk.load_board(path)


def tearDownModule():
    shutil.rmtree(DATA, ignore_errors=True)


class Layout(unittest.TestCase):
    def test_keys_are_placed_like_kle(self):
        board = make_board()
        keys = {k.idx: (k.x, k.y, k.w) for k in board.keys}
        self.assertEqual(keys[3], (3.0, 0.0, 2))  # the 2u Shift ends the top row
        self.assertEqual(keys[4], (0.0, 1.0, 1.25))  # matrix row 1, col 0 -> index 4
        self.assertEqual(keys[7], (3.25, 1.0, 1.75))
        self.assertEqual((board.width, board.height), (5, 2))


class Keycodes(unittest.TestCase):
    def check(self, protocol, cases):
        decode = nk.keycode_decoder(protocol)
        for code, name in cases.items():
            with self.subTest(code=f"0x{code:04x}"):
                self.assertEqual(decode(code), name)

    def test_via_protocol_12(self):
        self.check(12, {
            0x0004: "KC_A", 0x0001: "KC_TRNS", 0x006E: "KC_F19", 0x00D9: "KC_MS_WH_UP",
            0x5224: "MO(4)", 0x4405: "LT(4,KC_B)", 0x7E03: "CUSTOM(3)", 0x7826: "RGB_SAD",
            0x7700: "MACRO(0)", 0x0A0E: "LSFT(LGUI(KC_K))", 0x7FFF: "0x7fff",
        })

    def test_via_protocol_11(self):
        # QMK 0.19's numbering, like 12, but with macros and custom keycodes higher up
        self.check(11, {0x5221: "MO(1)", 0x2204: "MT(MOD_LSFT,KC_A)", 0x00CD: "KC_MS_UP",
                        0x7820: "RGB_TOG", 0x7702: "MACRO(0)", 0x7F00: "CUSTOM(0)"})

    def test_via_protocol_10(self):
        self.check(10, {0x5101: "MO(1)", 0x00F0: "KC_MS_UP", 0x5F80: "CUSTOM(0)", 0x5CC2: "RGB_TOG"})


class Layers(unittest.TestCase):
    def setUp(self):
        self.board = make_board()
        self.modes = {m.name: m for m in nk.find_modes(self.board, LAYERS)}

    def test_base_layers_are_told_apart_by_the_bottom_row(self):
        self.assertEqual(set(self.modes), {"Mac", "Windows"})
        self.assertEqual([i.index for i in self.modes["Windows"].layers], [3, 4, 5])

    def test_layers_are_named_after_the_keys_you_hold(self):
        layers = self.modes["Windows"].layers
        self.assertEqual([i.name for i in layers], ["Base", "Fn", "Fn + Shift"])
        self.assertEqual(sorted(layers[2].held), [3, 7])  # Shift and Fn

    def test_transparent_keys_fall_through(self):
        self.assertEqual(nk.effective(LAYERS, [5, 4, 3], 1), "KC_MUTE")
        self.assertEqual(nk.effective(LAYERS, [5, 4, 3], 2), "KC_E")

    def test_overview_puts_every_layer_on_the_keycap(self):
        km = nk.Keymap(LAYERS, "export", None, dt.datetime.now())
        overview = nk.build_views(self.board, km, self.modes["Windows"])[0]
        q = next(k for k in overview["keys"] if k["i"] == 0)
        self.assertEqual([(lg["v"][0], lg["role"]) for lg in q["legends"]],
                         [("Q", "base"), ("BT 1", "l1"), ("F1", "l2")])


class Labels(unittest.TestCase):
    def setUp(self):
        self.board = make_board()

    def test_nuphy_keys_get_their_everyday_names(self):
        self.assertEqual(nk.describe("CUSTOM(0)", self.board).variants[0], "BT 1")

    def test_shortcuts_are_spelled_out(self):
        self.assertIn("Meta+Shift+K", nk.describe("LSFT(LGUI(KC_K))", self.board).variants)


class Saving(unittest.TestCase):
    def test_a_snapshot_is_written_once_then_only_confirmed(self):
        board = make_board()
        first, changed = nk.save_snapshot(board, LAYERS, 12)
        self.assertTrue(changed)
        self.assertEqual(nk.save_snapshot(board, LAYERS, 12), (first, False))
        self.assertEqual(next(nk.saved_keymaps(board)).layers, LAYERS)

    def test_web_page_and_artifact_fragment(self):
        board = make_board()
        km = nk.Keymap(LAYERS, "export", None, dt.datetime(2026, 7, 30, 8, 22))
        model = nk.build_model(board, km, [])
        page, fragment = DATA / "page.html", DATA / "fragment.html"
        nk.write_html(model, page, fragment=False)
        nk.write_html(model, fragment, fragment=True)
        self.assertTrue(page.read_text().startswith("<!doctype html>"))
        self.assertTrue(fragment.read_text().startswith("<title>"))
        self.assertIn('"name":"Toy Board"', fragment.read_text())


class Errors(unittest.TestCase):
    def test_a_missing_file_is_named_not_blamed_on_usb(self):
        make_board()
        missing = DATA / "missing.layout.json"
        with self.assertRaises(SystemExit) as caught:
            nk.main(["verify", str(missing)])
        self.assertEqual(caught.exception.code, f"Can't find {missing}.")


if __name__ == "__main__":
    unittest.main()
