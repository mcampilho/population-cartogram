# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [1.0.0] - 2026-11-18

First tagged version.

### Features
- Population cartogram of 500,000-person squares on Max Roser's 2018 layout, updated with the
  latest World Bank figures and the UN WPP 2024 estimates and projections (1950–2100).
- `--style outlines`: real borders deformed so every country's area matches its population.
- `--style map`: an ordinary map in nine projections (Equal Earth, Robinson, Winkel Tripel,
  Mollweide, Mercator, Web Mercator, Miller, Gall–Peters, equirectangular), with
  `--central-meridian`.
- Thematic colouring (`--color-by`: continent, growth, life expectancy, GDP, any World Bank
  indicator or a CSV), with `--classes`, `--breaks quantile|jenks|log|linear` and `--ramp`.
- `--region` for continents and regions, light and dark themes, palettes in `palettes.json`.
- Texts in English, Portuguese and Spanish.
- PNG, SVG (labels fitted for zooming in) and vector PDF output (light theme only; needs
  `reportlab`).
- Map labels without halos, placed only where they contrast with the colours under them.

### Project
- `pyproject.toml` with the dependencies and the ruff, mypy and pytest settings.
- Unit and smoke tests in `test/` (no network needed), and a GitHub Actions workflow.

### Fixed
- `roundest` could return a class limit outside its interval through a floating-point error
  (e.g. between 5 and 5.0001), which would put a country in the wrong class.
- The outlines cache is stored as plain arrays (`.npz`) instead of a pickle.
