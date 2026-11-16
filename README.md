# Editor automático de videos (Cinematic Cut)

Sube un video y GitHub lo edita solo, sin que tengas que hacer nada más:

- Quita las pausas largas para que la conversación fluya.
- Quita las tomas en las que te equivocaste y volviste a empezar la frase, y las muletillas sueltas ("eh", "este"…).
- Alterna acercamientos y alejamientos a tu cara en cada corte, para que el video se sienta activo.
- Colores vivos: más saturación y contraste, piel cálida y sombras frías, y los bordes un poco más oscuros.
- Voz limpia y con el volumen que piden Reels y TikTok.
- Subtítulos grandes en mayúsculas, con una palabra destacada en amarillo.
- Sale en formato vertical 1080×1920, listo para publicar.

## Cómo usarlo

### Opción A: videos de hasta 25 MB (desde el teléfono o la computadora)
1. Abre este repositorio en GitHub y entra en la carpeta `videos/entrada`.
2. Toca **Add file → Upload files**, elige tu video y toca **Commit changes**.
3. Espera unos 5 a 10 minutos. El video editado aparece en `videos/salida` con el nombre `TU-VIDEO-editado.mp4`.

### Opción B: videos más pesados (enlace de Google Drive, Dropbox, etc.)
1. En GitHub ve a **Actions → Editar video automáticamente → Run workflow**.
2. Pega un enlace de **descarga directa** del video y toca **Run workflow**.
3. Cuando termine, descarga el resultado desde esa misma ejecución, en **Artifacts → videos-editados**.

Para ver el avance, entra en la pestaña **Actions**.
Si un video editado pesa más de 95 MB, no se guarda en la carpeta: descárgalo desde **Artifacts**.

## Ajustes
- **Sin subtítulos:** desmarca la casilla "Agregar subtítulos" al usar la opción B.
- **Usarlo en tu computadora:** `python editor/auto_edit.py entrada.mov salida.mp4 [--sin-subtitulos]` (necesitas `ffmpeg` y `pip install -r editor/requirements.txt`).
