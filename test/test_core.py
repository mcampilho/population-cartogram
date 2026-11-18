"""Unit tests of the pure functions: no network, no Natural Earth data."""
import math
import string
import sys

import numpy as np
import pytest

import population_cartogram as pc


# --- text tables ------------------------------------------------------------
def fields(s):
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def flat(table, prefix=""):
    for k, v in table.items():
        if isinstance(v, dict):
            yield from flat(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def test_every_language_has_a_table():
    assert set(pc.TEXTS) == set(pc.LANGS)
    assert set(pc.NAME_FIELD) == set(pc.LANGS)


@pytest.mark.parametrize("lang", [code for code in pc.LANGS if code != "en"])
def test_translations_match_english(lang):
    en, other = dict(flat(pc.TEXTS["en"])), dict(flat(pc.TEXTS[lang]))
    assert set(other) <= set(en), f"keys unknown in English: {set(other) - set(en)}"
    assert set(en) <= set(other), f"keys missing in {lang}: {set(en) - set(other)}"
    for key, text in other.items():
        assert fields(text) == fields(en[key]), f"{lang}.{key}: placeholders differ from English"


def test_every_break_method_is_named():
    for lang in pc.LANGS:
        assert set(pc.TEXTS[lang]["breaks"]) >= {"quantile", "jenks", "log", "linear"}


# --- numbers ------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [(41.7, 47.1), (0.123, 0.15), (99, 101), (1234, 1290), (5, 5.0001), (-7, -3),
                                 (0.3, 0.30000001), (72.25, 72.3), (1e9, 1.000001e9)])
def test_roundest_lies_in_the_interval(a, b):
    r = pc.roundest(a, b)
    assert a < r <= b


def test_roundest_on_random_neighbours():
    rng = np.random.default_rng(0)
    for a in np.exp(rng.uniform(-5, 22, 2000)):
        b = a * (1 + 10 ** rng.uniform(-9, 0))
        assert a < pc.roundest(a, b) <= b, (a, b)


def test_roundest_prefers_fewer_digits():
    assert pc.roundest(41.7, 47.1) == 42
    assert pc.roundest(99, 101) == 100
    assert pc.roundest(-3, 2) == 0


def test_fmt_pop_uses_the_language():
    assert pc.fmt_pop(8.22e9, pc.TEXTS["pt"]) == "8,22 mil milhões"
    assert pc.fmt_pop(57_000, pc.TEXTS["pt"]) == "57 mil"


# --- class breaks -------------------------------------------------------------
def classes(v, edges):
    return np.bincount(np.digitize(v, edges), minlength=len(edges) + 1)


def test_quantile_classes_hold_the_same_number_of_countries():
    v = np.exp(np.random.default_rng(1).normal(15, 2, 233))      # as uneven as population
    edges = pc.class_edges(v, 9, "quantile")
    assert len(edges) == 8
    counts = classes(v, edges)
    assert counts.min() >= 25 and counts.max() <= 27


def test_jenks_finds_clear_groups():
    v = np.r_[np.full(20, 10.0), np.full(20, 1000.0), np.full(20, 100000.0)]
    v = v * np.random.default_rng(2).uniform(0.9, 1.1, v.size)
    edges = pc.class_edges(v, 3, "jenks")
    assert classes(v, edges).tolist() == [20, 20, 20]


def test_jenks_cuts_are_optimal_on_a_small_case():
    s = np.array([1.0, 2, 3, 10, 11, 12, 30])
    assert pc.jenks_cuts(s, 3) == [3, 6]


@pytest.mark.parametrize("method", ["quantile", "jenks", "log", "linear"])
def test_edges_are_increasing_and_inside_the_range(method):
    v = np.exp(np.random.default_rng(3).normal(10, 1.5, 100))
    edges = pc.class_edges(v, 7, method)
    assert edges == sorted(edges) and len(set(edges)) == len(edges)
    assert v.min() < edges[0] and edges[-1] <= v.max()


# --- projections --------------------------------------------------------------
@pytest.mark.parametrize("name", list(pc.PROJECTIONS))
def test_projection_is_centred_and_symmetric(name):
    lon = np.array([0.0, 60, -60, 60, 120])
    lat = np.array([0.0, 30, 30, -30, 45])
    x, y = pc.project(name, lon, lat)
    assert abs(x[0]) < 1e-12 and abs(y[0]) < 1e-12
    assert x[1] == pytest.approx(-x[2]) and y[1] == pytest.approx(y[2])
    assert y[3] == pytest.approx(-y[1]) and x[3] == pytest.approx(x[1])
    assert x[4] > x[1] > 0 and y[4] > y[1] > 0


def area_scale(name, lon, lat, h=1e-4):
    """Projected area of a tiny lon/lat cell divided by its area on the sphere."""
    (x0, y0), (x1, y1), (x2, y2) = (pc.project(name, lon + dl, lat + dp) for dl, dp in ((0, 0), (h, 0), (0, h)))
    jac = abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / h ** 2
    return jac / (math.radians(1) ** 2 * math.cos(math.radians(lat)))


@pytest.mark.parametrize("name", ["equal-earth", "mollweide", "gall-peters"])
def test_equal_area_projections_keep_areas(name):
    scales = [area_scale(name, lon, lat) for lon in (0, 50, 150) for lat in (-70, -20, 0, 35, 80)]
    assert max(scales) / min(scales) == pytest.approx(1, abs=1e-3)


def test_mercator_inflates_high_latitudes():
    assert area_scale("mercator", 0, 70) > 7 * area_scale("mercator", 0, 0)


# --- colours ------------------------------------------------------------------
def test_contrast_ratio():
    assert pc.contrast_ratio("#000000", "#ffffff") == pytest.approx(21)
    assert pc.contrast_ratio("#ffffff", "#000000") == pytest.approx(21)
    assert pc.contrast_ratio("#3a7bd5", "#3a7bd5") == pytest.approx(1)


def test_mix_and_hex():
    assert pc.hex_rgb("#ff8000") == (255, 128, 0)
    assert pc.mix("#000000", "#ffffff", 0) == "#000000"
    assert pc.mix("#000000", "#ffffff", 1) == "#ffffff"


def test_palette_file_is_valid():
    pals, default = pc.load_palettes(pc.PALETTES_FILE)
    assert default in pals
    for pal in pals.values():
        for mode in ("light", "dark"):
            th = pc.make_theme(pal[mode], mode)
            assert th["bg"].startswith("#")


# --- output helpers -------------------------------------------------------------
def test_svg_path_drops_repeated_points():
    assert pc.svg_path([np.array([[0, 0], [1, 0], [1, 0], [1, 1]])]) == "M0 0L1 0L1 1Z"


def test_vector_output(monkeypatch):
    assert pc.vector_output("map.svg") and not pc.vector_output("map.png")
    monkeypatch.setitem(sys.modules, "reportlab", None)          # as if reportlab were not installed
    assert not pc.vector_output("map.pdf")


def test_outline_cache_round_trip(tmp_path):
    rng = np.random.default_rng(4)
    shapes = [(2, [[rng.random((5, 2)), rng.random((4, 2))], [rng.random((6, 2))]]),
              (1, [[rng.random((3, 2))]])]
    G, T, centre = rng.integers(0, 3, (4, 5)), np.array([0, 3, 4]), rng.random((3, 2))
    path = str(tmp_path / "outlines.npz")
    pc.save_outlines(path, G, T, centre, shapes)
    G2, T2, centre2, shapes2 = pc.load_outlines(path)
    assert (G2 == G).all() and (T2 == T).all() and (centre2 == centre).all()
    assert [i for i, _ in shapes2] == [2, 1]
    for (_, polys), (_, polys2) in zip(shapes, shapes2):
        assert len(polys) == len(polys2)
        for poly, poly2 in zip(polys, polys2):
            assert len(poly) == len(poly2)
            assert all((r == r2).all() for r, r2 in zip(poly, poly2))
