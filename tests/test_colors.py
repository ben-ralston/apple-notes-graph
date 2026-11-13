"""Tests for color name/RGB mapping module."""

import struct

from apple_notes.colors import (
    COLOR_MAP,
    color_name_to_rgb,
    hex_to_rgb_floats,
    rgb_floats_to_hex,
    rgb_floats_to_ints,
    rgb_ints_to_floats,
    rgb_to_color_name,
)


class TestColorNameToRgb:
    """Tests for color_name_to_rgb lookup."""

    def test_known_color(self) -> None:
        result = color_name_to_rgb("red")
        assert result is not None
        assert result == COLOR_MAP["red"]

    def test_case_insensitive(self) -> None:
        assert color_name_to_rgb("Red") == color_name_to_rgb("red")
        assert color_name_to_rgb("BLUE") == color_name_to_rgb("blue")

    def test_unknown_color(self) -> None:
        assert color_name_to_rgb("chartreuse") is None

    def test_all_named_colors_exist(self) -> None:
        for name in [
            "red",
            "orange",
            "yellow",
            "green",
            "teal",
            "blue",
            "indigo",
            "purple",
            "pink",
        ]:
            assert color_name_to_rgb(name) is not None, f"Missing color: {name}"


class TestRgbToColorName:
    """Tests for nearest-match color name lookup."""

    def test_exact_match(self) -> None:
        r, g, b = COLOR_MAP["red"]
        assert rgb_to_color_name(r, g, b) == "red"

    def test_close_match(self) -> None:
        r, g, b = COLOR_MAP["blue"]
        # Slightly perturbed values should still match
        assert rgb_to_color_name(r + 0.01, g + 0.01, b - 0.01) == "blue"

    def test_no_match(self) -> None:
        # Pure white — not close to any named color
        assert rgb_to_color_name(1.0, 1.0, 1.0) is None

    def test_all_colors_roundtrip(self) -> None:
        for name, (r, g, b) in COLOR_MAP.items():
            assert rgb_to_color_name(r, g, b) == name, f"Failed for {name}"

    def test_highlight_only_rejects_non_highlight_colors(self) -> None:
        """Non-highlight colors (red, yellow, teal, indigo) return None with highlight_only."""
        for name in ("red", "yellow", "teal", "indigo"):
            r, g, b = COLOR_MAP[name]
            assert rgb_to_color_name(r, g, b, highlight_only=True) is None, (
                f"{name} should not match with highlight_only=True"
            )

    def test_highlight_only_matches_highlight_colors(self) -> None:
        """Highlight colors (purple, pink, orange, green, blue) still match with highlight_only."""
        for name in ("purple", "pink", "orange", "green", "blue"):
            r, g, b = COLOR_MAP[name]
            assert rgb_to_color_name(r, g, b, highlight_only=True) == name, (
                f"{name} should match with highlight_only=True"
            )


class TestRgbIntsToFloats:
    """Tests for IEEE 754 uint32 ↔ float32 conversion."""

    def test_known_conversion(self) -> None:
        # 1.0f = 0x3F800000 = 1065353216
        r, g, b = rgb_ints_to_floats(1065353216, 0, 0)
        assert abs(r - 1.0) < 1e-6
        assert abs(g - 0.0) < 1e-6
        assert abs(b - 0.0) < 1e-6

    def test_roundtrip(self) -> None:
        for name, (r, g, b) in COLOR_MAP.items():
            r_int, g_int, b_int = rgb_floats_to_ints(r, g, b)
            r2, g2, b2 = rgb_ints_to_floats(r_int, g_int, b_int)
            assert abs(r - r2) < 1e-6, f"R roundtrip failed for {name}"
            assert abs(g - g2) < 1e-6, f"G roundtrip failed for {name}"
            assert abs(b - b2) < 1e-6, f"B roundtrip failed for {name}"

    def test_zero_is_zero(self) -> None:
        r, g, b = rgb_ints_to_floats(0, 0, 0)
        assert r == 0.0
        assert g == 0.0
        assert b == 0.0


class TestRgbFloatsToInts:
    """Tests for float32 → uint32 conversion."""

    def test_one_point_zero(self) -> None:
        r_int, g_int, b_int = rgb_floats_to_ints(1.0, 1.0, 1.0)
        # 1.0f = 0x3F800000
        assert r_int == 0x3F800000
        assert g_int == 0x3F800000
        assert b_int == 0x3F800000

    def test_half(self) -> None:
        r_int, _, _ = rgb_floats_to_ints(0.5, 0.0, 0.0)
        # Verify it decodes back
        val = struct.unpack("f", struct.pack("I", r_int))[0]
        assert abs(val - 0.5) < 1e-6


class TestHexToRgbFloats:
    """Tests for hex string → float RGB conversion."""

    def test_valid_hex_with_hash(self) -> None:
        result = hex_to_rgb_floats("#FF0000")
        assert result is not None
        assert abs(result[0] - 1.0) < 1e-3
        assert abs(result[1] - 0.0) < 1e-3
        assert abs(result[2] - 0.0) < 1e-3

    def test_lowercase_hex(self) -> None:
        result = hex_to_rgb_floats("#ff8000")
        assert result is not None
        assert abs(result[0] - 1.0) < 1e-3
        assert abs(result[1] - 128 / 255.0) < 1e-3

    def test_no_hash_prefix(self) -> None:
        result = hex_to_rgb_floats("00FF00")
        assert result is not None
        assert abs(result[0] - 0.0) < 1e-3
        assert abs(result[1] - 1.0) < 1e-3
        assert abs(result[2] - 0.0) < 1e-3

    def test_invalid_characters(self) -> None:
        assert hex_to_rgb_floats("#GGGGGG") is None

    def test_wrong_length(self) -> None:
        assert hex_to_rgb_floats("#FFF") is None
        assert hex_to_rgb_floats("#FF00FF00") is None


class TestRgbFloatsToHex:
    """Tests for float RGB → hex string."""

    def test_pure_red(self) -> None:
        assert rgb_floats_to_hex(1.0, 0.0, 0.0) == "#FF0000"

    def test_pure_white(self) -> None:
        assert rgb_floats_to_hex(1.0, 1.0, 1.0) == "#FFFFFF"

    def test_pure_black(self) -> None:
        assert rgb_floats_to_hex(0.0, 0.0, 0.0) == "#000000"
