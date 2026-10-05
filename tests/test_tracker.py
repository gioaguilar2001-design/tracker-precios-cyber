"""
Tests offline: filtro, limpieza de precios y scrapers ejecutados contra HTML
de ejemplo (Playwright intercepta las peticiones, no sale a internet).

    python -m pytest -q        # o: python tests/test_tracker.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scraper as tp  # noqa: E402


def test_limpiar_precio():
    assert tp.limpiar_precio("$ 29.990") == 29990
    assert tp.limpiar_precio("$129.990,00") == 129990
    assert tp.limpiar_precio(45990) == 45990
    assert tp.limpiar_precio("") is None


def test_filtro():
    ok = [
        "Disco Olímpico 15 kg Bumper 50mm",
        "Par de Discos Olimpicos 15kg Hierro",
        "Disco Bumper 15 Kilos Competición",
        "Disco olímpico de goma 15KG orificio 50 mm",
    ]
    no = [
        "Disco Pre-Olímpico 15 kg",
        "Disco preolimpico 15kg",
        "Disco 15 kg 28mm",
        "Barra sola olímpica 15 kg",
        "Disco Olímpico 20 kg Bumper",
        "Set discos olímpicos 5, 10 y 15 kg",
        "Disco olímpico 30kg (2x15)",
        "Disco 15 kg estándar",  # no es olímpico
        "Barra olímpica 15 kg mujer",  # no es disco
        "Kit discos olímpicos 15 kg",
        "Juego de discos olímpicos bumper 15kg",
        "Barra olímpica + disco olímpico 15 kg",
    ]
    for t in ok:
        assert tp.es_disco_olimpico_15kg(t), t
    for t in no:
        assert not tp.es_disco_olimpico_15kg(t), t


ML_HTML = """<html><body><ol>
<li class="ui-search-layout__item"><div class="poly-card">
  <a class="poly-component__title" href="https://articulo.mercadolibre.cl/MLC-1">Disco Olímpico Bumper 15 Kg 50mm</a>
  <div class="poly-price__current"><span class="andes-money-amount__fraction">49.990</span></div></div></li>
<li class="ui-search-layout__item"><div class="poly-card">
  <a class="poly-component__title" href="https://articulo.mercadolibre.cl/MLC-2">Disco Pre-Olímpico 15 kg 28mm</a>
  <div class="poly-price__current"><span class="andes-money-amount__fraction">19.990</span></div></div></li>
</ol></body></html>"""

FALABELLA_DATA = {"props": {"pageProps": {"results": [
    {"displayName": "Disco olímpico 15 kg", "brand": "Kansas",
     "url": "https://www.falabella.com/falabella-cl/product/123",
     "prices": [{"price": ["54.990"], "type": "internetPrice"},
                {"price": ["44.990"], "type": "cmrPrice"}]},
    {"displayName": "Disco olímpico 20 kg", "brand": "Kansas",
     "url": "https://www.falabella.com/falabella-cl/product/124",
     "prices": [{"price": ["64.990"]}]},
]}}}
FALABELLA_HTML = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(FALABELLA_DATA)}</script></body></html>'

PARIS_HTML = """<html><body><script type="application/ld+json">
{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{"@type":"Product",
"name":"Disco Olímpico Bumper 15 kg","url":"https://www.paris.cl/disco-15.html",
"offers":{"@type":"Offer","price":"39990"}}}]}</script></body></html>"""

GENERIC_HTML = """<html><body>
<div class="card"><a href="/products/disco-bumper-15kg">Disco Bumper Olímpico 15kg</a>
  <span>$59.990</span> <span>$52.990</span></div>
<div class="card"><a href="/products/barra">Barra sola 20 kg</a><span>$89.990</span></div>
</body></html>"""


async def _correr_scrapers():
    from playwright.async_api import async_playwright

    async def responder(route):
        url = route.request.url
        if "mercadolibre" in url:
            body = ML_HTML
        elif "falabella" in url:
            body = FALABELLA_HTML
        elif "paris.cl" in url:
            body = PARIS_HTML
        else:
            body = GENERIC_HTML
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    async with async_playwright() as p:
        browser = await tp.lanzar_navegador(p, visible=False)
        ctx = await browser.new_context()
        await ctx.route("**/*", responder)
        out = {}
        for clave, (nombre, fn) in tp.TIENDAS.items():
            out[clave] = await fn(ctx)
        await browser.close()
    return out


def test_scrapers_offline():
    r = asyncio.run(_correr_scrapers())
    validos = {k: [x for x in v if tp.es_disco_olimpico_15kg(x.nombre)] for k, v in r.items()}

    assert [(x.precio, x.url) for x in validos["ml"]] == [(49990, "https://articulo.mercadolibre.cl/MLC-1")]
    assert [(x.nombre, x.precio) for x in validos["falabella"]] == [("Kansas Disco olímpico 15 kg", 44990)]
    assert [x.precio for x in validos["paris"]] == [39990]
    assert [x.precio for x in validos["ironside"]] == [52990]
    assert validos["ironside"][0].url == "https://www.ironside.cl/products/disco-bumper-15kg"
    assert len(r["ironside"]) == 2  # la barra se extrae pero el filtro la descarta


if __name__ == "__main__":
    test_limpiar_precio()
    test_filtro()
    test_scrapers_offline()
    print("OK")
