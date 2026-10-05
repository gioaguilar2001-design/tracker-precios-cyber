# Tracker de precios Cyber: discos olímpicos 15 kg

Compara precios de discos olímpicos de 15 kg (orificio de 50 mm) en MercadoLibre Chile
(categoría Fitness y Musculación), Falabella, Paris, Ironside y Ten Series, usando Playwright en modo headless.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # en Windows usar siempre esta forma
python scraper.py                           # todas las tiendas
python scraper.py --tiendas ml paris         # solo algunas
python -m pytest -q tests                    # tests offline (no necesitan internet)
```

Muestra una tabla `rich` ordenada de menor a mayor precio y exporta `precios_discos.csv`
con las columnas Nombre, Precio, Tienda y URL.

Filtro: descarta títulos con "pre-olímpico", "28mm", "barra sola", sets/kits/juegos, packs con barra o rangos de pesos
y cualquier peso distinto de 15 kg; además exige que sea un disco olímpico/bumper.

Las tiendas cambian su HTML seguido. Cada scraper intenta primero leer los datos estructurados
(`__NEXT_DATA__` en Falabella, JSON-LD) y si no los encuentra usa una heurística sobre el DOM.
Si una tienda falla, el resto sigue y la tabla "Estado por tienda" muestra el error.
