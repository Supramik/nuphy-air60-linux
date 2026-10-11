"""Tests for nuphy-keymap that need neither a keyboard nor Qt.

Run them with:  python3 -m unittest discover -s tests
"""
import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


def clear_saved():
    """No snapshots and no VIA exports, just the definition."""
    shutil.rmtree(nk.SNAPSHOT_DIR, ignore_errors=True)
    for path in nk.VIA_DIR.glob("*.json"):
        if path != nk.DEFINITION_FILE:
            path.unlink()


def write_layout(name, layers, macros=None, age=0.0, folder=None):
    """A VIA layout file, last modified `age` seconds ago."""
    path = (folder or nk.SNAPSHOT_DIR) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"name": "Toy Board", "vendorProductId": 0x19F53255, "layers": layers}
    if macros is not None:
        data["macros"] = macros
    path.write_text(json.dumps(data))
    os.utime(path, (time.time() - age,) * 2)
    return path


def codes_of(layers):
    encode = nk.keycode_encoder(12)
    return [[encode(name) for name in layer] for layer in layers]


class FakeKeyboard:
    """The keyboard's side of the VIA protocol, in memory, in place of a ViaLink."""

    def __init__(self, codes, protocol=12, macro_count=4, macro_size=64, macros=b""):
        self.protocol, self.layer_count, self.macro_count = protocol, len(codes), macro_count
        self.keymap = bytearray(b"".join(c.to_bytes(2, "big") for layer in codes for c in layer))
        self.macros = bytearray(macros.ljust(macro_size, b"\0"))
        self.requests = []  # command bytes, in order

    def codes(self):
        cols = DEFINITION["matrix"]["cols"]
        per_layer = DEFINITION["matrix"]["rows"] * cols
        values = [int.from_bytes(self.keymap[i:i + 2], "big") for i in range(0, len(self.keymap), 2)]
        return [values[i:i + per_layer] for i in range(0, len(values), per_layer)]

    def writes(self):
        return [r for r in self.requests if r in (0x05, 0x0F, 0x10)]

    def ask(self, *request, echo=1, timeout=0.8):
        msg = bytes(request).ljust(32, b"\0")
        cmd, offset, n = msg[0], msg[1] << 8 | msg[2], msg[3]
        reply = bytearray(msg)
        self.requests.append(cmd)
        if cmd == 0x01:  # protocol version
            reply[1:3] = self.protocol.to_bytes(2, "big")
        elif cmd == 0x11:  # layer count
            reply[1] = self.layer_count
        elif cmd == 0x12:  # read keymap
            reply[4:4 + n] = self.keymap[offset:offset + n].ljust(n, b"\0")
        elif cmd == 0x05:  # set one key
            i = 2 * ((msg[1] * DEFINITION["matrix"]["rows"] + msg[2]) * DEFINITION["matrix"]["cols"] + msg[3])
            self.keymap[i:i + 2] = msg[4:6]
        elif cmd == 0x0C:  # macro count
            reply[1] = self.macro_count
        elif cmd == 0x0D:  # macro memory size
            reply[1:3] = len(self.macros).to_bytes(2, "big")
        elif cmd == 0x0E:  # read macros
            reply[4:4 + n] = self.macros[offset:offset + n].ljust(n, b"\0")
        elif cmd == 0x0F:  # write macros
            n = min(n, len(self.macros) - offset)
            self.macros[offset:offset + n] = msg[4:4 + n]
        elif cmd == 0x10:  # clear macros
            self.macros[:] = bytes(len(self.macros))
        else:
            raise RuntimeError(f"the keyboard doesn't support VIA command 0x{cmd:02x}")
        return bytes(reply)


def use_keyboard(test, keyboard):
    """Make nuphy-keymap talk to `keyboard` for the rest of the test."""
    original = nk.via_session
    nk.via_session = lambda board: contextlib.nullcontext(keyboard)
    test.addCleanup(setattr, nk, "via_session", original)


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

    def test_names_are_the_ones_vias_load_knows(self):
        # VIA's Load Saved Layout empties any key whose name it doesn't know
        self.check(12, {0x00B6: "KC_WWW_BACK", 0x007D: "KC_PASTE", 0x7C00: "RESET", 0x7C16: "KC_GESC",
                        0x7803: "BL_DEC", 0x0088: "KC_KANA", 0x7842: "RM_TOGG", 0x777F: "MACRO(127)",
                        0x7EFF: "CUSTOM(255)"})
        self.check(11, {0x7711: "MACRO(15)", 0x7700: "FN_MO13", 0x7842: "0x7842", 0x7712: "0x7712"})
        # modifiers around nothing VIA can name come out as plain hex, not as another key
        self.check(12, {0x1004: "0x1004", 0x2004: "0x2004", 0x52A0: "0x52a0", 0x02FF: "0x02ff"})

    def test_every_keycode_comes_back_from_its_name(self):
        for protocol in (11, 12):
            decode, encode = nk.keycode_decoder(protocol), nk.keycode_encoder(protocol)
            lost = [f"0x{c:04x}" for c in range(0x10000) if encode(decode(c)) != c]
            self.assertEqual(lost, [], f"VIA protocol {protocol}")

    def test_restore_reads_vias_own_spellings(self):
        encode = nk.keycode_encoder(12)
        cases = {"S(KC_1)": 0x021E, "LSG(KC_K)": 0x0A0E, "MT(MOD_LCTL | MOD_LSFT,KC_A)": 0x2304,
                 "MT(MOD_LSFT | MOD_RSFT,KC_A)": 0x3204, "LM(2,MOD_LALT)": 0x5044, "OSM(MOD_RGUI)": 0x52B8,
                 "QK_BOOT": 0x7C00, "KC_WBAK": 0x00B6, "KC_TRANSPARENT": 0x0001, "0x7fff": 0x7FFF}
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertEqual(encode(name), code)
        for name in ("KC_BOGUS", "MO(32)", "LT(1,RGB_TOG)", "MT(MOD_NOPE,KC_A)", "MACRO(128)"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                encode(name)
        with self.assertRaises(ValueError):
            nk.keycode_encoder(10)  # older numbering: restore leaves that to VIA


class Macros(unittest.TestCase):
    # (bytes the way VIA writes them, VIA's text for them), protocol 11 and up
    CASES = [
        (b"Hi\x01\x01\x28", "Hi{KC_ENT}"),
        (b"\x01\x02\xe0\x01\x02\x06\x01\x03\x06\x01\x03\xe0", "{KC_LCTL,KC_C}"),
        (b"\x01\x04250|", "{250}"),
        (b"\x01\x02\xe1x\x01\x03\xe1", "{+KC_LSFT}x{-KC_LSFT}"),
        (b"a{b", "a\\{b"),
        (b"", ""),
    ]

    def test_text_and_bytes_match_vias(self):
        data = b"".join(raw + b"\0" for raw, _ in self.CASES)
        texts = [text for _, text in self.CASES]
        self.assertEqual(nk.decode_macros(data, len(texts), 12), texts)
        self.assertEqual(nk.encode_macros(texts, 12), data)

    def test_a_tap_inside_held_keys_is_a_chord_too(self):
        self.assertEqual(nk.decode_macros(b"\x01\x02\xe0\x01\x01\x06\x01\x03\xe0\0", 1, 12), ["{KC_LCTL,KC_C}"])

    def test_older_firmware_has_no_prefix(self):
        self.assertEqual(nk.decode_macros(b"\x01\x04ok\0", 1, 10), ["{KC_A}ok"])

    def test_partial_memory_asks_for_more(self):
        data = b"Hi\0\x01\x04100"
        self.assertIsNone(nk.decode_macros(data, 2, 12, final=False))
        self.assertEqual(nk.decode_macros(data, 2, 12, final=True), ["Hi"])

    def test_what_the_keyboard_cant_hold_is_refused(self):
        for text in ("{KC_BOGUS}", "{MO(1)}", "café☃"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                nk.encode_macros([text], 12)


class LiveRead(unittest.TestCase):
    def test_layers_and_macros_come_from_the_keyboard(self):
        board = make_board()
        keyboard = FakeKeyboard(codes_of(LAYERS), macros=b"Hi\x01\x01\x28\0\0\0\0")
        state = nk.read_state(keyboard, board)
        self.assertEqual(state.layers, LAYERS)
        self.assertEqual(state.macros, ["Hi{KC_ENT}", "", "", ""])
        self.assertEqual(keyboard.requests.count(0x0E), 1)  # it stops where the last macro ends
        self.assertEqual(keyboard.writes(), [])

    def test_macros_it_cant_get_dont_cost_the_keymap(self):
        for failure in (RuntimeError("the keyboard doesn't support VIA command 0x0e"),
                        TimeoutError("the keyboard didn't answer")):
            class NoMacros(FakeKeyboard):
                def ask(self, *request, **kw):
                    if request[0] == 0x0E:
                        raise failure
                    return super().ask(*request, **kw)
            with self.subTest(failure=failure):
                state = nk.read_state(NoMacros(codes_of(LAYERS)), make_board())
                self.assertEqual(state.layers, LAYERS)
                self.assertIsNone(state.macros)


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
    def setUp(self):
        clear_saved()

    def test_a_snapshot_is_written_once_then_only_confirmed(self):
        board = make_board()
        first, changed = nk.save_snapshot(board, LAYERS, 12)
        self.assertTrue(changed)
        self.assertEqual(nk.save_snapshot(board, LAYERS, 12), (first, False))
        self.assertEqual(next(nk.saved_keymaps(board)).layers, LAYERS)

    def test_snapshots_are_backups_vias_load_takes(self):
        board = make_board()
        first, _ = nk.save_snapshot(board, LAYERS, 12, ["", "", "", ""])
        second, changed = nk.save_snapshot(board, LAYERS, 12, ["Hi", "", "", ""])
        self.assertTrue(changed)  # a macro change alone is worth a backup
        self.assertNotEqual(first, second)  # even within the same second
        data = json.loads(second.read_text())
        # VIA's Load checks these three, and wants one macro per macro slot
        self.assertTrue(data["name"] and data["layers"] and data["vendorProductId"])
        self.assertEqual(data["macros"], ["Hi", "", "", ""])

    def test_unknown_macros_are_left_out_rather_than_guessed(self):
        path, _ = nk.save_snapshot(make_board(), LAYERS, 12, None)
        self.assertNotIn("macros", json.loads(path.read_text()))  # VIA's Load then skips them

    def test_old_spellings_are_not_a_change(self):
        board = make_board()
        old = [layer[:] for layer in LAYERS]
        old[6][0] = "KC_WBAK"
        nk.save_snapshot(board, old, 12)
        new = [layer[:] for layer in LAYERS]
        new[6][0] = "KC_WWW_BACK"
        self.assertFalse(nk.save_snapshot(board, new, 12)[1])

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


class Restore(unittest.TestCase):
    def setUp(self):
        self.board = make_board()
        clear_saved()
        reset = codes_of(LAYERS)
        for li in (1, 2, 4, 5):
            reset[li] = [1] * 8  # say a reset left the Fn layers transparent
        self.reset = reset
        self.keyboard = FakeKeyboard(reset)
        use_keyboard(self, self.keyboard)

    def restore(self, choice=None, yes=True):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nk.restore(self.board, choice, yes)
        return out.getvalue()

    def test_a_reset_keyboard_gets_its_keymap_back(self):
        macros = ["Hi{KC_ENT}", "", "", ""]
        write_layout("keymap-2026-10-01_120000.layout.json", LAYERS, macros, age=3600)
        out = self.restore()
        self.assertIn("8 keys and the macros", out)
        self.assertIn("BT 1", out)  # Fn + Q, by NuPhy's name for it
        self.assertEqual(self.keyboard.codes(), codes_of(LAYERS))
        self.assertEqual(nk.decode_macros(self.keyboard.macros, 4, 12), macros)
        self.assertEqual(self.keyboard.writes().count(0x05), 8)  # only the keys that differ
        after, before, _ = nk.saved_keymaps(self.board)
        self.assertEqual(after.layers, LAYERS)
        self.assertEqual(codes_of(before.layers), self.reset)  # so the restore can be undone too

    def test_by_default_the_newest_backup_that_differs(self):
        write_layout("keymap-2026-10-01_120000.layout.json", LAYERS, age=7200)
        reset_names = [[nk.keycode_decoder(12)(c) for c in layer] for layer in self.reset]
        write_layout("keymap-2026-10-02_120000.layout.json", reset_names, age=3600)
        self.restore()
        self.assertEqual(self.keyboard.codes(), codes_of(LAYERS))

    def test_a_via_export_with_vias_spellings(self):
        layers = [layer[:] for layer in LAYERS]
        layers[4][1] = "S(KC_1)"  # VIA writes LSFT(KC_1) like this
        export = write_layout("toy.layout.json", layers, ["", "", "", ""], folder=nk.VIA_DIR)
        self.restore(str(export))
        self.assertEqual(self.keyboard.codes()[4][1], 0x021E)

    def test_nothing_is_written_when_it_matches(self):
        reset_names = [[nk.keycode_decoder(12)(c) for c in layer] for layer in self.reset]
        write_layout("keymap-2026-10-01_120000.layout.json", reset_names)
        self.assertIn("already matches", self.restore("1"))
        self.assertIn("matches all your backups", self.restore())
        self.assertEqual(self.keyboard.writes(), [])

    def test_keycodes_it_cant_write_stop_it_before_anything_is_written(self):
        layers = [layer[:] for layer in LAYERS]
        layers[1][2] = "KC_BOGUS"
        write_layout("keymap-2026-10-01_120000.layout.json", layers)
        with self.assertRaises(SystemExit) as caught:
            self.restore()
        self.assertIn("KC_BOGUS (layer 1, key 2)", caught.exception.code)
        self.assertEqual(self.keyboard.writes(), [])

    def test_it_asks_first(self):
        write_layout("keymap-2026-10-01_120000.layout.json", LAYERS)
        with mock.patch("sys.stdin", io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.restore(yes=False)  # no terminal to ask in
        self.assertIn("--yes", caught.exception.code)
        tty = mock.Mock(isatty=lambda: True)
        with mock.patch("sys.stdin", tty), mock.patch("builtins.input", return_value="n"):
            self.assertIn("Nothing written", self.restore(yes=False))
        self.assertEqual(self.keyboard.writes(), [])

    def test_a_keyboard_that_changed_meanwhile_is_left_alone(self):
        class Fiddled(FakeKeyboard):
            reads = 0

            def ask(self, *request, **kw):
                if request[0] == 0x01:
                    self.reads += 1
                    if self.reads == 2:
                        self.keymap[0:2] = b"\x00\x05"  # someone changed a key in VIA
                return super().ask(*request, **kw)
        self.keyboard = Fiddled(self.reset)
        use_keyboard(self, self.keyboard)
        write_layout("keymap-2026-10-01_120000.layout.json", LAYERS)
        with self.assertRaises(SystemExit) as caught:
            self.restore()
        self.assertIn("changed in the meantime", caught.exception.code)
        self.assertEqual(self.keyboard.writes(), [])


class Backups(unittest.TestCase):
    def test_the_list_says_what_changed(self):
        board = make_board()
        clear_saved()
        write_layout("keymap-2026-10-01_120000.layout.json", LAYERS, age=7200)
        changed = [layer[:] for layer in LAYERS]
        changed[4][1] = "KC_VOLD"
        write_layout("keymap-2026-10-02_120000.layout.json", changed, age=3600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nk.list_backups(board)
        self.assertRegex(out.getvalue(), r"\n +1  \S+ \S+  1 key +snapshots/keymap-2026-10-02")
        self.assertRegex(out.getvalue(), r"\n +2  \S+ \S+  oldest +snapshots/keymap-2026-10-01")

    def test_the_commands_it_suggests_work_from_any_folder(self):
        with mock.patch("shutil.which", return_value=None):
            hint = nk.as_command("restore 2")
        self.assertTrue(hint.endswith("bin/nuphy-keymap restore 2") and not hint.startswith("bin/"), hint)
        with mock.patch("shutil.which", return_value=str(ROOT / "bin" / "nuphy-keymap")):
            self.assertEqual(nk.as_command("restore 2"), "nuphy-keymap restore 2")


class Errors(unittest.TestCase):
    def test_a_missing_file_is_named_not_blamed_on_usb(self):
        make_board()
        missing = DATA / "missing.layout.json"
        with self.assertRaises(SystemExit) as caught:
            nk.main(["verify", str(missing)])
        self.assertEqual(caught.exception.code, f"Can't find {missing}.")


if __name__ == "__main__":
    unittest.main()
