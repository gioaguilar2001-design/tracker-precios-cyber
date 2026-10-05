#!/usr/bin/env python3
"""
Tracker de precios Cyber: discos olímpicos de 15 kg (orificio 50 mm) en Chile.

Busca en MercadoLibre Chile (categoría Fitness y Musculación), Falabella, Paris,
Ironside y Ten Series usando Playwright headless, filtra los resultados que no
corresponden a discos olímpicos de 15 kg, muestra una tabla con `rich` ordenada
por precio y exporta todo a `precios_cyber_discos.csv`.

Uso:
    python tracker_precios.py                 # todas las tiendas
    python tracker_precios.py --tiendas ml falabella
    python tracker_precios.py --visible       # abre el navegador (debug)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import pandas as pd
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from rich.console import Console
from rich.table import Table

CSV_SALIDA = "precios_cyber_discos.csv"
TIMEOUT_MS = 45_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

console = Console()


@dataclass
class Producto:
    nombre: str
    precio: int
    tienda: str
    url: str


# --------------------------------------------------------------------------- #
# Utilidades de limpieza y filtro
# --------------------------------------------------------------------------- #

def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar títulos."""
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def limpiar_precio(valor) -> int | None:
    """'$ 29.990' -> 29990. Acepta int/float/str; descarta decimales ',00'."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return int(valor) if valor > 0 else None
    texto = str(valor).strip()
    texto = re.sub(r",\d{1,2}$", "", texto)  # CLP no usa decimales
    digitos = re.sub(r"[^\d]", "", texto)  # quita $, puntos y espacios
    return int(digitos) if digitos else None


PALABRAS_EXCLUIDAS = (
    r"pre[\s\-]?olimpic",  # pre-olímpico / preolímpico / pre olimpico
    r"\b28\s?mm\b",
    r"\b25\s?mm\b",
    r"\b30\s?mm\b",
    r"barra\s+sola",
)
RE_EXCLUIDAS = re.compile("|".join(PALABRAS_EXCLUIDAS))
# Captura pesos tipo "15kg", "15 kg", "15 kilos", "2,5kg", "10-15 kg"
RE_PESO = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:kg|kgs|kilo|kilos|k)\b")
# Listas o rangos de pesos: "5, 10 y 15 kg", "10-15kg", "10/15 kilos"
RE_LISTA_PESOS = re.compile(r"\d+\s*(?:,|\by\b|/|-|\ba\b)\s*\d+[\d\s,/\-ya]*(?:kg|kilo)")
RE_OLIMPICO = re.compile(r"olimpic|50\s?mm|bumper")
RE_DISCO = re.compile(r"disco|bumper|plate")


def es_disco_olimpico_15kg(nombre: str) -> bool:
    """
    Filtro inteligente:
      - Debe ser un disco olímpico (o bumper / 50 mm).
      - Descarta pre-olímpico, 28 mm, barra sola, etc.
      - Todos los pesos mencionados en el título deben ser 15 kg
        (un set "5, 10, 15 kg" o "disco 20kg" queda fuera).
    """
    t = normalizar(nombre)
    if RE_EXCLUIDAS.search(t):
        return False
    if not RE_DISCO.search(t) or not RE_OLIMPICO.search(t):
        return False
    if RE_LISTA_PESOS.search(t):
        return False
    pesos = {float(p.replace(",", ".")) for p in RE_PESO.findall(t)}
    # "par de discos 15kg" (2x15) se acepta; "30kg (2x15)" no, porque 30 != 15
    return pesos == {15.0}


# --------------------------------------------------------------------------- #
# Extracción genérica (JSON-LD + heurística DOM) reutilizable por tienda
# --------------------------------------------------------------------------- #

JS_JSON_LD = """
() => {
  const out = [];
  const visit = (n) => {
    if (!n || typeof n !== 'object') return;
    if (Array.isArray(n)) { n.forEach(visit); return; }
    const type = [].concat(n['@type'] || []).join(',');
    if (type.includes('Product')) {
      let offers = [].concat(n.offers || []);
      let price = null;
      for (const o of offers) {
        price = o.price ?? o.lowPrice ?? (o.priceSpecification || {}).price ?? price;
      }
      out.push({nombre: n.name, precio: price, url: n.url || (offers[0] || {}).url});
    }
    if (n.itemListElement) visit(n.itemListElement);
    if (n.item) visit(n.item);
    if (n['@graph']) visit(n['@graph']);
  };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { visit(JSON.parse(s.textContent)); } catch (e) {}
  });
  return out;
}
"""

# Recorre enlaces a productos y sube por el DOM hasta encontrar la "tarjeta"
# que contiene un precio. Toma el precio más bajo visible (precio oferta/Cyber).
JS_HEURISTICA = """
(linkSelector) => {
  const res = [], vistos = new Set();
  const rePrecio = /\\$\\s?\\d{1,3}(?:\\.\\d{3})+/g;
  for (const a of document.querySelectorAll(linkSelector)) {
    const href = a.href;
    if (!href || vistos.has(href)) continue;
    let card = a, depth = 0;
    while (card && depth < 7 && !(card.innerText || '').match(rePrecio)) {
      card = card.parentElement; depth++;
    }
    if (!card) continue;
    const texto = card.innerText || '';
    const precios = (texto.match(rePrecio) || []).map(p => parseInt(p.replace(/\\D/g, ''), 10));
    if (!precios.length) continue;
    const titulo = (a.getAttribute('title') || a.innerText || '').trim()
      || texto.split('\\n').map(s => s.trim())
              .filter(s => s && !s.includes('$')).sort((x, y) => y.length - x.length)[0] || '';
    if (titulo.length < 6) continue;
    vistos.add(href);
    res.push({nombre: titulo.split('\\n')[0], precio: Math.min(...precios), url: href});
  }
  return res;
}
"""


async def autoscroll(page: Page, pasos: int = 6) -> None:
    """Hace scroll para disparar lazy-loading de productos."""
    for _ in range(pasos):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(600)


async def abrir(ctx: BrowserContext, url: str, esperar: str | None = None) -> Page:
    page = await ctx.new_page()
    await page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
    if esperar:
        try:
            await page.wait_for_selector(esperar, timeout=15_000)
        except Exception:
            pass  # seguimos con lo que haya cargado
    await page.wait_for_timeout(2_000)
    await autoscroll(page)
    return page


def a_productos(crudos: list[dict], tienda: str, base: str) -> list[Producto]:
    productos = []
    for c in crudos:
        nombre = " ".join(str(c.get("nombre") or "").split())
        precio = limpiar_precio(c.get("precio"))
        url = c.get("url") or ""
        if not nombre or not precio or not url:
            continue
        productos.append(Producto(nombre, precio, tienda, urljoin(base, url)))
    return productos


# --------------------------------------------------------------------------- #
# Scrapers por tienda
# --------------------------------------------------------------------------- #

async def scrape_mercadolibre(ctx: BrowserContext) -> list[Producto]:
    # Ruta de categoría Deportes y Fitness > Fitness y Musculación
    url = ("https://listado.mercadolibre.cl/deportes-fitness/fitness-musculacion/"
           "disco-olimpico-15-kg_NoIndex_True")
    page = await abrir(ctx, url, "li.ui-search-layout__item, .poly-card")
    crudos = await page.evaluate(
        """
        () => {
          // Tarjetas de resultado; si el layout no usa <li>, cae a .poly-card (sin anidar duplicados)
          let cards = [...document.querySelectorAll('li.ui-search-layout__item')];
          if (!cards.length) cards = [...document.querySelectorAll('div.poly-card')];
          return cards.map(card => {
          const a = card.querySelector('a.poly-component__title, h3 a, a.ui-search-link, a.ui-search-item__group__element');
          const titulo = card.querySelector('.poly-component__title, .ui-search-item__title');
          // precio actual (no el tachado)
          const frac = card.querySelector(
            '.poly-price__current .andes-money-amount__fraction, ' +
            '.ui-search-price__second-line .andes-money-amount__fraction, ' +
            '.andes-money-amount:not(.andes-money-amount--previous) .andes-money-amount__fraction');
          return {
            nombre: titulo ? titulo.innerText : (a ? a.innerText : ''),
            precio: frac ? frac.innerText : null,
            url: a ? a.href.split('#')[0] : null,
          };
          });
        }
        """
    )
    if not crudos:
        crudos = await page.evaluate(JS_JSON_LD)
    await page.close()
    return a_productos(crudos, "MercadoLibre", url)


async def scrape_falabella(ctx: BrowserContext) -> list[Producto]:
    url = "https://www.falabella.com/falabella-cl/search?Ntt=" + quote_plus("disco olimpico 15 kg")
    page = await abrir(ctx, url, "#__NEXT_DATA__")
    crudos: list[dict] = []
    # Falabella (Next.js) expone los resultados en __NEXT_DATA__
    raw = await page.evaluate(
        "() => (document.getElementById('__NEXT_DATA__') || {}).textContent || ''"
    )
    if raw:
        try:
            data = json.loads(raw)
            resultados = data["props"]["pageProps"].get("results", [])
            for r in resultados:
                precios = r.get("prices") or []
                # El menor precio listado = precio CMR/oferta/Cyber vigente
                valores = [limpiar_precio((p.get("price") or [None])[0]) for p in precios]
                valores = [v for v in valores if v]
                crudos.append({
                    "nombre": " ".join(filter(None, [r.get("brand"), r.get("displayName")])),
                    "precio": min(valores) if valores else None,
                    "url": r.get("url"),
                })
        except (KeyError, ValueError, TypeError):
            pass
    if not crudos:
        crudos = await page.evaluate(JS_HEURISTICA, "a[href*='/product/']")
    await page.close()
    return a_productos(crudos, "Falabella", url)


async def scrape_paris(ctx: BrowserContext) -> list[Producto]:
    url = "https://www.paris.cl/search/?q=" + quote_plus("disco olimpico 15 kg")
    page = await abrir(ctx, url, "[data-testid*='product'], .product-tile, a[href$='.html']")
    crudos = await page.evaluate(JS_JSON_LD)
    if not crudos:
        crudos = await page.evaluate(JS_HEURISTICA, "a[href$='.html'], a[href*='/p/']")
    await page.close()
    return a_productos(crudos, "Paris", url)


async def scrape_tienda_generica(ctx: BrowserContext, tienda: str, url: str) -> list[Producto]:
    """Tiendas especializadas (Shopify / Jumpseller / WooCommerce)."""
    page = await abrir(ctx, url, "a[href*='/product']")
    crudos = await page.evaluate(JS_JSON_LD)
    if not crudos:
        crudos = await page.evaluate(
            JS_HEURISTICA, "a[href*='/products/'], a[href*='/product/'], a[href*='/producto/']"
        )
    await page.close()
    return a_productos(crudos, tienda, url)


TIENDAS = {
    "ml": ("MercadoLibre", scrape_mercadolibre),
    "falabella": ("Falabella", scrape_falabella),
    "paris": ("Paris", scrape_paris),
    "ironside": ("Ironside", lambda ctx: scrape_tienda_generica(
        ctx, "Ironside", "https://www.ironside.cl/search?q=" + quote_plus("disco olimpico 15"))),
    "tenseries": ("Ten Series", lambda ctx: scrape_tienda_generica(
        ctx, "Ten Series", "https://www.tenseries.cl/search?q=" + quote_plus("disco olimpico 15"))),
}


# --------------------------------------------------------------------------- #
# Orquestación, tabla y CSV
# --------------------------------------------------------------------------- #

async def lanzar_navegador(p, visible: bool) -> Browser:
    kwargs = {"headless": not visible, "args": ["--disable-blink-features=AutomationControlled"]}
    try:
        return await p.chromium.launch(**kwargs)
    except Exception:
        # Fallback: Chromium del sistema (p.ej. PLAYWRIGHT_BROWSERS_PATH preinstalado)
        exe = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
        return await p.chromium.launch(executable_path=exe, **kwargs)


async def ejecutar(claves: list[str], visible: bool) -> tuple[list[Producto], dict[str, str]]:
    estado: dict[str, str] = {}
    todos: list[Producto] = []
    async with async_playwright() as p:
        browser = await lanzar_navegador(p, visible)
        ctx = await browser.new_context(
            user_agent=USER_AGENT, locale="es-CL", timezone_id="America/Santiago",
            viewport={"width": 1366, "height": 900},
        )

        async def correr(clave: str):
            nombre, fn = TIENDAS[clave]
            try:
                with console.status(f"[cyan]Buscando en {nombre}..."):
                    encontrados = await fn(ctx)
                validos = [x for x in encontrados if es_disco_olimpico_15kg(x.nombre)]
                estado[nombre] = f"{len(validos)} válidos / {len(encontrados)} extraídos"
                todos.extend(validos)
            except Exception as e:  # una tienda caída no detiene al resto
                estado[nombre] = f"[red]error: {str(e).splitlines()[0][:80]}[/red]"

        await asyncio.gather(*(correr(c) for c in claves))
        await browser.close()

    # Deduplica por URL y ordena de menor a mayor precio
    unicos = {x.url: x for x in todos}
    return sorted(unicos.values(), key=lambda x: x.precio), estado


def formato_clp(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


def mostrar_tabla(productos: list[Producto], estado: dict[str, str]) -> None:
    resumen = Table(title="Estado por tienda", show_header=True, header_style="bold")
    resumen.add_column("Tienda")
    resumen.add_column("Resultado")
    for tienda, msg in estado.items():
        resumen.add_row(tienda, msg)
    console.print(resumen)

    tabla = Table(
        title="🏋️  Discos olímpicos 15 kg (50 mm) — Cyber Chile",
        header_style="bold magenta", show_lines=True,
    )
    tabla.add_column("#", justify="right", style="dim")
    tabla.add_column("Producto", style="white", max_width=55)
    tabla.add_column("Precio (CLP)", justify="right", style="bold green")
    tabla.add_column("Tienda", style="cyan")
    tabla.add_column("URL", style="blue", overflow="fold", max_width=60)
    for i, prod in enumerate(productos, 1):
        tabla.add_row(str(i), prod.nombre, formato_clp(prod.precio), prod.tienda,
                      f"[link={prod.url}]{prod.url}[/link]")
    if productos:
        console.print(tabla)
        mejor = productos[0]
        console.print(f"\n[bold green]Mejor precio:[/] {formato_clp(mejor.precio)} — "
                      f"{mejor.nombre} ({mejor.tienda})")
    else:
        console.print("[yellow]No se encontraron productos que pasen el filtro.[/yellow]")


def exportar_csv(productos: list[Producto], ruta: str = CSV_SALIDA) -> None:
    df = pd.DataFrame([asdict(p) for p in productos], columns=["nombre", "precio", "tienda", "url"])
    df.columns = ["Nombre", "Precio", "Tienda", "URL"]
    df.to_csv(ruta, index=False, encoding="utf-8-sig")  # utf-8-sig: abre bien en Excel
    console.print(f"[dim]CSV exportado: {Path(ruta).resolve()} ({len(df)} filas)[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tiendas", nargs="+", choices=list(TIENDAS), default=list(TIENDAS))
    parser.add_argument("--visible", action="store_true", help="Navegador no headless (debug)")
    parser.add_argument("--csv", default=CSV_SALIDA)
    args = parser.parse_args()

    productos, estado = asyncio.run(ejecutar(args.tiendas, args.visible))
    mostrar_tabla(productos, estado)
    exportar_csv(productos, args.csv)


if __name__ == "__main__":
    main()
