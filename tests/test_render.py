"""Smoke tests of render(): a made-up world of three countries, drawn in every
style and format, without network or Natural Earth data."""
import numpy as np
import pytest
from PIL import Image

import population_cartogram as pc

BOXES = {1: (2, 2, 14, 12), 2: (2, 14, 14, 26), 3: (15, 6, 18, 9)}     # row0, col0, row1, col1
NAMES = {1: ("AAA", "Alphaland", 60e6), 2: ("BBB", "Betania", 40e6), 3: ("CCC", "Gamma Isle", 1.2e6)}


def made_up_layout(style):
    H, W = 20, 30
    G = np.zeros((H, W), int)
    for i, (r0, c0, r1, c1) in BOXES.items():
        G[r0:r1, c0:c1] = i
    T = np.bincount(G.ravel(), minlength=4)
    T[0] = 0
    centre = np.array([[0.0, 0.0]] + [[(r0 + r1) / 2 - 0.5, (c0 + c1) / 2 - 0.5]
                                      for r0, c0, r1, c1 in BOXES.values()])
    units = [{"code": code, "name": name, "continent": "Europe", "pop": pop, "year": 2025,
              "source": "World Bank"} for code, name, pop in NAMES.values()]
    pops = np.array([0.0] + [pop for _, _, pop in NAMES.values()])
    info = {"year": 2025, "year_given": False, "projected": False, "latest": 2025, "wb_years": "2025",
            "owid": [], "world": float(pops.sum()), "cells": int(T.sum()), "layout": "2018", "style": style,
            "continents": [("Europe", float(pops.sum()))], "projection": "equal-earth",
            "colour_what": None, "breaks": None, "classes": 0}
    shapes = sphere = graticule = None
    if style in ("outlines", "map"):
        shapes = [(i, [[np.array([[c0, r0], [c1, r0], [c1, r1], [c0, r1]], float)]])
                  for i, (r0, c0, r1, c1) in BOXES.items()]
    if style == "map":
        sphere = np.array([[0.5, 0.5], [W - 0.5, 0.5], [W - 0.5, H - 0.5], [0.5, H - 0.5]])
        graticule = [np.array([[15.0, 0.5], [15.0, H - 0.5]])]
    return {"G": G, "T": T, "centre": centre, "units": units, "pops": pops, "info": info, "per_cell": 5e6,
            "shapes": shapes, "shape_off": (0, 0), "sphere": sphere, "graticule": graticule}


@pytest.mark.parametrize("ext", ["png", "svg", "pdf"])
@pytest.mark.parametrize("style", ["squares", "outlines", "map"])
def test_render_writes_every_format(tmp_path, style, ext):
    layout = made_up_layout(style)
    pals, default = pc.load_palettes(pc.PALETTES_FILE)
    th = pc.make_theme(pals[default]["light"], "light")
    out = tmp_path / f"world.{ext}"
    thematic = None
    if style == "map":
        ctx = {"breaks": "quantile", "classes": 3, "full_range": True}
        thematic = pc.thematic_spec("population", layout["units"], layout["T"], pc.TEXTS["en"], layout["info"], ctx)
        layout["info"].update(breaks=thematic["breaks"], classes=len(thematic["labels"]))
    pc.render(layout, th, "en", 40, str(out), thematic)
    data = out.read_bytes()
    if ext == "png":
        w, h = Image.open(out).size                  # the page grows to fit the title and legend
        assert w >= 30 * 40 and h >= 20 * 40
    elif ext == "svg":
        text = data.decode("utf-8")
        assert text.startswith("<svg") and 'id="AAA"' in text and "Alphaland" in text
    else:
        assert data.startswith(b"%PDF")
