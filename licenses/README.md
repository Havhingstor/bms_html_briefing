# License Inventory

## Dependency roots from requirements.txt
- jinja2
- pillow==12.3.0
- resvg_py==0.5.0
- pymupdf
- fastapi
- uvicorn
- weasyprint
- beautifulsoup4
- pystray

## Python dependency closure (33 packages discovered)
- annotated-doc==0.0.4
- annotated-types==0.7.0
- anyio==4.12.0
- beautifulsoup4==4.14.3
- cffi==2.0.0
- click==8.3.1
- cssselect2==0.8.0
- fastapi==0.124.0
- fonttools==4.61.0
- h11==0.16.0
- idna==3.11
- jinja2==3.1.6
- markupsafe==3.0.3
- pillow==12.3.0
- pycparser==2.23
- pydantic==2.12.5
- pydantic-core==2.41.5
- pydyf==0.12.1
- pystray==0.19.5
- resvg-py==0.5.0
- pymupdf==1.26.6
- python-xlib==0.33
- pyphen==0.17.2
- six==1.17.0
- soupsieve==2.8
- starlette==0.50.0
- tinycss2==1.5.1
- tinyhtml5==2.0.0
- typing-extensions==4.15.0
- typing-inspection==0.4.2
- uvicorn==0.38.0
- weasyprint==67.0
- webencodings==0.5.1

## Additional packaged Linux GI runtime dependencies
- PyGObject==3.56.2
- pycairo==1.29.0

## OpenChart PDF runtime
- OpenChart renders through the pinned `resvg_py` Python extension and Pillow;
  no external converter or system font configuration is required.
- OpenChart bundles Nimbus Sans regular and bold fonts under AGPL-3 with the
  font exception. The complete notice is packaged at
  `openchart/fonts/LICENSE`.
- The packaged IP2Location IATA/ICAO catalog retains its CC BY-SA 4.0 notice at
  `openchart/third_party_licenses/IP2Location-IATA-ICAO.md`.

## Python license files
- files in `licenses/python/`: 43
- `falcon-bms-tacview-converter-LICENSE` covers the heightmap elevation lookup algorithm adapted from the Falcon BMS Tacview Converter.
- `openchart-OpenTaxiway-LICENSE.md` covers code derived from OpenTaxiway in the bundled OpenChart runtime.

## Bundled asset licenses
- `ibm-plex-mono-OFL-1.1.txt`
- `inter-OFL-1.1.txt`
- `leaflet-LICENSE`
- `viper-display-LICENSE`
