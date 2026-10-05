"""Genera docs/informe-corte2.pdf a partir de docs/informe.html (requiere: pip install playwright && playwright install chromium).
Alternativa sin instalar nada: abrir informe.html en Edge/Chrome -> Ctrl+P -> Guardar como PDF."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

AQUI = Path(__file__).resolve().parent


async def main():
    async with async_playwright() as p:
        nav = await p.chromium.launch()
        pag = await nav.new_page()
        await pag.goto((AQUI / "informe.html").as_uri())
        await pag.wait_for_timeout(500)
        await pag.pdf(path=str(AQUI / "informe-corte2.pdf"), format="A4", print_background=True,
                      margin={"top": "18mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
                      display_header_footer=True, header_template="<span></span>",
                      footer_template="<div style='font-size:8px;width:100%;text-align:center;color:#888'>"
                                      "Consola de monitoreo · Ronald Toro · <span class='pageNumber'></span>/<span class='totalPages'></span></div>")
        await nav.close()
    print("PDF generado:", AQUI / "informe-corte2.pdf")

asyncio.run(main())
