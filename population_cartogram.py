#!/usr/bin/env python3
"""
population_cartogram.py
=======================
World population cartogram on a square grid. Every square stands for a fixed
number of people (500,000 by default) and the squares of each country keep
its rough shape and its place on the world map: populous countries grow
(India, China, Nigeria) and empty ones shrink (Canada, Russia, Australia).

Default layout (--layout 2018): Max Roser's 2018 cartogram for Our World in
Data (CC BY), digitised by Matt Dzugan, with squares added or removed so that
every country matches its latest population. Countries grow into the sea next
to them; landlocked ones take squares from a neighbour, which takes from the
next one, until a country that can grow into the sea.

Computed layout (--layout auto), for any number of people per square:
  1. Population: the most recent value for every country from the World Bank
     API (indicator SP.POP.TOTL). Countries the World Bank does not list
     (Taiwan, Western Sahara, Vatican...) come from the UN World Population
     Prospects through Our World in Data.
  2. Map: Natural Earth 1:50m countries in the Equal Earth projection,
     rasterised. Inside each country the people are spread with the help of
     Natural Earth's ~7300 populated places, so Alaska, Siberia or the Sahara
     get few squares and coasts and river valleys get many.
  3. Cartogram: Gastner-Newman diffusion (a density-equalising map), repeated
     a few times to correct what is left over.
  4. Grid: the deformed map is sampled on a square grid where one cell holds
     500,000 people; then single squares are moved along the coasts until
     every country has exactly round(population / 500,000) squares.
  5. Drawing: neighbours get different colours; labels go inside a country
     when they fit and outside, with a leader line, when they do not.

Outline style (--style outlines), like Worldmapper: the same diffusion
cartogram, but the Natural Earth borders themselves are moved with it, then
corrected with the Dougenik-Chrisman-Niemeyer algorithm so that every
country's area is proportional to its population.

Data (downloaded on the first run and cached in ~/.cache/population_cartogram):
  - World Bank API (api.worldbank.org), refreshed after 30 days
  - Our World in Data, population (UN WPP), refreshed after 30 days
  - Natural Earth countries and populated places (github.com/nvkelso/natural-earth-vector)

Requirements:
    pip install numpy scipy pillow

Examples:
    python population_cartogram.py                         # light theme, system language
    python population_cartogram.py --theme dark --lang pt
    python population_cartogram.py --theme both -o mapa.png   # mapa_light.png and mapa_dark.png
    python population_cartogram.py --year 2050             # UN projection
    python population_cartogram.py --region africa --color-by life -o africa.svg
    python population_cartogram.py --per-cell 1000000 --cell-px 14
    python population_cartogram.py --data my_pop.csv       # iso3,population[,year] overrides
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import locale
import math
import os
import pickle
import sys
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.fft import dctn, idctn
from scipy.ndimage import distance_transform_edt, gaussian_filter, map_coordinates
from scipy.ndimage import label as cc_label
from scipy.spatial import cKDTree

UA = "PopulationCartogram/1.0 (personal study script)"
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
COUNTRIES_URL = NE + "ne_50m_admin_0_countries.geojson"
PLACES_URL = NE + "ne_10m_populated_places_simple.geojson"
WB_API = "https://api.worldbank.org/v2"
# UN World Population Prospects 2024: estimates up to 2023, medium-variant projection to 2100
OWID_URL = ("https://ourworldindata.org/grapher/population-with-un-projections.csv"
            "?v=1&csvType=full&useColumnShortNames=true")
UN_LABEL = "UN WPP (Our World in Data)"
UN_PROJ_LABEL = "UN WPP projection (Our World in Data)"
# Max Roser's 2018 cartogram for Our World in Data (CC BY), digitised square by
# square by Matt Dzugan: X, Y, ISO 3166-1 numeric code.
BASE2018_URL = ("https://raw.githubusercontent.com/mattdzugan/World-Population-Cartogram/"
                "master/data/year_2018__cell_500k/squares/cells.csv")
DATA_MAX_AGE = 30 * 86400              # seconds before population data is downloaded again
CARTOGRAM_VERSION = 1                  # bump when build_cartogram changes, to invalidate the cache
OUTLINES_VERSION = 1                   # the same for layout_outlines / rubber_sheet

# Natural Earth units without a population figure of their own, folded into
# the country the data sources count them in.
MERGE = {"SOL": "SOM", "CYN": "CYP", "KAS": "IND", "ALA": "FIN", "KOS": "XKX",
         "JEY": "CHI", "GGY": "CHI"}
SKIP = {"ATA"}                         # Antarctica

# French overseas departments: squares of their own in the 2018 layout, but part
# of France in Natural Earth and in the World Bank's figure for France.
OVERSEAS = {
    638: ("REU", {"en": "Réunion", "pt": "Reunião", "es": "Reunión"}, "Africa", (55.5, -21.1)),
    312: ("GLP", {"en": "Guadeloupe", "pt": "Guadalupe", "es": "Guadalupe"}, "North America", (-61.5, 16.2)),
    474: ("MTQ", {"en": "Martinique", "pt": "Martinica", "es": "Martinica"}, "North America", (-61.0, 14.6)),
    254: ("GUF", {"en": "French Guiana", "pt": "Guiana Francesa", "es": "Guayana Francesa"},
          "South America", (-53.0, 4.0)),
    175: ("MYT", {"en": "Mayotte", "pt": "Maiote", "es": "Mayotte"}, "Africa", (45.1, -12.8)),
}

LANGS = ("en", "pt", "es")
NAME_FIELD = {"en": "NAME", "pt": "NAME_PT", "es": "NAME_ES"}
NAME_FIX = {
    "en": {"United States of America": "United States", "Dem. Rep. Congo": "DR Congo",
           "Jersey": "Channel Islands"},
    "pt": {"República Democrática do Congo": "R. D. Congo", "República do Congo": "Congo",
           "República Centro-Africana": "Rep. Centro-Africana", "República da Irlanda": "Irlanda",
           "República Turca do Chipre do Norte": "Chipre do Norte", "Jersey": "Ilhas do Canal"},
    "es": {"República Democrática del Congo": "R. D. del Congo", "República del Congo": "Congo",
           "Suazilandia": "Esuatini", "Jersey": "Islas del Canal"},
}

TEXTS = {
    "en": {
        "title": "World population cartogram {year}",
        "square": "Each square represents {n} people.",
        "squares": "{cells} squares show where the world's {total} people live.",
        "share": "{p}% of the world population",
        "small": "Not on the map (fewer than {n} people)",
        "source": "Population: World Bank (SP.POP.TOTL), latest year per country: {years}{owid}.",
        "owid": "; UN WPP via Our World in Data for {k}",
        "map": "Map: Natural Earth. Shapes and positions are approximate.",
        "base": "Layout: Max Roser's 2018 cartogram (Our World in Data, CC BY), digitised by Matt Dzugan, "
                "with squares added or removed for the latest figures.",
        "continents": {"Asia": "Asia", "Africa": "Africa", "Europe": "Europe",
                       "North America": "North America", "South America": "South America",
                       "Oceania": "Oceania", "Americas": "Americas"},
        "billion": "{v} billion", "million": "{v}m", "thousand": "{v}k",
        "dec": ".", "sep": ",",
        "title_proj": "World population cartogram {year} (UN projection)",
        "title_region": "{region}: population cartogram {year}",
        "title_region_proj": "{region}: population cartogram {year} (UN projection)",
        "region_total": "{cells} squares: {total} people, {pct}% of the world population.",
        "source_year": "Population in {years}: World Bank (SP.POP.TOTL){owid}.",
        "source_un": "Population: UN World Population Prospects 2024, medium variant, via Our World in Data.",
        "area": "The area of each country is proportional to its population.",
        "area_world": "The map shows where the world's {total} people live.",
        "region_area": "{total} people, {pct}% of the world population.",
        "small_outline": "Smallest countries (fewer than {n} people)",
        "outline_map": "Map: Natural Earth, resized with the Gastner–Newman diffusion method, in the style of "
                       "Worldmapper. Shapes and positions are approximate.",
        "colour_by": "Colour: {what}",
        "no_data": "no data",
        "growth": "population change {a}–{b}",
        "continent": "continent",
        "range": "{a} to {b}", "below": "below {b}", "above": "{a} and over",
        "indicators": {"gdp": "GDP per capita (current US$)", "life": "life expectancy at birth (years)",
                       "fertility": "fertility rate (births per woman)", "urban": "urban population (% of total)",
                       "density": "population density (people per km²)"},
    },
    "pt": {
        "title": "Cartograma da população mundial {year}",
        "square": "Cada quadrado representa {n} pessoas.",
        "squares": "{cells} quadrados mostram onde vivem os {total} habitantes do mundo.",
        "share": "{p}% da população mundial",
        "small": "Fora do mapa (menos de {n} pessoas)",
        "source": "População: Banco Mundial (SP.POP.TOTL), ano mais recente de cada país: {years}{owid}.",
        "owid": "; ONU (WPP) via Our World in Data para {k}",
        "map": "Mapa: Natural Earth. Formas e posições aproximadas.",
        "base": "Disposição: cartograma de 2018 de Max Roser (Our World in Data, CC BY), digitalizado por "
                "Matt Dzugan, com quadrados acrescentados ou retirados segundo os números mais recentes.",
        "continents": {"Asia": "Ásia", "Africa": "África", "Europe": "Europa",
                       "North America": "América do Norte", "South America": "América do Sul",
                       "Oceania": "Oceânia", "Americas": "Américas"},
        "billion": "{v} mil milhões", "million": "{v} M", "thousand": "{v} mil",
        "dec": ",", "sep": " ",
        "title_proj": "Cartograma da população mundial {year} (projeção da ONU)",
        "title_region": "{region}: cartograma da população {year}",
        "title_region_proj": "{region}: cartograma da população {year} (projeção da ONU)",
        "region_total": "{cells} quadrados: {total} pessoas, {pct}% da população mundial.",
        "source_year": "População em {years}: Banco Mundial (SP.POP.TOTL){owid}.",
        "source_un": "População: ONU, World Population Prospects 2024, variante média, via Our World in Data.",
        "area": "A área de cada país é proporcional à sua população.",
        "area_world": "O mapa mostra onde vivem os {total} habitantes do mundo.",
        "region_area": "{total} pessoas, {pct}% da população mundial.",
        "small_outline": "Países mais pequenos (menos de {n} pessoas)",
        "outline_map": "Mapa: Natural Earth, redimensionado pelo método de difusão de Gastner–Newman, ao estilo "
                       "do Worldmapper. Formas e posições aproximadas.",
        "colour_by": "Cor: {what}",
        "no_data": "sem dados",
        "growth": "variação da população {a}–{b}",
        "continent": "continente",
        "range": "{a} a {b}", "below": "menos de {b}", "above": "{a} ou mais",
        "indicators": {"gdp": "PIB per capita (US$ correntes)", "life": "esperança de vida à nascença (anos)",
                       "fertility": "fecundidade (filhos por mulher)", "urban": "população urbana (% do total)",
                       "density": "densidade populacional (hab./km²)"},
    },
    "es": {
        "title": "Cartograma de la población mundial {year}",
        "square": "Cada cuadrado representa {n} personas.",
        "squares": "{cells} cuadrados muestran dónde viven los {total} habitantes del mundo.",
        "share": "{p} % de la población mundial",
        "small": "Fuera del mapa (menos de {n} personas)",
        "source": "Población: Banco Mundial (SP.POP.TOTL), año más reciente de cada país: {years}{owid}.",
        "owid": "; ONU (WPP) vía Our World in Data para {k}",
        "map": "Mapa: Natural Earth. Formas y posiciones aproximadas.",
        "base": "Disposición: cartograma de 2018 de Max Roser (Our World in Data, CC BY), digitalizado por "
                "Matt Dzugan, con cuadrados añadidos o quitados según las cifras más recientes.",
        "continents": {"Asia": "Asia", "Africa": "África", "Europe": "Europa",
                       "North America": "América del Norte", "South America": "América del Sur",
                       "Oceania": "Oceanía", "Americas": "Américas"},
        "billion": "{v} mil millones", "million": "{v} M", "thousand": "{v} mil",
        "dec": ",", "sep": ".",
        "title_proj": "Cartograma de la población mundial {year} (proyección de la ONU)",
        "title_region": "{region}: cartograma de la población {year}",
        "title_region_proj": "{region}: cartograma de la población {year} (proyección de la ONU)",
        "region_total": "{cells} cuadrados: {total} personas, {pct} % de la población mundial.",
        "source_year": "Población en {years}: Banco Mundial (SP.POP.TOTL){owid}.",
        "source_un": "Población: ONU, World Population Prospects 2024, variante media, vía Our World in Data.",
        "area": "El área de cada país es proporcional a su población.",
        "area_world": "El mapa muestra dónde viven los {total} habitantes del mundo.",
        "region_area": "{total} personas, {pct} % de la población mundial.",
        "small_outline": "Países más pequeños (menos de {n} personas)",
        "outline_map": "Mapa: Natural Earth, redimensionado con el método de difusión de Gastner–Newman, al estilo "
                       "de Worldmapper. Formas y posiciones aproximadas.",
        "colour_by": "Color: {what}",
        "no_data": "sin datos",
        "growth": "variación de la población {a}–{b}",
        "continent": "continente",
        "range": "{a} a {b}", "below": "menos de {b}", "above": "{a} o más",
        "indicators": {"gdp": "PIB per cápita (US$ corrientes)", "life": "esperanza de vida al nacer (años)",
                       "fertility": "fecundidad (hijos por mujer)", "urban": "población urbana (% del total)",
                       "density": "densidad de población (hab./km²)"},
    },
}

# Colour palettes live in palettes.json next to this script (see the "_help"
# entry there). This copy of the default palette is used when the file is missing.
PALETTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palettes.json")
BUILTIN_PALETTES = {
    "default": "classic",
    "palettes": {
        "classic": {
            "description": "Vivid colours on light blue (2015 style) / muted colours on slate (2018 style)",
            "light": {"background": "#c6e9f2", "box_text": "#13232b", "box_fill": "#eef8fb",
                      "countries": ["#e41a1c", "#ff7f00", "#ffd92f", "#1a9641", "#1f4fe0", "#7b3fbf",
                                    "#e7298a", "#8c1c13", "#f4a6c4", "#0d8a8a", "#a65628", "#222222"]},
            "dark": {"background": "#343f50", "box_text": "#f1f3f6", "box_fill": "#273040",
                     "countries": ["#e5736f", "#f2a541", "#f1d65c", "#4fae6e", "#3f86d4", "#9f6fd0",
                                   "#e95e9f", "#2db6b3", "#b9d36a", "#b97a4c", "#8fa2ef", "#dcdcdc"]},
        },
    },
}
MIN_COUNTRY_COLOURS, MAX_COUNTRY_COLOURS = 4, 16
SIMILAR_DE = 12.0                      # CIEDE2000 below this: colours look alike
# Colour ramps for thematic maps (--color-by); a palette may override them with
# "sequential" (low to high) and "diverging" (decline, neutral, growth) lists.
DEFAULT_RAMPS = {
    "light": {"sequential": ["#ffffcc", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8", "#0c2c84"],
              "diverging": ["#2166ac", "#92c5de", "#f7f7f7", "#fddbc7", "#f4a582", "#d6604d", "#b2182b"]},
    "dark": {"sequential": ["#3b2a6b", "#3e4f94", "#2f7d8f", "#21a07f", "#5cc35a", "#b2d834", "#fde725"],
             "diverging": ["#4393c3", "#92c5de", "#e8e8e8", "#fddbc7", "#f4a582", "#e0614f", "#c21f2e"]},
}

# --region: continents (as in the title box) that make up each region
REGIONS = {"africa": ["Africa"], "asia": ["Asia"], "europe": ["Europe"],
           "north-america": ["North America"], "south-america": ["South America"],
           "americas": ["North America", "South America"], "oceania": ["Oceania"]}

# --color-by shortcuts for World Bank indicators
INDICATORS = {"gdp": "NY.GDP.PCAP.CD", "life": "SP.DYN.LE00.IN", "fertility": "SP.DYN.TFRT.IN",
              "urban": "SP.URB.TOTL.IN.ZS", "density": "EN.POP.DNST"}
GROWTH_EDGES = [-5, -1, 1, 5, 10, 20, 35]        # % change; class 2 (-1..1) is neutral

FONT_FILES = {False: ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
              True: ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]}

N4 = ((-1, 0), (1, 0), (0, -1), (0, 1))
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def cached(url, path, offline, max_age=None):
    """Return a local copy of url, downloading it when missing or older than max_age."""
    if os.path.exists(path) and (offline or max_age is None
                                 or time.time() - os.path.getmtime(path) < max_age):
        return path
    if offline:
        sys.exit(f"--offline: no cached copy of {url}")
    try:
        log(f"downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = urllib.request.urlopen(req, timeout=180).read()
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    except Exception as e:  # noqa: BLE001 - any network error falls back to the cache
        if os.path.exists(path):
            log(f"  download failed ({e}); using the cached copy")
        else:
            raise
    return path


def load_worldbank(indicator, cache, offline, year=None):
    """World Bank indicator for every economy: iso3 -> (value, year). Latest value
    per country when year is None, otherwise the value for that year."""
    query = f"date={year}" if year else "mrnev=1"
    url = f"{WB_API}/country/all/indicator/{indicator}?format=json&per_page=1000&{query}"
    p = cached(url, os.path.join(cache, f"worldbank_{indicator}_{year or 'latest'}.json"), offline, DATA_MAX_AGE)
    with open(p, encoding="utf-8") as f:
        payload = json.load(f)
    out = {}
    for r in payload[1] if isinstance(payload, list) and len(payload) > 1 and payload[1] else []:
        code, v = r.get("countryiso3code"), r.get("value")
        if code and v is not None:
            out[code] = (float(v), int(r["date"]))
    return out


def worldbank_indicator_name(indicator, cache, offline):
    p = cached(f"{WB_API}/indicator/{indicator}?format=json",
               os.path.join(cache, f"worldbank_{indicator}_meta.json"), offline)
    with open(p, encoding="utf-8") as f:
        payload = json.load(f)
    try:
        return payload[1][0]["name"]
    except (IndexError, KeyError, TypeError):
        sys.exit(f"unknown World Bank indicator '{indicator}'")


def load_un(cache, offline):
    """UN WPP 2024 via Our World in Data: iso3 -> {year: (population, projected)}."""
    p = cached(OWID_URL, os.path.join(cache, "owid_population_with_projections.csv"), offline, DATA_MAX_AGE)
    series = collections.defaultdict(dict)
    with open(p, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = next(rd)
        est = next(k for k, h in enumerate(head) if "estimates" in h)
        proj = next(k for k, h in enumerate(head) if "projected" in h)
        for row in rd:
            code = row[1]
            if code == "OWID_WRL":
                code = "WLD"
            if len(code) != 3:
                continue
            try:
                year = int(row[2])
            except ValueError:
                continue
            if row[est]:
                series[code][year] = (float(row[est]), False)
            elif row[proj]:
                series[code][year] = (float(row[proj]), True)
    return series


def load_population(cache, offline, source, data_csv, year=None):
    """iso3 -> (population, year, source name), and the world total (or None).
    year None: the latest estimate for every country. A year past the latest
    World Bank figures uses the UN medium-variant projection."""
    pops = {}
    world = None
    if source in ("auto", "worldbank"):
        for code, (v, y) in load_worldbank("SP.POP.TOTL", cache, offline, year).items():
            pops[code] = (v, y, "World Bank")
        if "WLD" in pops:
            world = pops["WLD"][0]
    if source in ("auto", "owid"):
        this_year = dt.date.today().year
        for code, s in load_un(cache, offline).items():
            if year is None:
                ys = [y for y, (v, projected) in s.items() if not projected and y <= this_year]
                if not ys:
                    continue
                y = max(ys)
            elif year in s:
                y = year
            else:
                continue
            v, projected = s[y]
            if code == "WLD":
                world = world or v
                continue
            pops.setdefault(code, (v, y, UN_PROJ_LABEL if projected else UN_LABEL))
    pops.pop("WLD", None)
    if data_csv:
        with open(data_csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if not row or not row[0].strip() or row[0].strip().lower() in ("iso3", "code"):
                    continue
                year = int(row[2]) if len(row) > 2 and row[2].strip() else dt.date.today().year
                pops[row[0].strip().upper()] = (float(row[1]), year, "user data")
    return pops, world


def unit_code(p):
    code = p.get("ISO_A3_EH")
    if not code or code == "-99":
        code = p.get("ADM0_A3")
    return code


def load_countries(cache, offline, lang):
    path = cached(COUNTRIES_URL, os.path.join(cache, "ne_50m_admin_0_countries.geojson"), offline)
    with open(path, encoding="utf-8") as f:
        feats = json.load(f)["features"]
    units = collections.OrderedDict()
    for feat in feats:
        p = feat["properties"]
        raw = unit_code(p)
        code = MERGE.get(raw, raw)
        if code in SKIP:
            continue
        u = units.setdefault(code, {"code": code, "polys": [], "own": False, "name": None, "nums": set()})
        for k in ("ISO_N3_EH", "ISO_N3"):
            try:
                if int(p[k]) > 0:
                    u["nums"].add(int(p[k]))
            except (TypeError, ValueError):
                pass
        own = raw == code
        if u["name"] is None or (own and not u["own"]):
            name = p.get(NAME_FIELD[lang]) or p["NAME"]
            u["name"] = NAME_FIX[lang].get(name, name)
            cont = p["CONTINENT"]
            if cont.startswith("Seven seas"):
                cont = {"Americas": "North America"}.get(p["REGION_UN"], p["REGION_UN"])
            u["continent"] = cont
            u["label"] = (p["LABEL_X"], p["LABEL_Y"])
            u["own"] = own
        g = feat["geometry"]
        u["polys"].extend(g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]])
    return list(units.values())


def load_places(cache, offline):
    path = cached(PLACES_URL, os.path.join(cache, "ne_10m_populated_places_simple.geojson"), offline)
    with open(path, encoding="utf-8") as f:
        feats = json.load(f)["features"]
    lon = np.array([f["properties"]["longitude"] for f in feats], float)
    lat = np.array([f["properties"]["latitude"] for f in feats], float)
    pop = np.array([max(f["properties"].get("pop_max") or 0, 0) for f in feats], float)
    return lon, lat, pop


# ---------------------------------------------------------------------------
# Projection and raster
# ---------------------------------------------------------------------------
EE_A = (1.340264, -0.081106, 0.000893, 0.003796)
LAT_MIN, LAT_MAX = -57.0, 84.0


def equal_earth(lon, lat):
    a1, a2, a3, a4 = EE_A
    lam = np.radians(lon)
    th = np.arcsin(np.sqrt(3) / 2 * np.sin(np.radians(lat)))
    t2 = th * th
    t6 = t2 * t2 * t2
    x = 2 * np.sqrt(3) * lam * np.cos(th) / (3 * (9 * a4 * t6 * t2 + 7 * a3 * t6 + 3 * a2 * t2 + a1))
    y = th * (a1 + a2 * t2 + t6 * (a3 + a4 * t2))
    return x, y


class Frame:
    """Equal Earth raster frame: lon/lat -> pixel column/row."""

    def __init__(self, width):
        self.xmax = float(equal_earth(180.0, 0.0)[0])
        self.ytop = float(equal_earth(0.0, LAT_MAX)[1])
        ybot = float(equal_earth(0.0, LAT_MIN)[1])
        self.scale = width / (2 * self.xmax)
        self.w = width
        self.h = int(math.ceil((self.ytop - ybot) * self.scale))

    def px(self, lon, lat):
        x, y = equal_earth(np.asarray(lon, float), np.clip(np.asarray(lat, float), -89.9, 89.9))
        return (x + self.xmax) * self.scale, (self.ytop - y) * self.scale


def ring_area(xs, ys):
    return 0.5 * abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1)))


def rasterize(units, frame):
    """Label raster: 0 = sea, k + 1 = units[k]. Smaller units are drawn last (enclaves)."""
    rings = []
    for k, u in enumerate(units):
        area = 0.0
        prepared = []
        for poly in u["polys"]:
            pr = []
            for ring in poly:
                a = np.asarray(ring, float)
                cx, cy = frame.px(a[:, 0], a[:, 1])
                pr.append(list(zip(cx.tolist(), cy.tolist())))
            area += ring_area(*map(np.asarray, zip(*pr[0])))
            prepared.append(pr)
        rings.append((area, k, prepared))
    img = Image.new("I", (frame.w, frame.h), 0)
    draw = ImageDraw.Draw(img)
    for area, k, prepared in sorted(rings, key=lambda r: -r[0]):
        for pr in prepared:
            if len(pr[0]) >= 3:
                draw.polygon(pr[0], fill=k + 1)
            for hole in pr[1:]:
                if len(hole) >= 3:
                    draw.polygon(hole, fill=0)
    lab = np.array(img, dtype=np.int32)
    # units too small to cover a pixel get one at their label point
    present = np.bincount(lab.ravel(), minlength=len(units) + 1)
    for k, u in enumerate(units):
        if present[k + 1] == 0:
            c, r = frame.px(*u["label"])
            r, c = int(np.clip(r, 0, frame.h - 1)), int(np.clip(c, 0, frame.w - 1))
            lab[r, c] = k + 1
    return lab


def pixel_population(lab, pops, frame, places, rural, sigma_km):
    """People per pixel: a share `rural` spread evenly over the country, the rest
    following the (blurred) population of its towns and cities."""
    n = len(pops) - 1
    lon, lat, ppop = places
    c, r = frame.px(lon, lat)
    ok = (r >= 0) & (r < frame.h) & (c >= 0) & (c < frame.w)
    city = np.zeros(lab.shape)
    np.add.at(city, (r[ok].astype(int), c[ok].astype(int)), ppop[ok])
    km_per_px = 6371.0 / frame.scale
    city = gaussian_filter(city, sigma_km / km_per_px)
    flat = lab.ravel()
    npix = np.bincount(flat, minlength=n + 1).astype(float)
    csum = np.bincount(flat, weights=city.ravel(), minlength=n + 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        share_city = np.where(csum[lab] > 0, city / csum[lab], 1.0 / npix[lab])
        w = rural / npix[lab] + (1 - rural) * share_city
    pix = pops[lab] * w
    pix[lab == 0] = 0.0
    return pix


# ---------------------------------------------------------------------------
# Diffusion cartogram (Gastner & Newman 2004)
# ---------------------------------------------------------------------------
def diffuse(rho, max_steps=4000):
    """Move the cell centres of `rho` with the diffusion flow until the density is
    uniform. Returns the final (y, x) position of every cell centre."""
    ny, nx = rho.shape
    rho_hat = dctn(rho, type=2, norm="ortho")
    ky = np.pi * np.arange(ny) / ny
    kx = np.pi * np.arange(nx) / nx
    k2 = ky[:, None] ** 2 + kx[None, :] ** 2
    gy, gx = np.mgrid[0:ny, 0:nx].astype(float)
    py, px = gy.ravel().copy(), gx.ravel().copy()

    def velocity(t, y, x):
        r = idctn(rho_hat * np.exp(-k2 * t), type=2, norm="ortho")
        dy, dx = np.gradient(r)
        vy, vx = -dy / r, -dx / r
        c = np.vstack([y, x])
        return (map_coordinates(vy, c, order=1, mode="nearest"),
                map_coordinates(vx, c, order=1, mode="nearest"))

    t, dtime = 0.0, 1e-3
    t_end = 4.0 / min(ky[1], kx[1]) ** 2      # slowest mode decayed by e^-4
    for _ in range(max_steps):
        vy1, vx1 = velocity(t, py, px)
        vmax = float(np.sqrt(vy1 * vy1 + vx1 * vx1).max())
        dtime = min(dtime * 1.4, 0.25 / max(vmax, 1e-12))
        vy2, vx2 = velocity(t + dtime / 2, py + 0.5 * dtime * vy1, px + 0.5 * dtime * vx1)
        py = np.clip(py + dtime * vy2, 0, ny - 1)
        px = np.clip(px + dtime * vx2, 0, nx - 1)
        t += dtime
        if t > t_end or vmax * dtime < 1e-5:
            break
    return py.reshape(ny, nx), px.reshape(ny, nx)


def warp(field, y, x):
    fy, fx = field
    c = np.vstack([y, x])
    return (map_coordinates(fy, c, order=1, mode="nearest"),
            map_coordinates(fx, c, order=1, mode="nearest"))


def build_cartogram(lab, pix, pops, factor, pad_frac, ocean_factor, passes):
    """Returns the final position of every raster pixel (coarse-grid units) and the
    coarse grid's mean density (people per coarse cell)."""
    n = len(pops) - 1
    h, w = lab.shape
    land = lab > 0
    ocean_px = ocean_factor * pix.sum() / land.sum()
    dens = np.where(land, pix, ocean_px)
    hc, wc = -(-h // factor), -(-w // factor)
    padded = np.full((hc * factor, wc * factor), ocean_px)
    padded[:h, :w] = dens
    coarse = padded.reshape(hc, factor, wc, factor).sum(axis=(1, 3))
    py0, px0 = int(hc * pad_frac), int(wc * pad_frac)
    ocean_cell = ocean_px * factor * factor
    rho = np.full((hc + 2 * py0, wc + 2 * px0), ocean_cell)
    rho[py0:py0 + hc, px0:px0 + wc] = coarse
    ny, nx = rho.shape
    total = rho.sum()
    ocean_total = total - pix.sum()

    rr, cc = np.mgrid[0:h, 0:w]
    y = (rr.ravel() + 0.5) / factor - 0.5 + py0
    x = (cc.ravel() + 0.5) / factor - 0.5 + px0
    flat = lab.ravel()
    target_share = pops[1:] / pops[1:].sum()
    gy, gx = np.mgrid[0:ny, 0:nx]
    cells = np.c_[gy.ravel(), gx.ravel()].astype(float)
    for k in range(passes):
        if k > 0:
            _, nn = cKDTree(np.c_[y, x]).query(cells, workers=-1)
            L = flat[nn].reshape(ny, nx)
            area = np.bincount(L.ravel(), minlength=n + 1).astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                dens_l = np.where(area > 0, np.append(ocean_total, pops[1:]) / area, 0.0)
            rho = dens_l[L]
            share = area[1:] / max(area[1:].sum(), 1)
            big = pops[1:] / pops[1:].sum() > 2e-4
            err = np.abs(share[big] / target_share[big] - 1)
            log(f"  pass {k}: area error median {np.median(err):.1%}, 90th pct {np.percentile(err, 90):.1%}")
        field = diffuse(rho)
        y, x = warp(field, y, x)
        log(f"  diffusion pass {k + 1}/{passes} done")
    return y, x, total / (ny * nx)


# ---------------------------------------------------------------------------
# Square grid
# ---------------------------------------------------------------------------
def neighbours(mask):
    """Cells 4-adjacent to mask, not in it."""
    a = np.zeros_like(mask)
    a[1:] |= mask[:-1]
    a[:-1] |= mask[1:]
    a[:, 1:] |= mask[:, :-1]
    a[:, :-1] |= mask[:, 1:]
    return a & ~mask


def path_to_sea(G, start, passable):
    """Shortest 4-connected path from a cell next to `start` through `passable`
    cells to a sea cell. Returns (start cell it leaves from, [c1 .. sea cell])."""
    H, W = G.shape
    prev = {}
    dq = collections.deque()
    ys, xs = np.nonzero(start)
    for y, x in zip(ys.tolist(), xs.tolist()):
        for dy, dx in N4:
            v, u = y + dy, x + dx
            if 0 <= v < H and 0 <= u < W and (v, u) not in prev and not start[v, u] \
                    and (passable[v, u] or G[v, u] == 0):
                prev[(v, u)] = ("S", (y, x))
                dq.append((v, u))
    while dq:
        c = dq.popleft()
        if G[c] == 0:
            path = [c]
            while True:
                p = prev[path[-1]]
                if p[0] == "S":
                    path.reverse()
                    return p[1], path
                path.append(p)
        y, x = c
        for dy, dx in N4:
            v, u = y + dy, x + dx
            if 0 <= v < H and 0 <= u < W and (v, u) not in prev and not start[v, u] \
                    and (passable[v, u] or G[v, u] == 0):
                prev[(v, u)] = c
                dq.append((v, u))
    return None, None


class Balancer:
    """Moves squares one at a time until every country has its target count."""

    def __init__(self, G, target, centre):
        self.G, self.T, self.c = G, target, centre
        H, W = G.shape
        self.yy, self.xx = np.mgrid[0:H, 0:W]

    def dist2(self, i):
        cy, cx = self.c[i]
        return (self.yy - cy) ** 2 + (self.xx - cx) ** 2

    def add(self, i):
        G = self.G
        mine = G == i
        if not mine.any():
            ok = (G == 0) | (self.T[G] >= 5)
            d = np.where(ok, self.dist2(i), np.inf)
            G[np.unravel_index(np.argmin(d), G.shape)] = i
            return
        cand = neighbours(mine) & (G == 0)
        if cand.any():
            d = np.where(cand, self.dist2(i), np.inf)
            G[np.unravel_index(np.argmin(d), G.shape)] = i
            return
        _, path = path_to_sea(G, mine, ~mine)
        if path is None:
            return
        for j in range(len(path) - 1, 0, -1):
            G[path[j]] = G[path[j - 1]]
        G[path[0]] = i

    def remove(self, i):
        G = self.G
        mine = G == i
        cand = mine & neighbours(G == 0)
        if cand.any():
            d = np.where(cand, self.dist2(i), -np.inf)
            G[np.unravel_index(np.argmax(d), G.shape)] = 0
            return
        s, path = path_to_sea(G, mine, ~mine & (G != 0))
        if path is None:
            return
        G[s] = G[path[0]]
        for j in range(len(path) - 1):
            G[path[j]] = G[path[j + 1]]

    def run(self, rounds=30):
        n = len(self.T) - 1
        for _ in range(rounds):
            diff = np.bincount(self.G.ravel(), minlength=n + 1) - self.T
            diff[0] = 0
            if not diff.any():
                return 0
            for i in np.argsort(-diff):
                for _ in range(max(int(diff[i]), 0)):
                    self.remove(i)
            for i in np.argsort(diff):
                for _ in range(max(-int(diff[i]), 0)):
                    self.add(i)
        diff = np.bincount(self.G.ravel(), minlength=n + 1) - self.T
        diff[0] = 0
        return int(np.abs(diff).sum())


def smooth(G, T, rounds=2):
    """Cells whose four neighbours all share another label take that label
    (removes specks and pinholes; tiny countries are left alone)."""
    for _ in range(rounds):
        P = np.pad(G, 1, mode="edge")
        up, dn, lf, rt = P[:-2, 1:-1], P[2:, 1:-1], P[1:-1, :-2], P[1:-1, 2:]
        same = (up == dn) & (up == lf) & (up == rt) & (up != G)
        protect = (G > 0) & (T[G] <= 3)
        G = np.where(same & ~protect, up, G)
    return G


def drop_slivers(G, T):
    """Thin strings of squares (what is left of sparse areas such as the Arctic
    islands after the cartogram squeezes them) go back to the sea; the balancing
    step then puts them back next to the country's main body. Countries that are
    thin by nature (Chile, Norway...) are left alone."""
    a = G[:-1, :-1]
    blk = (a > 0) & (a == G[1:, :-1]) & (a == G[:-1, 1:]) & (a == G[1:, 1:])
    cov = np.zeros(G.shape, bool)
    cov[:-1, :-1] |= blk
    cov[1:, :-1] |= blk
    cov[:-1, 1:] |= blk
    cov[1:, 1:] |= blk
    n = len(T) - 1
    total = np.bincount(G.ravel(), minlength=n + 1)
    solid = np.bincount(G[cov].ravel(), minlength=n + 1)
    ok = (T >= 20) & (solid >= 0.5 * total)
    ok[0] = False
    G = G.copy()
    G[(G > 0) & ~cov & ok[G]] = 0
    return G


def drop_specks(G, T):
    """Small detached bits of larger countries (one or two squares out at sea)
    go back to the sea; the balancing step re-adds them by the main body."""
    G = G.copy()
    for i in np.unique(G):
        if i == 0 or T[i] < 10:
            continue
        lbl, k = cc_label(G == i)
        if k <= 1:
            continue
        sizes = np.bincount(lbl.ravel())[1:]
        small = sizes <= max(2, 0.02 * T[i])
        small[np.argmax(sizes)] = False
        if small.any():
            G[np.isin(lbl, np.nonzero(small)[0] + 1)] = 0
    return G


def fill_holes(G, max_size=8):
    """Small pockets of sea enclosed by land take the most common neighbouring country."""
    G = G.copy()
    lbl, k = cc_label(G == 0)
    edge = set(np.unique(np.r_[lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]]).tolist())
    sizes = np.bincount(lbl.ravel())
    for c in range(1, k + 1):
        if c in edge or sizes[c] > max_size:
            continue
        hole = lbl == c
        around = G[neighbours(hole)]
        around = around[around > 0]
        if len(around):
            G[hole] = np.bincount(around).argmax()
    return G


class Grower:
    """Adds or removes squares on an existing layout while keeping its shapes.

    A country grows into the sea next to it, preferring hollows of its own
    outline and avoiding cells that would close a strait against another
    country. A country without a suitable coast pushes a line of its
    neighbours' squares one step towards the sea, along the shortest route.
    Shrinking works the other way round, trimming the most exposed coast.
    Countries take turns, one square each per round, so the change is spread out."""

    def __init__(self, G, T, centre):
        self.G, self.T, self.c = G, T, centre
        self.H, self.W = G.shape

    def _n8(self, y, x, q):
        G = self.G
        own = other = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    v = G[y + dy, x + dx]
                    if v == q:
                        own += 1
                    elif v:
                        other += 1
        return own, other

    def _search(self, i, grow):
        """Breadth-first search from country i through other countries' land.
        grow: candidates are sea cells; otherwise coastal land cells.
        Returns [(depth, cell)] for the depths dmin..dmin+2, and the parent map."""
        G, H, W = self.G, self.H, self.W
        prev = {}
        frontier = []
        ys, xs = np.nonzero(G == i)
        if grow:
            for y, x in zip(ys.tolist(), xs.tolist()):
                for dy, dx in N4:
                    nb = (y + dy, x + dx)
                    if nb not in prev and G[nb] != i:
                        prev[nb] = ("S", (y, x))
                        frontier.append(nb)
            depth = 1
        else:
            for c in zip(ys.tolist(), xs.tolist()):
                prev[c] = None
                frontier.append(c)
            depth = 0
        cands, dmin = [], None
        while frontier:
            nxt = []
            for c in frontier:
                y, x = c
                if grow and G[c] == 0:
                    if 2 <= y < H - 2 and 2 <= x < W - 2:
                        cands.append((depth, c))
                        dmin = depth if dmin is None else dmin
                    continue
                if not grow and any(G[y + dy, x + dx] == 0 for dy, dx in N4):
                    cands.append((depth, c))
                    dmin = depth if dmin is None else dmin
                for dy, dx in N4:
                    nb = (y + dy, x + dx)
                    if nb in prev or G[nb] == i or not (1 <= nb[0] < H - 1 and 1 <= nb[1] < W - 1):
                        continue
                    if not grow and G[nb] == 0:
                        continue
                    prev[nb] = c
                    nxt.append(nb)
            if dmin is not None and depth >= dmin + 2:
                break
            frontier = nxt
            depth += 1
        return cands, prev

    # --- chains of neighbours: a country without a good coast takes a square
    # from a neighbour along their border, the neighbour from the next one, and
    # so on until a country that can grow into (or give up) the sea.
    def _chain(self, i):
        G = self.G
        a, b = G[:, :-1], G[:, 1:]
        c, d = G[:-1, :], G[1:, :]
        adj = collections.defaultdict(set)
        for p, q in (a, b), (c, d):
            m = (p != q) & (p > 0) & (q > 0)
            for u, v in set(zip(p[m].tolist(), q[m].tolist())):
                adj[u].add(v)
                adj[v].add(u)
        coast = neighbours(G == 0) & (G > 0)
        size = np.bincount(G.ravel(), minlength=len(self.T))
        cy, cx = self.c[i]
        dist = {i: 0.0}
        prev = {i: None}
        heap = [(0.0, int(i))]
        best = None
        import heapq
        while heap:
            dcur, x = heapq.heappop(heap)
            if dcur > dist.get(x, math.inf) or (best and dcur >= best[0]):
                continue
            ys, xs = np.nonzero(coast & (G == x))
            if len(ys):
                end = dcur + 0.25 * float(np.min(np.hypot(ys - cy, xs - cx))) / 4
                if best is None or end < best[0]:
                    best = (end, x)
            for y in adj[x]:
                if size[y] <= 2:
                    continue
                nd = dcur + 1.0 + 8.0 / size[y]
                if nd < dist.get(y, math.inf):
                    dist[y] = nd
                    prev[y] = x
                    heapq.heappush(heap, (nd, y))
        if best is None:
            return None
        chain = [best[1]]
        while prev[chain[-1]] is not None:
            chain.append(prev[chain[-1]])
        return chain[::-1]

    def _transfer(self, giver, taker):
        """taker gets the giver's border square that keeps both shapes most compact."""
        G = self.G
        cand = (G == giver) & neighbours(G == taker)
        ys, xs = np.nonzero(cand)
        if len(ys) == 0:
            return False
        best = None
        for y, x in zip(ys.tolist(), xs.tolist()):
            t, _ = self._n8(y, x, taker)
            g, _ = self._n8(y, x, giver)
            score = t - 0.7 * g
            if best is None or score > best[0]:
                best = (score, (y, x))
        G[best[1]] = taker
        return True

    def _sea_cell(self, x, i, grow):
        """Best sea square for country x to take (grow) or best coastal square to give up."""
        G = self.G
        cy, cx = self.c[i]
        if grow:
            cand = neighbours(G == x) & (G == 0)
            cand[:2] = cand[-2:] = False
            cand[:, :2] = cand[:, -2:] = False
        else:
            cand = (G == x) & neighbours(G == 0)
        ys, xs = np.nonzero(cand)
        if len(ys) == 0:
            return None
        best = None
        for y, xx in zip(ys.tolist(), xs.tolist()):
            own, other = self._n8(y, xx, x)
            if grow:
                score = -0.6 * own + 1.2 * other + 0.08 * math.hypot(y - cy, xx - cx)
            else:
                score = 0.5 * own + 0.3 * other - 0.03 * math.hypot(y - cy, xx - cx)
            if best is None or score < best[0]:
                best = (score, (y, xx))
        return best[1]

    def add(self, i):
        G = self.G
        if not (G == i).any():
            return False
        chain = self._chain(i)
        if chain:
            cell = self._sea_cell(chain[-1], i, True)
            if cell is not None:
                G[cell] = chain[-1]
                for taker, giver in reversed(list(zip(chain[:-1], chain[1:]))):
                    self._transfer(giver, taker)
                return True
        return self._add_path(i)

    def remove(self, i):
        G = self.G
        chain = self._chain(i)
        if chain:
            cell = self._sea_cell(chain[-1], i, False)
            if cell is not None:
                G[cell] = 0
                for giver, taker in reversed(list(zip(chain[:-1], chain[1:]))):
                    self._transfer(giver, taker)
                return True
        return self._remove_path(i)

    def _add_path(self, i):
        G = self.G
        cands, prev = self._search(i, True)
        if not cands:
            return False
        cy, cx = self.c[i]
        best = None
        for d, (y, x) in cands:
            p = prev[(y, x)]
            q = i if p[0] == "S" else G[p]
            own, other = self._n8(y, x, q)
            score = 1.5 * d + 0.08 * math.hypot(y - cy, x - cx) - 0.6 * own + 1.2 * other
            if best is None or score < best[0]:
                best = (score, (y, x))
        path = [best[1]]
        while prev[path[-1]][0] != "S":
            path.append(prev[path[-1]])
        path.reverse()
        for j in range(len(path) - 1, 0, -1):
            G[path[j]] = G[path[j - 1]]
        G[path[0]] = i
        return True

    def _remove_path(self, i):
        G = self.G
        cands, prev = self._search(i, False)
        if not cands:
            return False
        cy, cx = self.c[i]
        best = None
        for d, (y, x) in cands:
            own, other = self._n8(y, x, G[y, x])
            score = 1.5 * d + 0.5 * own + 0.3 * other - 0.03 * math.hypot(y - cy, x - cx)
            if best is None or score < best[0]:
                best = (score, (y, x))
        path = [best[1]]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        path.reverse()
        for j in range(len(path) - 1):
            G[path[j]] = G[path[j + 1]]
        G[path[-1]] = 0
        return True

    def run(self):
        n = len(self.T) - 1
        for grow in (False, True):
            for _ in range(5000):
                diff = np.bincount(self.G.ravel(), minlength=n + 1) - self.T
                diff[0] = 0
                todo = np.nonzero(diff < 0 if grow else diff > 0)[0]
                if len(todo) == 0:
                    break
                moved = False
                for i in todo[np.argsort(-np.abs(diff[todo]))]:
                    moved |= self.add(i) if grow else self.remove(i)
                if not moved:
                    break
        diff = np.bincount(self.G.ravel(), minlength=n + 1) - self.T
        diff[0] = 0
        return int(np.abs(diff).sum())


def load_base_2018(cache, offline):
    path = cached(BASE2018_URL, os.path.join(cache, "roser_2018_cells.csv"), offline)
    with open(path, encoding="utf-8", newline="") as f:
        rows = [(int(r["X"]), int(r["Y"]), int(r["CountryCode"])) for r in csv.DictReader(f)]
    return rows


def layout_2018(units, pops, per_cell, cache, offline, margin=14):
    """Start from the 2018 layout and change each country by the difference
    between its 2018 squares and round(population / per_cell)."""
    n = len(units)
    rows = load_base_2018(cache, offline)
    num_to_idx = {}
    for k, u in enumerate(units):
        for num in u.get("nums", ()):
            num_to_idx.setdefault(num, k + 1)
    xmax = max(r[0] for r in rows)
    ymax = max(r[1] for r in rows)
    G = np.zeros((ymax + 1, xmax + 1), np.int64)
    unknown = collections.Counter()
    for x, y, num in rows:
        k = num_to_idx.get(num)
        if k is None:
            unknown[num] += 1
        else:
            G[ymax - y, x] = k
    if unknown:
        log(f"  2018 codes without population data (left as sea): {dict(unknown)}")
    G = np.pad(G, margin)
    T = np.rint(pops / per_cell).astype(int)
    T[0] = 0
    C = np.bincount(G.ravel(), minlength=n + 1)
    yy, xx = np.nonzero(G)
    with np.errstate(invalid="ignore", divide="ignore"):
        centre = np.c_[np.bincount(G[yy, xx], weights=yy, minlength=n + 1) / C,
                       np.bincount(G[yy, xx], weights=xx, minlength=n + 1) / C]

    # countries that need squares but had none in 2018: place a seed where their
    # nearest mapped neighbours suggest
    have = [k for k in range(1, n + 1) if C[k] > 0]
    lon = np.radians([units[k - 1]["label"][0] for k in have])
    lat = np.radians([units[k - 1]["label"][1] for k in have])
    for k in range(1, n + 1):
        if C[k] > 0 or T[k] == 0:
            continue
        lo, la = np.radians(units[k - 1]["label"])
        d = np.arccos(np.clip(np.sin(la) * np.sin(lat) + np.cos(la) * np.cos(lat) * np.cos(lon - lo), -1, 1))
        near = np.argsort(d)[:4]
        w = 1 / (d[near] + 1e-3)
        pos = (centre[[have[j] for j in near]] * w[:, None]).sum(0) / w.sum()
        centre[k] = pos
        ok = (G == 0) | (T[G] >= 5)
        dist = np.where(ok, (np.arange(G.shape[0])[:, None] - pos[0]) ** 2
                        + (np.arange(G.shape[1])[None, :] - pos[1]) ** 2, np.inf)
        G[np.unravel_index(np.argmin(dist), G.shape)] = k
        log(f"  {units[k - 1]['name']}: not in the 2018 layout, seeded near "
            f"{', '.join(units[have[j] - 1]['name'] for j in near[:2])}")
    C = np.bincount(G.ravel(), minlength=n + 1)
    gain, loss = int(np.clip(T - C, 0, None)[1:].sum()), int(np.clip(C - T, 0, None)[1:].sum())
    log(f"  2018 layout: {int(C[1:].sum())} squares; adding {gain}, removing {loss}")
    grower = Grower(G, T, centre)
    left = grower.run()
    # large changes (projections far ahead) can leave pockets of sea inside the
    # land: fill them, then let the coasts give back the extra squares
    for _ in range(3):
        filled = fill_holes(G, max_size=40)
        if (filled == G).all():
            break
        G[:] = filled
        left = grower.run()
    if left:
        log(f"  warning: {left} squares could not be placed")
    rows_ = np.nonzero((G > 0).any(axis=1))[0]
    cols = np.nonzero((G > 0).any(axis=0))[0]
    r0, c0 = max(rows_[0] - 3, 0), max(cols[0] - 3, 0)
    G = G[r0:rows_[-1] + 4, c0:cols[-1] + 4]
    C = np.bincount(G.ravel(), minlength=n + 1)
    yy, xx = np.nonzero(G)
    with np.errstate(invalid="ignore", divide="ignore"):
        centre = np.c_[np.bincount(G[yy, xx], weights=yy, minlength=n + 1) / C,
                       np.bincount(G[yy, xx], weights=xx, minlength=n + 1) / C]
    return G, T, centre


def main_part(mask):
    """The largest 4-connected piece of mask."""
    lbl, k = cc_label(mask)
    if k <= 1:
        return mask
    return lbl == np.argmax(np.bincount(lbl.ravel())[1:]) + 1


def make_grid(lab, pix, y, x, pops, per_cell, rho_bar):
    n = len(pops) - 1
    flat = lab.ravel()
    T = np.rint(pops / per_cell).astype(int)
    T[0] = 0
    target = int(T.sum())
    tree = cKDTree(np.c_[y, x])
    land = flat > 0
    s = math.sqrt(per_cell / rho_bar)
    ly, lx = y[land], x[land]
    for it in range(4):
        y0, y1 = ly.min() - 3 * s, ly.max() + 3 * s
        x0, x1 = lx.min() - 3 * s, lx.max() + 3 * s
        gh, gw = int(math.ceil((y1 - y0) / s)), int(math.ceil((x1 - x0) / s))
        gy, gx = np.mgrid[0:gh, 0:gw]
        _, nn = tree.query(np.c_[y0 + (gy.ravel() + 0.5) * s, x0 + (gx.ravel() + 0.5) * s], workers=-1)
        G = flat[nn].reshape(gh, gw)
        have = int((T[G] > 0).sum())
        if it == 3 or abs(have / target - 1) < 0.01:
            break
        s *= math.sqrt(have / target)
    log(f"  grid {gw}x{gh}, {have} land squares before balancing (target {target})")
    # weighted centre of every country, in grid cells
    wsum = np.bincount(flat, weights=pix.ravel(), minlength=n + 1)
    cy = np.bincount(flat, weights=pix.ravel() * y, minlength=n + 1)
    cx = np.bincount(flat, weights=pix.ravel() * x, minlength=n + 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        centre = np.c_[(cy / wsum - y0) / s - 0.5, (cx / wsum - x0) / s - 0.5]
    G = fill_holes(drop_specks(drop_slivers(smooth(G, T), T), T))
    left = Balancer(G, T, centre).run()
    G = fill_holes(G)
    left += Balancer(G, T, centre).run()
    if left:
        log(f"  warning: {left} squares could not be balanced")
    # crop to the land with a margin of 2 squares
    rows = np.nonzero((G > 0).any(axis=1))[0]
    cols = np.nonzero((G > 0).any(axis=0))[0]
    r0, c0 = max(rows[0] - 2, 0), max(cols[0] - 2, 0)
    G = np.pad(G[r0:rows[-1] + 3, c0:cols[-1] + 3], 2)
    centre = centre - [r0 - 2, c0 - 2]
    return G, T, centre


# ---------------------------------------------------------------------------
# Outlines (--style outlines): the country borders moved with the cartogram
# ---------------------------------------------------------------------------
def densify(pts, step):
    """The closed ring pts (N x 2) with extra points every `step`, so long straight
    edges bend smoothly once deformed."""
    d = np.roll(pts, -1, axis=0) - pts
    k = np.maximum(np.ceil(np.hypot(d[:, 0], d[:, 1]) / step).astype(int), 1)
    idx = np.repeat(np.arange(len(pts)), k)
    t = (np.arange(k.sum()) - np.repeat(np.cumsum(k) - k, k)) / np.repeat(k, k)
    return pts[idx] + d[idx] * t[:, None]


def rubber_sheet(shapes, target, extra, iterations=15):
    """Dougenik, Chrisman & Niemeyer (1985): every part of a country pushes the points
    around it outwards (too small) or pulls them in (too big), so that country i's area
    approaches target[i]. Shared border points move together, so borders stay joined.
    Moves the rings in `shapes` in place and returns the moved `extra` points (N x 2)."""
    rings, owner = [], []                    # owner: (country, polygon) of every ring, outer ring first
    for i, polys in shapes:
        for k, poly in enumerate(polys):
            for j, ring in enumerate(poly):
                rings.append(ring)
                owner.append((i, k, j == 0))
    cuts = np.cumsum([len(r) for r in rings])[:-1]
    P = np.vstack(rings + [extra])
    n_ring_pts = len(P) - len(extra)
    for it in range(iterations):
        parts = {}                           # (country, polygon) -> [area, sx, sy]
        for (i, k, outer), ring in zip(owner, np.split(P[:n_ring_pts], cuts)):
            x, y = ring[:, 0], ring[:, 1]
            xn, yn = np.roll(x, -1), np.roll(y, -1)
            cross = x * yn - xn * y
            a = cross.sum() / 2
            e = parts.setdefault((i, k), [0.0, 0.0, 0.0])
            if outer:
                e[0] += abs(a)
                if abs(a) > 1e-9:
                    e[1] += ((x + xn) * cross).sum() / (6 * a) * abs(a)
                    e[2] += ((y + yn) * cross).sum() / (6 * a) * abs(a)
            else:
                e[0] -= abs(a)
        country = collections.defaultdict(float)
        for (i, k), (a, _, _) in parts.items():
            country[i] += a
        errs = [abs(country[i] - target[i]) / target[i] for i in country if target[i] > 0]
        force = 1 / (1 + np.mean(errs))
        move = np.zeros_like(P)
        with np.errstate(divide="ignore", invalid="ignore"):
            for (i, k), (a, sx, sy) in parts.items():
                if a < 0.02 or country[i] <= 0:
                    continue
                want = a * target[i] / country[i]
                r = math.sqrt(a / math.pi)
                mass = math.sqrt(want / math.pi) - r
                if abs(mass) < 1e-3:
                    continue
                c = np.array([sx, sy]) / a
                d = P - c
                dist = np.hypot(d[:, 0], d[:, 1])
                f = np.where(dist > r, mass * r / dist, mass * (dist / r) ** 2 * (4 - 3 * dist / r))
                move += np.nan_to_num(d * (force * f / dist)[:, None])
        P += move
    for ring, new in zip(rings, np.split(P[:n_ring_pts], cuts)):
        ring[:] = new
    return P[n_ring_pts:]


def layout_outlines(units, frame, lab, pix, y, x, pops, per_cell, rho_bar):
    """Move every border vertex with the same deformation as the raster pixels.
    Coordinates are in grid cells of per_cell people, like the square map, so the
    labels and boxes are placed the same way. Returns G (the outlines rasterised on
    that grid: labels, colouring, free sea), T, the label centres and a list of
    (unit index, polygons), largest first; a polygon is a list of rings (N x 2, x y)."""
    n = len(units)
    h, w = lab.shape
    Y, X = y.reshape(h, w), x.reshape(h, w)
    s = math.sqrt(per_cell / rho_bar)
    moved = []
    for k, u in enumerate(units):
        polys = []
        for poly in u["polys"]:
            rings = []
            for ring in poly:
                a = np.asarray(ring, float)
                c, r = frame.px(a[:, 0], a[:, 1])
                p = densify(np.c_[c, r], 1.0)
                q = np.vstack([p[:, 1] - 0.5, p[:, 0] - 0.5])      # pixel centres are at +0.5
                rings.append(np.c_[map_coordinates(X, q, order=1, mode="nearest"),
                                   map_coordinates(Y, q, order=1, mode="nearest")])
            polys.append(rings)
        moved.append((k + 1, polys))
    outer = np.vstack([poly[0] for _, polys in moved for poly in polys])
    x0, y0 = outer.min(axis=0) - 3 * s
    x1, y1 = outer.max(axis=0) + 3 * s
    gw, gh = int(math.ceil((x1 - x0) / s)), int(math.ceil((y1 - y0) / s))
    shapes = [(i, [[(ring - [x0, y0]) / s for ring in poly] for poly in polys]) for i, polys in moved]
    flat = lab.ravel()
    wsum = np.bincount(flat, weights=pix.ravel(), minlength=n + 1)
    cy = np.bincount(flat, weights=pix.ravel() * y, minlength=n + 1)
    cx = np.bincount(flat, weights=pix.ravel() * x, minlength=n + 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        labels = np.c_[(cx / wsum - x0) / s, (cy / wsum - y0) / s]

    def areas():
        a = np.zeros(n + 1)
        for i, polys in shapes:
            for poly in polys:
                a[i] += ring_area(*poly[0].T) - sum(ring_area(*hole.T) for hole in poly[1:])
        return a

    def report(a, what):
        big = pops[1:] > 20 * per_cell
        err = np.abs(a[1:][big] / (pops[1:][big] / per_cell) - 1)
        log(f"  areas {what}: median error {np.median(err):.1%}, 90th pct {np.percentile(err, 90):.1%} "
            f"(countries over {fmt_num(20 * per_cell / 1e6, 0, TEXTS['en'])} million)")

    report(areas(), "after the diffusion")
    labels = rubber_sheet(shapes, pops / per_cell, labels)
    area = areas()
    report(area, "after the correction")
    shapes.sort(key=lambda e: -area[e[0]])
    centre = labels[:, ::-1] - 0.5

    T = np.rint(pops / per_cell).astype(int)
    T[0] = 0

    # the outlines on the grid: cell (r, c) covers [c, c + 1) x [r, r + 1)
    img = Image.new("I", (gw, gh), 0)
    draw = ImageDraw.Draw(img)
    for i, polys in shapes:
        for poly in polys:
            if len(poly[0]) >= 3:
                draw.polygon([tuple(p) for p in (poly[0] - 0.5).tolist()], fill=i)
            for hole in poly[1:]:
                if len(hole) >= 3:
                    draw.polygon([tuple(p) for p in (hole - 0.5).tolist()], fill=0)
    G = np.array(img, dtype=np.int32)
    present = np.bincount(G.ravel(), minlength=n + 1)
    for i in range(1, n + 1):          # too thin to cover a cell: one cell for the label
        if present[i] == 0 and T[i] > 0 and np.isfinite(centre[i]).all():
            r, c = np.clip(np.rint(centre[i]).astype(int), 0, [gh - 1, gw - 1])
            G[r, c] = i
    log(f"  {gw}x{gh} cells of {fmt_num(per_cell, 0, TEXTS['en'])} people")
    return G, T, centre, shapes


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
_FONT_PATHS = {}


def font(size, bold=False):
    size = max(int(round(size)), 6)
    if bold not in _FONT_PATHS:
        _FONT_PATHS[bold] = None
        cands = list(FONT_FILES[bold])
        try:
            import matplotlib
            cands.append(os.path.join(matplotlib.get_data_path(), "fonts", "ttf",
                                      "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"))
        except ImportError:
            pass
        for c in cands:
            try:
                ImageFont.truetype(c, 12)
                _FONT_PATHS[bold] = c
                break
            except OSError:
                continue
    if _FONT_PATHS[bold] is None:
        return ImageFont.load_default(size)
    return ImageFont.truetype(_FONT_PATHS[bold], size)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def luminance(rgb):
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def mix(c1, c2, t):
    """Blend two '#rrggbb' colours: t = 0 gives c1, t = 1 gives c2."""
    a, b = np.array(hex_rgb(c1), float), np.array(hex_rgb(c2), float)
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in a + (b - a) * t)


def rgb_to_lab(rgb):
    """sRGB (0-255, last axis) to CIELAB (D65)."""
    c = np.asarray(rgb, float) / 255
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def delta_e(lab1, lab2):
    """CIEDE2000 colour difference (about 1 = just noticeable, 10+ = clearly different)."""
    L1, a1, b1 = np.moveaxis(np.asarray(lab1, float), -1, 0)
    L2, a2, b2 = np.moveaxis(np.asarray(lab2, float), -1, 0)
    cb = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(cb ** 7 / (cb ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dl, dc = L2 - L1, c2p - c1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    zero = c1p * c2p == 0
    dh = np.where(zero, 0, dh)
    dhh = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    lb, cbp = (L1 + L2) / 2, (c1p + c2p) / 2
    hs = h1p + h2p
    hb = np.where(zero, hs, np.where(np.abs(h1p - h2p) <= 180, hs / 2,
                                     np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)))
    t = (1 - 0.17 * np.cos(np.radians(hb - 30)) + 0.24 * np.cos(np.radians(2 * hb))
         + 0.32 * np.cos(np.radians(3 * hb + 6)) - 0.20 * np.cos(np.radians(4 * hb - 63)))
    rt = (-np.sin(np.radians(60 * np.exp(-(((hb - 275) / 25) ** 2))))
          * 2 * np.sqrt(cbp ** 7 / (cbp ** 7 + 25.0 ** 7)))
    sl = 1 + 0.015 * (lb - 50) ** 2 / np.sqrt(20 + (lb - 50) ** 2)
    sc, sh = 1 + 0.045 * cbp, 1 + 0.015 * cbp * t
    return np.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dhh / sh) ** 2 + rt * (dc / sc) * (dhh / sh))


def contrast_ratio(c1, c2):
    """WCAG contrast ratio between two '#rrggbb' colours (4.5+ reads well)."""
    def rel(c):
        v = np.array(hex_rgb(c), float) / 255
        v = np.where(v <= 0.03928, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
    hi, lo = sorted((rel(c1), rel(c2)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def colour_countries(G, n, palette):
    """Give every country a palette index so that neighbours look as different as
    possible: each country takes the colour whose smallest CIEDE2000 difference to
    its neighbours is largest. Touching countries count fully; countries up to 3
    squares apart count with a bonus of NEAR_BONUS (they matter less). Above CAP
    two colours are 'different enough' and the least used colour wins, so the
    palette is spread evenly. A few passes then revisit every choice."""
    CAP, NEAR_BONUS = 40.0, 15.0
    H, W = G.shape
    touch = [set() for _ in range(n + 1)]
    near = [set() for _ in range(n + 1)]
    for dy in range(0, 4):
        for dx in range(-3, 4):
            if dy == 0 and dx <= 0:
                continue
            a = G[0:H - dy, max(0, -dx):W - max(0, dx)]
            b = G[dy:H, max(0, dx):W - max(0, -dx)]
            m = (a > 0) & (b > 0) & (a != b)
            target = touch if max(dy, abs(dx)) == 1 else near
            for p, q in set(zip(a[m].tolist(), b[m].tolist())):
                target[p].add(q)
                target[q].add(p)
    for i in range(n + 1):
        near[i] -= touch[i]
    lab = rgb_to_lab(np.array([hex_rgb(c) for c in palette]))
    D = delta_e(lab[:, None, :], lab[None, :, :])
    k = len(palette)
    size = np.bincount(G.ravel(), minlength=n + 1)
    colour = np.full(n + 1, -1)
    used = np.zeros(k, int)

    def score(i):
        s = np.full(k, CAP)
        for j in touch[i]:
            if colour[j] >= 0:
                s = np.minimum(s, D[:, colour[j]])
        for j in near[i]:
            if colour[j] >= 0:
                s = np.minimum(s, D[:, colour[j]] + NEAR_BONUS)
        return s

    order = [i for i in sorted(range(1, n + 1), key=lambda i: (-len(touch[i]), -len(near[i]), -size[i]))
             if size[i] > 0]
    for i in order:
        s = score(i)
        c = int(np.lexsort((np.arange(k), used, -s))[0])
        colour[i] = c
        used[c] += 1
    for _ in range(4):
        changed = False
        for i in order:
            s = score(i)
            c = int(np.lexsort((np.arange(k), used, -s))[0])
            if s[c] > s[colour[i]] + 0.5:
                used[colour[i]] -= 1
                colour[i] = c
                used[c] += 1
                changed = True
        if not changed:
            break
    return colour


def neighbour_contrast(G, n, palette, colour):
    """Smallest CIEDE2000 difference between two touching countries, and that pair."""
    lab = rgb_to_lab(np.array([hex_rgb(c) for c in palette]))
    H, W = G.shape
    worst = (math.inf, None)
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        a = G[0:H - dy, max(0, -dx):W - max(0, dx)]
        b = G[dy:H, max(0, dx):W - max(0, -dx)]
        m = (a > 0) & (b > 0) & (a != b)
        for p, q in set(zip(a[m].tolist(), b[m].tolist())):
            d = float(delta_e(lab[colour[p]], lab[colour[q]]))
            if d < worst[0]:
                worst = (d, (p, q))
    return worst


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------
def load_palettes(path):
    """Read and check the palette file. Returns (palettes, default name)."""
    if path is None:
        path = PALETTES_FILE
        if not os.path.exists(path):
            log(f"  {path} not found; using the built-in palette")
            return BUILTIN_PALETTES["palettes"], BUILTIN_PALETTES["default"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"cannot read palette file {path}: {e}")
    pals = data.get("palettes") or {}
    problems = []

    def is_hex(v):
        return isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(
            ch in "0123456789abcdefABCDEF" for ch in v[1:])

    for name, p in pals.items():
        for mode in ("light", "dark"):
            m = p.get(mode)
            if not isinstance(m, dict):
                problems.append(f"{name}: missing the '{mode}' palette")
                continue
            for key in ("background", "box_text", "box_fill"):
                if not is_hex(m.get(key)):
                    problems.append(f"{name}.{mode}.{key}: expected a colour like '#1a2b3c'")
            cs = m.get("countries")
            if not isinstance(cs, list) or not MIN_COUNTRY_COLOURS <= len(cs) <= MAX_COUNTRY_COLOURS:
                problems.append(f"{name}.{mode}.countries: needs {MIN_COUNTRY_COLOURS} to "
                                f"{MAX_COUNTRY_COLOURS} colours")
            else:
                problems += [f"{name}.{mode}.countries[{k}]: '{c}' is not a colour like '#1a2b3c'"
                             for k, c in enumerate(cs) if not is_hex(c)]
            for ramp in ("sequential", "diverging"):          # optional, for --color-by
                r = m.get(ramp)
                if r is None:
                    continue
                if not isinstance(r, list) or len(r) < 3 or not all(is_hex(c) for c in r):
                    problems.append(f"{name}.{mode}.{ramp}: needs at least 3 colours like '#1a2b3c'")
                elif ramp == "diverging" and len(r) % 2 == 0:
                    problems.append(f"{name}.{mode}.diverging: needs an odd number of colours "
                                    "(the middle one is 'no change')")
    default = data.get("default")
    if not pals:
        problems.append("no palettes defined")
    elif default not in pals:
        problems.append(f"default palette '{default}' is not defined")
    if problems:
        sys.exit(f"palette file {path}:\n  " + "\n  ".join(problems))
    return pals, default


def palette_warnings(name, mode, m):
    """Pairs of colours that look alike and hard-to-read box text."""
    out = []
    cs = [c.lower() for c in m["countries"]]
    lab = rgb_to_lab(np.array([hex_rgb(c) for c in cs]))
    D = delta_e(lab[:, None, :], lab[None, :, :])
    for a in range(len(cs)):
        for b in range(a + 1, len(cs)):
            if D[a, b] < SIMILAR_DE:
                out.append(f"{name}/{mode}: {cs[a]} and {cs[b]} look alike (difference {D[a, b]:.1f})")
    bg = rgb_to_lab(np.array(hex_rgb(m["background"])))
    for c, l in zip(cs, lab):
        d = float(delta_e(l, bg))
        if d < SIMILAR_DE:
            out.append(f"{name}/{mode}: {c} is close to the background (difference {d:.1f})")
    r = contrast_ratio(m["box_text"], m["box_fill"])
    if r < 4.5:
        out.append(f"{name}/{mode}: box text on box fill has low contrast ({r:.1f}:1, 4.5 or more reads well)")
    return out


def palette_summary(pals, default):
    lines = []
    for name, p in pals.items():
        mark = " (default)" if name == default else ""
        lines.append(f"{name}{mark}: {p.get('description', '')}")
        for mode in ("light", "dark"):
            m = p[mode]
            lab = rgb_to_lab(np.array([hex_rgb(c) for c in m["countries"]]))
            D = delta_e(lab[:, None, :], lab[None, :, :])
            D[np.diag_indices_from(D)] = np.inf
            lines.append(f"    {mode:5}: {len(m['countries']):2} colours, background {m['background']}, "
                         f"closest pair {D.min():.1f}")
    return "\n".join(lines)


def make_theme(pal, mode):
    """Everything render() needs, derived from one palette entry (light or dark)."""
    bg = pal["background"]
    dark_bg = luminance(hex_rgb(bg)) < 0.45
    label = "#f1f3f6" if dark_bg else "#141414"
    return {
        "mode": mode,
        "bg": bg,
        # light mode draws the sea as squares with a grid, like the 2015 map;
        # dark mode leaves it plain, like the 2018 map
        "ocean": bg if mode == "light" else None,
        "grid": (mix(bg, "#ffffff", 0.12) if dark_bg else mix(bg, "#000000", 0.10)) if mode == "light" else None,
        "land_gap": 0.22 if mode == "light" else None,
        "label": label,
        "lead": mix(label, bg, 0.25),
        "text": pal["box_text"],
        "box": pal["box_fill"],
        "box_edge": mix(pal["box_text"], pal["box_fill"], 0.45),
        "palette": [c.lower() for c in pal["countries"]],
        "ramps": {k: [c.lower() for c in pal.get(k) or DEFAULT_RAMPS[mode][k]]
                  for k in ("sequential", "diverging")},
        # lines between countries on thematic maps, where neighbours may share a colour
        "border": "#1b1b1b" if not dark_bg else "#0b0e13",
        # country outlines of --style outlines: pale on a light sea (like Worldmapper), the sea on a dark one
        "edge": mix(bg, "#ffffff", 0.75) if not dark_bg else mix(bg, "#000000", 0.15),
    }


def sample_ramp(stops, n):
    """n colours spread evenly along a list of colour stops."""
    if n == 1:
        return [stops[len(stops) // 2]]
    out = []
    for p in np.linspace(0, len(stops) - 1, n):
        i = min(int(p), len(stops) - 2)
        out.append(mix(stops[i], stops[i + 1], p - i))
    return out


def thematic_colours(spec, th):
    """One colour per legend class of a thematic map."""
    k = len(spec["labels"])
    if spec["kind"] == "categorical":
        pal = th["palette"]
        lab = rgb_to_lab(np.array([hex_rgb(c) for c in pal]))
        chosen = [0]
        while len(chosen) < min(k, len(pal)):
            d = np.min(delta_e(lab[:, None, :], lab[None, chosen, :]), axis=1)
            d[chosen] = -1
            chosen.append(int(np.argmax(d)))
        return [pal[chosen[j % len(chosen)]] for j in range(k)]
    ramp = th["ramps"][spec["ramp"]]
    if spec["ramp"] == "diverging":
        mid, nn = len(ramp) // 2, spec["neutral"]
        npos = k - nn - 1
        neg = sample_ramp(ramp[:mid + 1], nn + 1)[:nn] if nn else []
        pos = sample_ramp(ramp[mid:], npos + 1)[1:] if npos else []
        return neg + [ramp[mid]] + pos
    return sample_ramp(ramp, k)


def fmt_num(v, decimals, tx):
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "\0").replace(".", tx["dec"]).replace("\0", tx["sep"])


def fmt_pop(p, tx):
    if p >= 1e9:
        return tx["billion"].format(v=fmt_num(p / 1e9, 2, tx))
    if p >= 1e6:
        v = p / 1e6
        return tx["million"].format(v=fmt_num(v, 0 if v >= 100 else 1, tx))
    return tx["thousand"].format(v=fmt_num(p / 1e3, 0, tx))


def text_size(f, s):
    l, t, r, b = f.getbbox(s)
    asc, desc = f.getmetrics()
    return r - l, asc + desc


class Canvas:
    def __init__(self, G, cs, theme):
        self.G, self.cs, self.th = G, cs, theme
        self.H, self.W = G.shape
        self.occ = np.zeros(G.shape, bool)       # cells covered by labels and boxes

    def cells(self, x0, y0, x1, y1):
        cs = self.cs
        return (max(int(y0 // cs), 0), min(int(math.ceil(y1 / cs)), self.H),
                max(int(x0 // cs), 0), min(int(math.ceil(x1 / cs)), self.W))

    def inside(self, x0, y0, x1, y1):
        return x0 >= 2 and y0 >= 2 and x1 <= self.W * self.cs - 2 and y1 <= self.H * self.cs - 2

    def free_box(self, w, h, prefer, land_margin=1):
        """Top-left pixel of a w x h box over sea only, as close as possible to the
        `prefer` corner ("tl", "tr", "bl", "br"). None when there is no room."""
        cs = self.cs
        bh, bw = int(math.ceil(h / cs)) + 1, int(math.ceil(w / cs)) + 1
        blocked = self.G > 0
        for _ in range(land_margin):
            blocked = blocked | neighbours(blocked)
        blocked |= self.occ
        sat = np.pad(blocked.astype(int).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        if bh > self.H or bw > self.W:
            return None
        cnt = sat[bh:, bw:] - sat[:-bh, bw:] - sat[bh:, :-bw] + sat[:-bh, :-bw]
        ys, xs = np.nonzero(cnt == 0)
        if len(ys) == 0:
            return None
        ty = 0 if prefer[0] == "t" else self.H - bh
        tx = 0 if prefer[1] == "l" else self.W - bw
        k = np.argmin((ys - ty) ** 2 + (xs - tx) ** 2)
        return int(xs[k] * cs + cs // 2), int(ys[k] * cs + cs // 2)


def box_font(cs):
    """Title size: grows with the squares, but stays readable on regional maps."""
    return min(cs * 3.2, 80)


def layout_title(tx, info, per_cell, cs, region=None):
    """Lines of the title box: (text, font size, bold, swatch colour or None)."""
    big = box_font(cs)
    year, proj = info["year"], info["projected"]
    if region:
        title = tx["title_region_proj" if proj else "title_region"].format(region=region["name"], year=year)
    else:
        title = tx["title_proj" if proj else "title"].format(year=year)
    outlines = info.get("style") == "outlines"
    lines = [(title, big, True, None),
             (tx["area"] if outlines else tx["square"].format(n=fmt_num(per_cell, 0, tx)), big * 0.42, False, None)]
    if region:
        lines.append((tx["region_area" if outlines else "region_total"].format(cells=fmt_num(region["cells"], 0, tx),
                                                total=fmt_pop(region["pop"], tx),
                                                pct=fmt_num(100 * region["pop"] / info["world"], 1, tx)),
                      big * 0.42, False, None))
    else:
        lines.append((tx["area_world" if outlines else "squares"].format(cells=fmt_num(info["cells"], 0, tx),
                                                                         total=fmt_pop(info["world"], tx)),
                      big * 0.42, False, None))
        lines.append(("", big * 0.25, False, None))
        for cont, p in info["continents"]:
            name = tx["continents"].get(cont, cont)
            pct = fmt_num(100 * p / info["world"], 1, tx)
            lines.append((f"{name}: {fmt_pop(p, tx)} ({pct}%)", big * 0.38, False, None))
    lines.append(("", big * 0.25, False, None))
    if info["wb_years"]:
        un_names = region["owid"] if region else info["owid"]
        owid = tx["owid"].format(k=", ".join(un_names)) if un_names else ""
        key = "source_year" if info["year_given"] else "source"
        lines.append((tx[key].format(years=info["wb_years"], owid=owid), big * 0.3, False, None))
    else:
        lines.append((tx["source_un"], big * 0.3, False, None))
    method = "base" if info["layout"] == "2018" else "outline_map" if outlines else "map"
    lines.append((tx[method], big * 0.3, False, None))
    return lines


def legend_lines(tx, spec, colours, cs):
    big = box_font(cs)
    lines = [(tx["colour_by"].format(what=spec["what"]), big * 0.42, True, None)]
    for colour, label in zip(colours, spec["labels"]):
        lines.append((label, big * 0.38, False, colour))
    if spec.get("missing"):
        lines.append((tx["no_data"], big * 0.38, False, "missing"))
    return lines


def wrap_lines(lines, max_w):
    out = []
    for s, size, bold, sw in lines:
        f = font(size, bold)
        if not s or text_size(f, s)[0] <= max_w:
            out.append((s, size, bold, sw))
            continue
        cur = ""
        for word in s.split(" "):
            t = (cur + " " + word).strip()
            if cur and text_size(f, t)[0] > max_w:
                out.append((cur, size, bold, sw))
                cur, sw = word, None
            else:
                cur = t
        out.append((cur, size, bold, sw))
    return out


def block_size(lines):
    w = h = 0
    for s, size, bold, sw in lines:
        tw, th = text_size(font(size, bold), s or " ")
        w = max(w, tw + (size * 1.6 if sw else 0))
        h += th * 1.08
    return w, h


# ---------------------------------------------------------------------------
# Output back-ends: the same drawing calls produce a PNG/PDF or an SVG
# ---------------------------------------------------------------------------
def edge_width(cs):
    """Width of the country outlines (--style outlines), in output pixels."""
    return min(cs, 16) / 10


class RasterDrawer:
    SS = 3                                  # outlines are drawn this much larger, then reduced (antialiasing)

    def __init__(self, G, cs, fills, gaps, shapes=None, edge=None):
        H, W = G.shape
        if shapes is not None:
            ss = self.SS
            img = Image.new("RGB", (W * cs * ss, H * cs * ss), fills[0])
            d = ImageDraw.Draw(img)
            lw = max(1, round(edge_width(cs) * ss))
            for i, polys in shapes:
                for poly in polys:
                    rings = [[tuple(p) for p in (ring * ss).tolist()] for ring in poly]
                    if len(rings[0]) < 3:
                        continue
                    d.polygon(rings[0], fill=fills[i])
                    for hole in rings[1:]:
                        if len(hole) >= 3:
                            d.polygon(hole, fill=fills[0])
                    for ring in rings:
                        d.line(ring + ring[:1], fill=edge, width=lw, joint="curve")
            self.img = img.resize((W * cs, H * cs), Image.LANCZOS)
            self.draw = ImageDraw.Draw(self.img)
            return
        f = np.array([hex_rgb(c) for c in fills], np.uint8)
        g = np.array([hex_rgb(c) for c in gaps], np.uint8)
        big = np.repeat(np.repeat(f[G], cs, 0), cs, 1)
        gap = max(1, cs // 9)
        gm = ((np.arange(H * cs) % cs) >= cs - gap)[:, None] | ((np.arange(W * cs) % cs) >= cs - gap)[None, :]
        big[gm] = np.repeat(np.repeat(g[G], cs, 0), cs, 1)[gm]
        self.img = Image.fromarray(big, "RGB")
        self.draw = ImageDraw.Draw(self.img)

    def rect(self, box, fill, outline=None, width=0):
        self.draw.rectangle(box, fill=fill, outline=outline, width=width)

    def lines(self, segs, colour, width):
        for x0, y0, x1, y1 in segs:
            self.draw.line([(x0, y0), (x1, y1)], fill=colour, width=width)

    def text(self, xy, s, size, bold, fill, anchor="la", halo=None, halo_w=0):
        self.draw.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor,
                       stroke_width=halo_w if halo else 0, stroke_fill=halo)

    def save(self, path):
        if path.lower().endswith(".pdf"):
            self.img.save(path, "PDF", resolution=150)
        else:
            self.img.save(path)
        return self.img.width, self.img.height


class SvgDrawer:
    """Vector output. Every country is a <g> with its ISO code as id and its name
    and population as <title>, so the file can be restyled or reused."""
    FONT = "Segoe UI, Arial, Helvetica, sans-serif"

    def __init__(self, G, cs, fills, gaps, sea, grid, names, shapes=None, edge=None):
        H, W = G.shape
        self.w, self.h = W * cs, H * cs
        gap = max(1, cs // 9)
        s = cs - gap
        p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
             f'viewBox="0 0 {self.w} {self.h}">']
        if shapes is not None:
            p.append(f'<rect width="100%" height="100%" fill="{fills[0]}"/>')
            p.append(f'<g stroke="{edge}" stroke-width="{edge_width(cs):.2f}" stroke-linejoin="round">')
            for i, polys in shapes:
                d = []
                for poly in polys:
                    for ring in poly:
                        r = np.round(ring, 1)
                        keep = np.r_[True, (np.diff(r, axis=0) != 0).any(axis=1)]
                        pts = r[keep].tolist()
                        if len(pts) >= 3:
                            d.append("M" + "L".join(f"{x:g} {y:g}" for x, y in pts) + "Z")
                code, title = names[i]
                p.append(f'<g id="{xml_escape(code)}"><title>{xml_escape(title)}</title>'
                         f'<path fill="{fills[i]}" fill-rule="evenodd" d="{"".join(d)}"/></g>')
            p.append("</g>")
            self.parts = p
            return
        if grid:
            p.append(f'<defs><pattern id="sea" width="{cs}" height="{cs}" patternUnits="userSpaceOnUse">'
                     f'<rect width="{cs}" height="{cs}" fill="{grid}"/><rect width="{s}" height="{s}" '
                     f'fill="{sea}"/></pattern></defs><rect width="100%" height="100%" fill="url(#sea)"/>')
        else:
            p.append(f'<rect width="100%" height="100%" fill="{fills[0]}"/>')
        for i in np.unique(G[G > 0]).tolist():
            ys, xs = np.nonzero(G == i)
            code, title = names[i]
            p.append(f'<g id="{xml_escape(code)}"><title>{xml_escape(title)}</title>')
            if gaps[i] != fills[0] and gaps[i] != gaps[0]:
                d = "".join(f"M{x * cs} {y * cs}h{cs}v{cs}h-{cs}z" for y, x in zip(ys.tolist(), xs.tolist()))
                p.append(f'<path fill="{gaps[i]}" d="{d}"/>')
            d = "".join(f"M{x * cs} {y * cs}h{s}v{s}h-{s}z" for y, x in zip(ys.tolist(), xs.tolist()))
            p.append(f'<path fill="{fills[i]}" d="{d}"/></g>')
        self.parts = p

    def rect(self, box, fill, outline=None, width=0):
        x0, y0, x1, y1 = box
        stroke = f' stroke="{outline}" stroke-width="{width}"' if outline else ""
        self.parts.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" '
                          f'fill="{fill}"{stroke}/>')

    def lines(self, segs, colour, width):
        if segs:
            d = "".join(f"M{x0:.1f} {y0:.1f}L{x1:.1f} {y1:.1f}" for x0, y0, x1, y1 in segs)
            self.parts.append(f'<path d="{d}" stroke="{colour}" stroke-width="{width}" fill="none" '
                              f'stroke-linecap="square"/>')

    def text(self, xy, s, size, bold, fill, anchor="la", halo=None, halo_w=0):
        x, y = xy
        y += font(size, bold).getmetrics()[0]           # PIL anchors at the ascender, SVG at the baseline
        a = ' text-anchor="middle"' if anchor[0] == "m" else ""
        w = ' font-weight="bold"' if bold else ""
        h = (f' stroke="{halo}" stroke-width="{2 * halo_w}" paint-order="stroke" stroke-linejoin="round"'
             if halo else "")
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{self.FONT}" font-size="{size:.1f}px"'
                          f'{w}{a} fill="{fill}"{h}>{xml_escape(s)}</text>')

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.parts + ["</svg>"]))
        return self.w, self.h


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def border_segments(G, cs):
    """Line segments between squares of two different countries."""
    gap = max(1, cs // 9)
    off = cs - gap / 2
    segs = []
    a, b = G[:, :-1], G[:, 1:]
    for r, c in zip(*np.nonzero((a != b) & (a > 0) & (b > 0))):
        x = c * cs + off
        segs.append((x, r * cs - gap / 2, x, (r + 1) * cs - gap / 2))
    a, b = G[:-1, :], G[1:, :]
    for r, c in zip(*np.nonzero((a != b) & (a > 0) & (b > 0))):
        y = r * cs + off
        segs.append((c * cs - gap / 2, y, (c + 1) * cs - gap / 2, y))
    return segs


def render(layout, th, lang, cs, out_path, thematic=None, region=None):
    """Draw the cartogram. thematic: colour countries by a value (see
    thematic_spec); region: only label and list the countries of one region and
    fade the others."""
    G, T, centre, units, pops, info, per_cell = (layout[k] for k in
                                                 ("G", "T", "centre", "units", "pops", "info", "per_cell"))
    tx = TEXTS[lang]
    n = len(units)
    focus = region["members"] if region else None
    shapes = layout.get("shapes")
    off_r, off_c = layout.get("shape_off", (0, 0))

    def shown(i):
        return focus is None or i in focus

    # --- colours
    missing_colour = mix(th["bg"], "#9a9a9a", 0.55)
    if thematic:
        classes = thematic_colours(thematic, th)
        cls = thematic["cls"]
        colour_of = [None] + [classes[cls[i]] if cls[i] >= 0 else missing_colour for i in range(1, n + 1)]
    else:
        colour = colour_countries(G, n, th["palette"])
        worst, pair = neighbour_contrast(G, n, th["palette"], colour)
        if pair:
            log(f"  colours ({th['mode']}): closest touching pair {units[pair[0] - 1]['name']} / "
                f"{units[pair[1] - 1]['name']}, difference {worst:.1f}")
        colour_of = [None] + [th["palette"][max(colour[i], 0)] for i in range(1, n + 1)]
    for i in range(1, n + 1):
        if not shown(i):
            colour_of[i] = mix(colour_of[i], th["bg"], 0.72)

    def value_text(i):
        if thematic and thematic.get("value_text") is not None:
            return thematic["value_text"].get(i, "–")
        return fmt_pop(pops[i], tx)

    # --- title and "not on the map" boxes; grow the canvas when there is no room
    pad = min(cs, 25)
    title = wrap_lines(layout_title(tx, info, per_cell, cs, region), box_font(cs) * 22)
    legend = wrap_lines(legend_lines(tx, thematic, classes, cs), box_font(cs) * 22) if thematic else None
    small = [(units[k - 1]["name"], pops[k]) for k in range(1, n + 1)
             if T[k] == 0 and pops[k] > 0 and shown(k)]
    small.sort(key=lambda e: -e[1])
    small_head = tx["small" if shapes is None else "small_outline"].format(n=fmt_num(per_cell / 2, 0, tx))
    sfs = min(cs * 1.0, 26)
    entries = [f"{name} – {fmt_pop(p, tx)}" for name, p in small]

    def small_block(ncols):
        per = -(-len(entries) // ncols) if entries else 0
        cols = [entries[i * per:(i + 1) * per] for i in range(ncols)]
        widths = [max((text_size(font(sfs), e)[0] for e in c), default=0) for c in cols]
        hw, hh = text_size(font(sfs * 1.15, True), small_head)
        lh = text_size(font(sfs), "Ag")[1] * 1.05
        return cols, widths, max(sum(widths) + cs * 1.5 * (ncols - 1), hw), hh * 1.3 + per * lh

    canvas = Canvas(G, cs, th)
    tw, thh = block_size(title)
    pos = canvas.free_box(tw + 2 * pad, thh + 2 * pad, "tl")
    if pos is None:
        extra = int(math.ceil((thh + 2 * pad) / cs)) + 2
        wider = max(int(math.ceil((tw + 2 * pad) / cs)) + 2 - G.shape[1], 0)   # small regions: title wider than the map
        G = np.pad(G, ((extra, 0), (0, wider)))
        centre = centre + [extra, 0]
        off_r += extra
        canvas = Canvas(G, cs, th)
        pos = canvas.free_box(tw + 2 * pad, thh + 2 * pad, "tl")
    title_box = (pos[0], pos[1], pos[0] + tw + 2 * pad, pos[1] + thh + 2 * pad)
    r0, r1, c0, c1 = canvas.cells(*title_box)
    canvas.occ[r0:r1, c0:c1] = True

    legend_box = None
    if legend:
        lw, lh_ = block_size(legend)
        pos = None
        for corner in ("bl", "tr", "br"):
            pos = canvas.free_box(lw + 2 * pad, lh_ + 2 * pad, corner)
            if pos:
                break
        if pos is None:      # no room at sea: right under the title box, growing the canvas if needed
            need = int(math.ceil((title_box[3] + lh_ + 3 * pad) / cs)) + 1 - G.shape[0]
            if need > 0:
                G = np.pad(G, ((0, need), (0, 0)))
                occ = np.pad(canvas.occ, ((0, need), (0, 0)))
                canvas = Canvas(G, cs, th)
                canvas.occ = occ
            pos = (title_box[0], int(title_box[3] + pad))
        legend_box = (pos[0], pos[1], pos[0] + lw + 2 * pad, pos[1] + lh_ + 2 * pad)
        r0, r1, c0, c1 = canvas.cells(*legend_box)
        canvas.occ[r0:r1, c0:c1] = True

    small_box = None
    if entries:
        for ncols in (4, 5, 6, 3, 7, 8, 2, 1):
            cols, widths, bw, bh = small_block(ncols)
            pos = canvas.free_box(bw + 2 * pad, bh + 2 * pad, "br")
            if pos:
                break
        if pos is None:
            cols, widths, bw, bh = small_block(min(6, len(entries)))
            extra = int(math.ceil((bh + 2 * pad) / cs)) + 2
            G = np.pad(G, ((0, extra), (0, 0)))
            occ = np.pad(canvas.occ, ((0, extra), (0, 0)))
            canvas = Canvas(G, cs, th)
            canvas.occ = occ
            pos = canvas.free_box(bw + 2 * pad, bh + 2 * pad, "br")
        if pos is None:      # wider than the map: widen the canvas to the right
            extra = int(math.ceil((bw + 2 * pad) / cs)) + 2 - G.shape[1]
            G = np.pad(G, ((0, 0), (0, max(extra, 0))))
            occ = np.pad(canvas.occ, ((0, 0), (0, max(extra, 0))))
            canvas = Canvas(G, cs, th)
            canvas.occ = occ
            pos = canvas.free_box(bw + 2 * pad, bh + 2 * pad, "br")
        small_box = (pos[0], pos[1], pos[0] + bw + 2 * pad, pos[1] + bh + 2 * pad, cols, widths)
        r0, r1, c0, c1 = canvas.cells(*small_box[:4])
        canvas.occ[r0:r1, c0:c1] = True

    # --- squares
    sea = th["ocean"] or th["bg"]
    fills = [sea] + colour_of[1:]
    gaps = [th["grid"] or th["bg"]] + [
        mix(colour_of[i], "#000000", th["land_gap"]) if th["land_gap"] is not None else th["bg"]
        for i in range(1, n + 1)]
    px_shapes = None
    if shapes is not None:                 # outlines: the sea is plain, without the grid of squares
        fills[0] = th["bg"]
        px_shapes = [(i, [[(ring + [off_c, off_r]) * cs for ring in poly] for poly in polys])
                     for i, polys in shapes]
    # thematic maps: neighbours may share a (pale) colour, so the outlines must stand out
    edge = mix(th["border"], th["bg"], 0.3) if thematic else th["edge"]
    if out_path.lower().endswith(".svg"):
        names = {i: (units[i - 1]["code"], f"{units[i - 1]['name']} – {fmt_pop(pops[i], tx)}")
                 for i in range(1, n + 1)}
        dr = SvgDrawer(G, cs, fills, gaps, sea, th["grid"], names, px_shapes, edge)
    else:
        dr = RasterDrawer(G, cs, fills, gaps, px_shapes, edge)
    if thematic and shapes is None:
        segs = border_segments(G, cs)
        dr.lines(segs, th["border"], max(1, cs // 6))

    # --- labels inside countries
    world = info["world"]
    order = sorted((i for i in range(1, n + 1) if shown(i)), key=lambda k: -pops[k])
    external = []
    for i in order:
        if T[i] == 0:
            continue
        mine = G == i
        if not mine.any():
            continue
        name = units[i - 1]["name"]
        value = value_text(i)
        tcol = "#141414" if luminance(hex_rgb(colour_of[i])) > 0.5 else "#ffffff"
        dmap = distance_transform_edt(np.pad(mine, 1))[1:-1, 1:-1]
        ys, xs = np.nonzero(mine)
        dv = dmap[ys, xs]
        cy, cx = centre[i]
        rank = np.lexsort(((ys - cy) ** 2 + (xs - cx) ** 2, -dv))[:80]
        fs = cs * min(0.9 + 0.09 * math.sqrt(T[i]), 6.5)
        placed = False
        while fs >= max(cs * 0.75, 7):
            lines = [(name, fs, False), (value, fs * 0.8, False)]
            if not thematic and pops[i] / world >= 0.02 and fs >= cs * 2.2:
                lines.append((tx["share"].format(p=fmt_num(100 * pops[i] / world, 1, tx)), fs * 0.36, False))
            bw, bh = block_size([(s, z, b, None) for s, z, b in lines])
            for k in rank:
                px, py = (xs[k] + 0.5) * cs, (ys[k] + 0.5) * cs
                box = (px - bw / 2, py - bh / 2, px + bw / 2, py + bh / 2)
                if not canvas.inside(*box):
                    continue
                r0, r1, c0, c1 = canvas.cells(*box)
                part = mine[r0:r1, c0:c1]
                if part.mean() < 0.9 or canvas.occ[r0:r1, c0:c1].any():
                    continue
                y = box[1]
                for s, size, bold in lines:
                    dr.text((px, y), s, size, bold, tcol, anchor="ma")
                    y += text_size(font(size, bold), s)[1] * 1.08
                canvas.occ[r0:r1, c0:c1] = True
                placed = True
                break
            if placed:
                break
            fs *= 0.88
        if not placed:
            external.append(i)

    # --- labels outside, with a leader line. They may sit over other countries
    # (with a halo) when that keeps them much closer than the nearest open sea.
    efs = max(cs * 0.95, 8)
    f = font(efs)
    land = G > 0
    halo = th["ocean"] or th["bg"]
    dirs = [(math.cos(a), math.sin(a)) for a in np.linspace(0, 2 * math.pi, 24, endpoint=False)]
    leaders, ext_texts = [], []
    for i in external:
        s = f"{units[i - 1]['name']} {value_text(i)}"
        bw, bh = text_size(f, s)
        bw, bh = bw + 4, bh + 2
        ys, xs = np.nonzero(main_part(G == i))
        ay, ax = (ys.mean() + 0.5) * cs, (xs.mean() + 0.5) * cs
        best = None
        for r in range(1, 50):
            if best is not None and best[0] < r:
                break
            for dx, dy in dirs:
                px, py = ax + dx * r * cs * 0.8, ay + dy * r * cs * 0.8
                x0 = px if dx > 0.3 else (px - bw if dx < -0.3 else px - bw / 2)
                y0 = py - bh / 2
                box = (x0, y0, x0 + bw, y0 + bh)
                if not canvas.inside(*box):
                    continue
                r0, r1, c0, c1 = canvas.cells(*box)
                score = 1000 * canvas.occ[r0:r1, c0:c1].sum() + 0.6 * land[r0:r1, c0:c1].sum() + r
                if best is None or score < best[0]:
                    best = (score, box)
        if best is None:
            continue
        box = best[1]
        r0, r1, c0, c1 = canvas.cells(*box)
        canvas.occ[r0:r1, c0:c1] = True
        bx, by = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        k = np.argmin(((ys + 0.5) * cs - by) ** 2 + ((xs + 0.5) * cs - bx) ** 2)
        tyc, txc = (ys[k] + 0.5) * cs, (xs[k] + 0.5) * cs
        ex = min(max(txc, box[0]), box[2])
        ey = min(max(tyc, box[1]), box[3])
        if math.hypot(ex - txc, ey - tyc) > cs * 0.9:
            leaders.append((ex, ey, txc, tyc))
        ext_texts.append(((box[0] + 2, box[1] + 1), s))
    dr.lines(leaders, th["lead"], max(1, cs // 10))
    for xy, s in ext_texts:
        dr.text(xy, s, efs, False, th["label"], anchor="la", halo=halo, halo_w=max(1, cs // 7))

    # --- boxes
    def frame_box(b):
        dr.rect(b[:4], th["box"], th["box_edge"], max(1, cs // 6))

    def text_box(box, lines):
        frame_box(box)
        y = box[1] + pad
        for s, size, bold, sw in lines:
            x = box[0] + pad
            if sw:
                sq = size * 1.1
                th_ = text_size(font(size, bold), s or " ")[1]
                top = y + (th_ - sq) / 2
                fill = missing_colour if sw == "missing" else sw
                dr.rect((x, top, x + sq, top + sq), fill, th["box_edge"], 1)
                x += size * 1.6
            if s:
                dr.text((x, y), s, size, bold, th["text"])
            y += text_size(font(size, bold), s or " ")[1] * 1.08

    text_box(title_box, title)
    if legend_box:
        text_box(legend_box, legend)
    if small_box:
        frame_box(small_box)
        x0, y0, _, _, cols, widths = small_box
        dr.text((x0 + pad, y0 + pad), small_head, sfs * 1.15, True, th["text"])
        y0 += pad + text_size(font(sfs * 1.15, True), small_head)[1] * 1.3
        lh = text_size(font(sfs), "Ag")[1] * 1.05
        x = x0 + pad
        for col, w in zip(cols, widths):
            for j, e in enumerate(col):
                dr.text((x, y0 + j * lh), e, sfs, False, th["text"])
            x += w + cs * 1.5

    w, h = dr.save(out_path)
    log(f"saved {out_path} ({w}x{h})")


# ---------------------------------------------------------------------------
def system_lang():
    try:
        loc = locale.getlocale()[0] or ""
        if not loc and sys.platform == "win32":
            import ctypes
            loc = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
    except Exception:  # noqa: BLE001
        loc = ""
    loc = loc.lower()
    for code, names in (("pt", ("pt", "portuguese")), ("es", ("es", "spanish"))):
        if loc.startswith(names):
            return code
    return "en"


def split_overseas(pdata):
    """The World Bank counts the French overseas departments as part of France,
    while the 2018 layout (and the UN) give them squares of their own."""
    fra = pdata.get("FRA")
    if fra and fra[2] == "World Bank":
        extra = sum(pdata[c][0] for c, *_ in OVERSEAS.values() if c in pdata)
        pdata["FRA"] = (fra[0] - extra, fra[1], fra[2])


def fmt_value(v, tx):
    """A number with about three significant digits, in the language's format."""
    a = abs(v)
    decimals = 0 if a >= 100 else 1 if a >= 10 else 2 if a >= 0.1 else 3
    if a >= 1000:
        v = round(v, -int(math.floor(math.log10(a))) + 2)
    return fmt_num(v, decimals, tx)


def nice(v):
    """Round to two significant digits (for class limits)."""
    if v == 0:
        return 0.0
    return round(v, -int(math.floor(math.log10(abs(v)))) + 1)


def thematic_spec(arg, units, T, tx, info, ctx):
    """Build the --color-by description used by render(): a class per country,
    legend labels, the ramp to colour them and the value shown in each label."""
    n = len(units)
    cls = np.full(n + 1, -1)
    if arg == "continent":
        conts = [c for c, _ in info["continents"]]
        for k, u in enumerate(units):
            if u["continent"] in conts:
                cls[k + 1] = conts.index(u["continent"])
        return {"kind": "categorical", "what": tx["continent"], "cls": cls, "value_text": None,
                "labels": [tx["continents"].get(c, c) for c in conts]}

    if arg == "growth" or arg.startswith("growth:"):
        map_year = info["year"]
        if ":" in arg:
            base = int(arg.split(":", 1)[1])
        else:
            base = info["latest"] if map_year > info["latest"] else map_year - 10
        bdata, _ = load_population(ctx["cache"], ctx["offline"], ctx["source"], None, base)
        if ctx["layout"] == "2018":
            split_overseas(bdata)
        vals = {k + 1: 100 * (u["pop"] / bdata[u["code"]][0] - 1)
                for k, u in enumerate(units) if u["code"] in bdata and bdata[u["code"]][0] > 0}
        edges = GROWTH_EDGES
        for i, v in vals.items():
            cls[i] = int(np.digitize(v, edges))
        pct = [fmt_num(e, 0, tx) + "%" for e in edges]
        labels = ([tx["below"].format(b=pct[0])]
                  + [tx["range"].format(a=pct[j], b=pct[j + 1]) for j in range(len(edges) - 1)]
                  + [tx["above"].format(a=pct[-1])])
        text = {i: ("+" if v > 0 else "") + fmt_num(v, 1, tx) + "%" for i, v in vals.items()}
        return {"kind": "binned", "ramp": "diverging", "neutral": 2, "cls": cls, "labels": labels,
                "what": tx["growth"].format(a=base, b=map_year), "value_text": text,
                "missing": any(cls[i] < 0 for i in range(1, n + 1) if T[i] > 0)}

    if arg.startswith("csv:"):
        path = arg[4:]
        data, what = {}, os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 2 or not row[0].strip():
                    continue
                try:
                    data[row[0].strip().upper()] = float(row[1])
                except ValueError:
                    what = row[1].strip() or what          # header row: its second column names the values
    else:
        code = INDICATORS.get(arg) or (arg[3:] if arg.startswith("wb:") else None)
        if not code:
            sys.exit(f"--color-by: unknown value '{arg}' (use continent, growth[:YEAR], "
                     f"{', '.join(INDICATORS)}, wb:CODE or csv:FILE)")
        raw = load_worldbank(code, ctx["cache"], ctx["offline"])
        data = {c: v for c, (v, y) in raw.items()}
        years = collections.Counter(y for c, (v, y) in raw.items() if c in {u["code"] for u in units})
        what = tx["indicators"].get(arg) or worldbank_indicator_name(code, ctx["cache"], ctx["offline"])
        if years:
            what += f", {years.most_common(1)[0][0]}"
    vals = {k + 1: data[u["code"]] for k, u in enumerate(units) if u["code"] in data}
    on_map = np.array([v for i, v in vals.items() if T[i] > 0])
    if len(on_map) == 0:
        sys.exit("--color-by: no values for the countries on the map")
    edges = sorted(set(nice(q) for q in np.quantile(on_map, np.linspace(0, 1, 7)[1:-1])))
    for i, v in vals.items():
        cls[i] = int(np.digitize(v, edges))
    lim = [fmt_value(e, tx) for e in edges]
    labels = ([tx["below"].format(b=lim[0])]
              + [tx["range"].format(a=lim[j], b=lim[j + 1]) for j in range(len(edges) - 1)]
              + [tx["above"].format(a=lim[-1])])
    return {"kind": "binned", "ramp": "sequential", "cls": cls, "labels": labels, "what": what,
            "value_text": {i: fmt_value(v, tx) for i, v in vals.items()},
            "missing": any(cls[i] < 0 for i in range(1, n + 1) if T[i] > 0)}


def crop_region(layout, region_key, tx):
    """A copy of the layout cut to the bounding box of one region's countries."""
    G, T, units, pops = layout["G"], layout["T"], layout["units"], layout["pops"]
    conts = REGIONS[region_key]
    members = {k + 1 for k, u in enumerate(units) if u["continent"] in conts}
    ys, xs = np.nonzero(np.isin(G, list(members)))
    if len(ys) == 0:
        sys.exit(f"--region {region_key}: no countries on the map")
    m = 3
    if layout.get("shapes") is not None:
        # outlines: thin far-away strips (Russia across Asia, French Guiana) would make
        # the cut huge; keep each country's cells near its population centre
        keep = np.zeros(len(ys), bool)
        for i in members:
            sel = G[ys, xs] == i
            radius = 2.5 * math.sqrt(max(T[i], 1) / math.pi) + 2
            cy, cx = layout["centre"][i]
            keep |= sel & ((ys - cy) ** 2 + (xs - cx) ** 2 <= radius ** 2)
        if keep.any():
            ys, xs = ys[keep], xs[keep]
    ylo, yhi, xlo, xhi = ys.min(), ys.max(), xs.min(), xs.max()
    r0, r1 = max(ylo - m, 0), min(yhi + m + 1, G.shape[0])
    c0, c1 = max(xlo - m, 0), min(xhi + m + 1, G.shape[1])
    name = tx["continents"]["Americas"] if region_key == "americas" else tx["continents"][conts[0]]
    off_r, off_c = layout["shape_off"]
    out = dict(layout, G=G[r0:r1, c0:c1].copy(), centre=layout["centre"] - [r0, c0],
               shape_off=(off_r - r0, off_c - c0))
    region = {"name": name, "members": members, "cells": int(sum(T[i] for i in members)),
              "pop": float(sum(pops[i] for i in members)),
              "owid": sorted(units[i - 1]["name"] for i in members if units[i - 1]["source"].startswith("UN WPP"))}
    return out, region


def main(argv=None):
    ap = argparse.ArgumentParser(description="World population cartogram, as squares or as country outlines.")
    ap.add_argument("--theme", choices=["light", "dark", "both"], default="light")
    ap.add_argument("--palette", metavar="NAME", help="colour palette from the palette file (default: the file's default)")
    ap.add_argument("--palettes", metavar="FILE", help="palette file (default: palettes.json next to this script)")
    ap.add_argument("--list-palettes", action="store_true", help="list the palettes, check them and exit")
    ap.add_argument("--style", choices=["squares", "outlines"], default="squares",
                    help="squares = one square per --per-cell people (default); outlines = smooth country "
                         "outlines with areas proportional to population, like Worldmapper")
    ap.add_argument("--layout", choices=["2018", "auto"],
                    help="2018 = Max Roser's 2018 layout updated to the latest figures (default for squares); "
                         "auto = computed from the geographic map (always used for outlines)")
    ap.add_argument("--year", type=int, help="population of this year instead of the latest; years after the "
                                             "latest figures use the UN projection (up to 2100)")
    ap.add_argument("--region", choices=sorted(REGIONS), help="draw only this region, with bigger squares")
    ap.add_argument("--color-by", metavar="WHAT",
                    help="colour countries by a value: continent, growth[:BASEYEAR], "
                         f"{', '.join(INDICATORS)}, wb:INDICATOR (World Bank code) or csv:FILE (iso3,value)")
    ap.add_argument("--lang", choices=LANGS, default=None, help="language (default: system language)")
    ap.add_argument("-o", "--output", help="PNG, SVG or PDF file (default: population_cartogram_<theme>.png)")
    ap.add_argument("--per-cell", type=float, default=500_000, help="people per square (default: %(default)d)")
    ap.add_argument("--cell-px", type=int, help="size of a square in pixels (default: 10; larger for --region)")
    ap.add_argument("--source", choices=["auto", "worldbank", "owid"], default="auto",
                    help="auto = World Bank, with Our World in Data for the rest")
    ap.add_argument("--data", metavar="CSV", help="CSV with iso3,population[,year] that overrides the downloads")
    ap.add_argument("--csv", metavar="FILE", help="also write the figures used to this CSV")
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "population_cartogram"))
    ap.add_argument("--offline", action="store_true", help="use only cached downloads")
    ap.add_argument("--detail", type=int, default=1600, help="width of the base raster (default: %(default)s)")
    ap.add_argument("--passes", type=int, default=3, help="cartogram passes (default: %(default)s)")
    ap.add_argument("--ocean", type=float, default=0.6,
                    help="sea density relative to the mean land density; lower = continents closer")
    ap.add_argument("--rural", type=float, default=0.2,
                    help="share of each country's people spread evenly over its area (default: %(default)s)")
    ap.add_argument("--blur-km", type=float, default=180, help="spread of each city's people (default: %(default)s)")
    a = ap.parse_args(argv)
    lang = a.lang or system_lang()
    tx = TEXTS[lang]
    os.makedirs(a.cache, exist_ok=True)

    pals, default_pal = load_palettes(a.palettes)
    if a.list_palettes:
        print(palette_summary(pals, default_pal))
        for name, p in pals.items():
            for mode in ("light", "dark"):
                for w in palette_warnings(name, mode, p[mode]):
                    print("  warning: " + w)
        return
    pal_name = a.palette or default_pal
    if pal_name not in pals:
        ap.error(f"unknown palette '{pal_name}'; available: {', '.join(pals)}")
    if a.style == "outlines":
        if a.layout == "2018":
            ap.error("--style outlines deforms the geographic map; it cannot use the 2018 layout")
        a.layout = "auto"
    a.layout = a.layout or "2018"
    if a.layout == "2018" and a.per_cell != 500_000:
        ap.error("the 2018 layout has 500,000 people per square; use --layout auto for other values")
    if a.year is not None and not 1950 <= a.year <= 2100:
        ap.error("--year must be between 1950 and 2100")

    log("loading data")
    pdata, world = load_population(a.cache, a.offline, a.source, a.data, a.year)
    latest = max((v[1] for v in load_population(a.cache, a.offline, a.source, None)[0].values()), default=0)
    units_all = load_countries(a.cache, a.offline, lang)
    if a.layout == "2018":
        for num, (code, names, cont, label) in OVERSEAS.items():
            units_all.append({"code": code, "name": names[lang], "continent": cont, "label": label,
                              "nums": {num}, "polys": []})
        split_overseas(pdata)
    units = [u for u in units_all if u["code"] in pdata]
    missing = [u["code"] for u in units_all if u["code"] not in pdata]
    if missing:
        log(f"  no population for {len(missing)} map units (left as sea): {', '.join(missing)}")
    for u in units:
        u["pop"], u["year"], u["source"] = pdata[u["code"]]
    pops = np.array([0.0] + [u["pop"] for u in units])
    if world is None:
        world = float(pops.sum())

    shapes = None
    if a.layout == "2018":
        log("updating the 2018 layout")
        G, T, centre = layout_2018(units, pops, a.per_cell, a.cache, a.offline)
    else:
        places = load_places(a.cache, a.offline)
        log("rasterising the map")
        frame = Frame(a.detail)
        lab = rasterize(units, frame)
        pix = pixel_population(lab, pops, frame, places, a.rural, a.blur_km)

        # the cartogram is the slow part: keep it for runs with the same inputs
        key = hashlib.sha1(json.dumps([CARTOGRAM_VERSION, a.detail, a.passes, a.ocean, a.rural, a.blur_km,
                                       [u["code"] for u in units], pops.tolist()]).encode()).hexdigest()[:16]
        cpath = os.path.join(a.cache, f"cartogram_{key}.npz")
        if os.path.exists(cpath):
            log("using the cached cartogram")
            with np.load(cpath) as z:
                y, x, rho_bar = z["y"].astype(float), z["x"].astype(float), float(z["rho_bar"])
        else:
            log("computing the cartogram (takes a few minutes; the result is cached)")
            y, x, rho_bar = build_cartogram(lab, pix, pops, 4, 0.25, a.ocean, a.passes)
            np.savez_compressed(cpath, y=y.astype(np.float32), x=x.astype(np.float32), rho_bar=rho_bar)

        if a.style == "outlines":
            opath = os.path.join(a.cache, f"outlines_{key}_{int(a.per_cell)}_v{OUTLINES_VERSION}.pickle")
            if os.path.exists(opath):
                log("using the cached outlines")
                with open(opath, "rb") as f:
                    G, T, centre, shapes = pickle.load(f)
            else:
                log("deforming the borders (the result is cached)")
                G, T, centre, shapes = layout_outlines(units, frame, lab, pix, y, x, pops, a.per_cell, rho_bar)
                with open(opath, "wb") as f:
                    pickle.dump((G, T, centre, shapes), f)
        else:
            log("fitting the squares")
            G, T, centre = make_grid(lab, pix, y, x, pops, a.per_cell, rho_bar)

    conts = collections.Counter()
    for k, u in enumerate(units):
        conts[u["continent"]] += u["pop"]
    wb_years = collections.Counter(u["year"] for u in units if u["source"] == "World Bank")
    owid = sorted(u["name"] for u in units if u["source"].startswith("UN WPP"))
    projected = sum(u["source"] == UN_PROJ_LABEL for u in units) > len(units) / 2
    info = {
        "year": a.year or (wb_years.most_common(1)[0][0] if wb_years else max(u["year"] for u in units)),
        "year_given": a.year is not None, "projected": projected, "latest": latest,
        "wb_years": ", ".join(f"{y}" for y, _ in sorted(wb_years.items(), reverse=True)),
        "owid": owid, "world": world, "cells": int(T.sum()), "layout": a.layout, "style": a.style,
        "continents": [(c, p) for c, p in conts.most_common() if c in TEXTS["en"]["continents"]],
    }
    layout = {"G": G, "T": T, "centre": centre, "units": units, "pops": pops, "info": info,
              "per_cell": a.per_cell, "shapes": shapes, "shape_off": (0, 0)}

    if a.csv:
        with open(a.csv, "w", encoding="utf-8", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["iso3", "name", "continent", "population", "year", "source", "squares"])
            for k, u in enumerate(units):
                wr.writerow([u["code"], u["name"], u["continent"], int(u["pop"]), u["year"], u["source"],
                             int((G == k + 1).sum())])
        log(f"saved {a.csv}")

    thematic = None
    if a.color_by:
        ctx = {"cache": a.cache, "offline": a.offline, "source": a.source, "layout": a.layout}
        thematic = thematic_spec(a.color_by, units, T, tx, info, ctx)
        log(f"colouring by {thematic['what']}")
    region = None
    if a.region:
        layout, region = crop_region(layout, a.region, tx)
    cs = a.cell_px or (int(np.clip(round(3200 / layout["G"].shape[1]), 10, 40)) if region else 10)

    modes = ["light", "dark"] if a.theme == "both" else [a.theme]
    for mode in modes:
        for w in palette_warnings(pal_name, mode, pals[pal_name][mode]):
            log("  warning: " + w)
        out = a.output or ("population_cartogram" + (f"_{a.region}" if a.region else "") + ".png")
        root, ext = os.path.splitext(out)
        if a.theme == "both" or not a.output:
            out = f"{root}_{mode}{ext or '.png'}"
        log(f"drawing with palette '{pal_name}' ({mode})")
        render(layout, make_theme(pals[pal_name][mode], mode), lang, cs, out, thematic, region)


if __name__ == "__main__":
    main()
