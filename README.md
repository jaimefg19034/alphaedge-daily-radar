[README.md](https://github.com/user-attachments/files/32972157/README.md)
# AlphaEdge — Free Daily Radar

Web estática premium para mostrar 5 acciones de alto riesgo que se recalculan automáticamente cada día antes de la apertura regular de EE. UU.

## Lo que es realmente gratis

- **Sin servidor de pago.** La web se puede alojar en GitHub Pages.
- **Sin API bursátil de pago.** El escáner usa el endpoint público de Yahoo Finance para OHLCV diario y los feeds RSS por ticker.
- **Automatización gratis en repositorio público.** GitHub documenta que los runners estándar de GitHub-hosted son gratis para repositorios públicos; los workflows programados permiten ejecutar el escáner cada día. 
- **Sin clave obligatoria.** No hay `OPENAI_API_KEY`, Massive, Polygon ni otra API premium.

> Nota: el endpoint de Yahoo usado aquí es público/no oficial y puede cambiar o limitarse. La web no debe presentarse como un feed profesional tick-by-tick.

## Cómo desplegarlo GRATIS

1. Crea un repositorio **público** en GitHub.
2. Sube todo el contenido de esta carpeta a la raíz del repositorio.
3. En **Settings → Pages**, selecciona **GitHub Actions** como método de publicación.
4. Haz un primer `workflow_dispatch` manual desde **Actions → AlphaEdge daily stock scan**.
5. Activa Pages con un workflow de Pages o sirve `index.html` directamente desde tu hosting estático preferido.

El workflow de escaneo está en `.github/workflows/daily-scan.yml` y se ejecuta a las **08:20 America/New_York**, antes de la apertura regular. GitHub indica que `schedule` admite zona horaria; como cualquier cron programado, puede sufrir algún retraso puntual por carga del servicio.

## Qué hace cada mañana

1. Descarga OHLCV diario de un universo de acciones de alta beta/catalizador.
2. Calcula momentum de 5/20 días, RSI(14), EMA20, EMA50, ATR y volumen relativo.
3. Lee titulares recientes del feed financiero de cada símbolo.
4. Busca palabras clave de catalizadores/riesgos.
5. Puntúa todas las candidatas.
6. Guarda exactamente 5 picks.
7. Calcula entrada de referencia, stop, TP1, TP2 y R/R en función de la volatilidad.
8. Añade el resultado a `data/history.json` y hace commit automático.
9. La web recoge el nuevo JSON sin necesidad de tocar el HTML.

## Importante: IA vs sistema gratuito

Esta edición usa un **motor cuantitativo automático**, no un LLM remoto. Eso es lo que permite mantener el flujo sin coste de API obligatorio. Puedes conectar posteriormente un modelo con plan gratuito/limitado, pero ese proveedor y sus límites pueden cambiar.

## Limitaciones

- Los precios son diarios, no tick-by-tick.
- Yahoo Finance es una fuente pública/no oficial; el endpoint puede cambiar, limitarse o dejar de responder.
- Los titulares dependen de la disponibilidad de los RSS.
- Los SL/TP son escenarios del modelo, no garantías de ejecución.
- El sistema puede fallar en una sesión si una fuente pública está caída; en ese caso conserva el último `picks.json` válido.
