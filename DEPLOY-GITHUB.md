# Publicación gratis en GitHub Pages

### 1. Repo
Crea un repositorio público, por ejemplo `alphaedge-daily-radar`.

### 2. Archivos
Sube:
- `index.html`
- `.github/workflows/daily-scan.yml`
- `scripts/update.py`
- `data/picks.json`
- `data/history.json`

### 3. Escaneo diario
En GitHub abre **Actions** → **AlphaEdge daily stock scan** → **Run workflow** para lanzar la primera actualización.

Después queda programado a las 08:20 de Nueva York de lunes a viernes.

### 4. Pages
En **Settings → Pages**, selecciona GitHub Actions. Usa el workflow estándar de Pages para publicar el repositorio estático.

El archivo `data/picks.json` se actualiza cada día y la web lo vuelve a leer.

### 5. Si quieres que el repo sea más privado
Los runners de repositorios públicos tienen una política gratuita distinta a los privados. Mantenerlo público es la ruta más sencilla para un coste de 0 € con GitHub-hosted runners.
