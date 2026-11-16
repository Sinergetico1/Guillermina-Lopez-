#!/usr/bin/env python3
"""Edición automática de videos verticales con estilo "Cinematic Cut".

Pasos:
  1. Detecta pausas largas en el audio y las elimina (cortes de salto).
  2. Si hay transcripción (faster-whisper), elimina frases repetidas
     (cuando te equivocas y vuelves a empezar) y muletillas sueltas.
  3. Detecta tu cara para centrar los acercamientos.
  4. Alterna acercamientos/alejamientos en cada corte y empujes lentos de cámara.
  5. Corrección de color viva (saturación, contraste, tono cálido/frío, viñeta).
  6. Limpia y normaliza el audio para redes sociales (-14 LUFS).
  7. Opcional: subtítulos grandes quemados en el video.

Uso:
  python editor/auto_edit.py ENTRADA.mov SALIDA.mp4 [--sin-subtitulos] [--sin-transcripcion]
"""
import argparse
import difflib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import unicodedata

FPS = 30
OUT_W, OUT_H = 1080, 1920
PRE_PAD, POST_PAD = 0.10, 0.14
SILENCE_DB = -32
SILENCE_MIN = 0.35
FILLERS = {"eh", "em", "ehm", "mm", "mmm", "este", "pues", "ah", "aja", "o", "sea", "bueno", "osea"}

# (zoom inicial, cambio durante el corte). Se repite en ciclo.
ZOOM_PATTERN = [(1.0, 0.06), (1.25, 0.0), (1.05, 0.07), (1.38, 0.0), (1.0, 0.08),
                (1.22, 0.05), (1.45, 0.0), (1.08, 0.06), (1.28, 0.0), (1.0, 0.10),
                (1.35, 0.0), (1.12, 0.07)]

GRADE = ("eq=contrast=1.10:saturation=1.30:brightness=0.01:gamma=0.97,vibrance=intensity=0.35,"
         "colorbalance=rs=-0.05:bs=0.07:rh=0.06:gh=0.02:bh=-0.05,"
         "curves=master='0/0.02 0.25/0.21 0.5/0.5 0.75/0.79 1/0.98',"
         "unsharp=5:5:0.6,vignette=angle=PI/5,format=yuv420p")


def log(msg):
    print(f"[auto_edit] {msg}", flush=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = int(v.get("tags", {}).get("rotate", rot))
    if abs(rot) in (90, 270):
        w, h = h, w
    return w, h, float(info["format"]["duration"])


def speech_intervals(path, duration):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af",
                          f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    silences = list(zip(starts, ends + [duration] * (len(starts) - len(ends))))
    speech, cur = [], 0.0
    for s, e in silences:
        if s - cur > 0.05:
            speech.append([cur, s])
        cur = e
    if duration - cur > 0.05:
        speech.append([cur, duration])
    # descarta ruidos sueltos muy cortos
    return [seg for seg in speech if seg[1] - seg[0] >= 0.20]


def transcribe(path):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper no instalado: sin transcripción")
        return None
    try:
        model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), compute_type="int8")
        segs, _ = model.transcribe(path, language="es", word_timestamps=True, vad_filter=False)
        words = [(w.start, w.end, w.word.strip()) for s in segs for w in s.words]
        log(f"transcripción: {len(words)} palabras")
        return words
    except Exception as exc:  # sin red / sin modelo
        log(f"no se pudo transcribir ({exc.__class__.__name__}): sigo sin transcripción")
        return None


def norm(word):
    word = unicodedata.normalize("NFD", word.lower())
    word = "".join(c for c in word if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9ñ]", "", word)


def remove_mistakes(segments, words):
    """Quita frases que se vuelven a decir justo después (toma fallida) y segmentos solo de muletillas."""
    seg_words = [[w for w in words if s <= (w[0] + w[1]) / 2 <= e] for s, e in segments]
    keep = []
    for i, (seg, ws) in enumerate(zip(segments, seg_words)):
        tokens = [norm(w[2]) for w in ws if norm(w[2])]
        if tokens and all(t in FILLERS for t in tokens):
            log(f"  quito muletilla {seg[0]:.2f}-{seg[1]:.2f}: {' '.join(tokens)}")
            continue
        if i + 1 < len(segments) and len(tokens) >= 2:
            nxt = [norm(w[2]) for w in seg_words[i + 1] if norm(w[2])]
            k = min(len(tokens), len(nxt), 5)
            if k >= 2 and difflib.SequenceMatcher(None, tokens[:k], nxt[:k]).ratio() >= 0.75:
                log(f"  quito toma repetida {seg[0]:.2f}-{seg[1]:.2f}: {' '.join(tokens)}")
                continue
        keep.append(seg)
    return keep


def face_center(path, duration):
    try:
        import cv2
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except (ImportError, AttributeError):
        log("OpenCV no disponible: centro por defecto")
        return 0.5, 0.33
    cap = cv2.VideoCapture(path)
    xs, ys = [], []
    t = 0.5
    while t < duration:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(w // 10, w // 10))
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + fw / 2) / w)
            ys.append((y + fh / 2) / h)
        t += 2.0
    cap.release()
    if not xs:
        log("no se detectó cara: centro por defecto")
        return 0.5, 0.33
    cx, cy = statistics.median(xs), statistics.median(ys)
    log(f"cara detectada en x={cx:.2f}, y={cy:.2f}")
    return cx, cy


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_subtitles(words, cuts, path):
    """Subtítulos de 1-3 palabras, grandes, en mayúsculas, remapeados a la línea de tiempo editada."""
    mapped, offset = [], 0.0
    for s, e in cuts:
        for ws, we, txt in words:
            mid = (ws + we) / 2
            if s <= mid <= e:
                mapped.append((offset + max(ws, s) - s, offset + min(we, e) - s, txt))
        offset += e - s
    lines, chunk = [], []
    for w in mapped:
        chunk.append(w)
        if len(chunk) == 3 or re.search(r"[.,;:?!]$", w[2]):
            lines.append(chunk)
            chunk = []
    if chunk:
        lines.append(chunk)
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Main,DejaVu Sans,78,&H00FFFFFF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,"
        "6,3,2,60,60,420,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for i, ch in enumerate(lines):
            start = ch[0][0]
            end = lines[i + 1][0][0] if i + 1 < len(lines) else ch[-1][1] + 0.3
            end = min(end, ch[-1][1] + 0.6)
            text = " ".join(w[2] for w in ch).upper().strip(" ,.")
            # destaca en amarillo la palabra más larga del bloque
            longest = max((w[2] for w in ch), key=len).upper().strip(" ,.")
            text = text.replace(longest, "{\\c&H0000E5FF&}" + longest + "{\\c&H00FFFFFF&}", 1)
            f.write(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Main,,0,0,0,,"
                    "{\\fad(60,0)\\t(0,90,\\fscx108\\fscy108)\\t(90,180,\\fscx100\\fscy100)}" + text + "\n")


def build(src, dst, subtitles=True, use_transcription=True):
    w, h, duration = probe(src)
    log(f"entrada {w}x{h}, {duration:.1f}s")
    segments = speech_intervals(src, duration)
    log(f"{len(segments)} frases detectadas por pausas")
    words = transcribe(src) if use_transcription else None
    if words:
        segments = remove_mistakes(segments, words)

    cuts = []
    for s, e in segments:
        s, e = max(0.0, s - PRE_PAD), min(duration, e + POST_PAD)
        if cuts and s <= cuts[-1][1]:
            cuts[-1][1] = e
        else:
            cuts.append([s, e])
    if not cuts:
        sys.exit("No se encontró voz en el video")

    cx, cy = face_center(src, duration)
    # recorte base a 9:16 alrededor de la cara si el video no es vertical
    if w / h > 9 / 16 + 0.01:
        cw = int(h * 9 / 16) // 2 * 2
        base = f"crop={cw}:{h}:{max(0, min(w - cw, int(cx * w - cw / 2)))}:0,"
        cx = (cx * w - max(0, min(w - cw, int(cx * w - cw / 2)))) / cw
    else:
        base = ""

    x = f"max(0,min(iw-iw/zoom,iw*{cx:.3f}-iw/zoom/2))"
    y = f"max(0,min(ih-ih/zoom,ih*{cy:.3f}-ih/zoom/2))"
    parts, labels = [], []
    for i, (s, e) in enumerate(cuts):
        d = e - s
        n = max(1, int(d * FPS))
        if i == 0:
            z = "1.30-0.30*min(on/15,1)"  # gancho inicial: alejamiento rápido
        elif d < 1.0:
            z = "1.50"  # frase corta: acercamiento fuerte
        else:
            z0, dz = ZOOM_PATTERN[(i - 1) % len(ZOOM_PATTERN)]
            z = f"{z0}+({dz})*min(on/{n},1)"
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS,fps={FPS},{base}"
                     f"scale={OUT_W * 2}:{OUT_H * 2}:flags=lanczos,"
                     f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUT_W}x{OUT_H}:fps={FPS},setsar=1[v{i}]")
        parts.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.02,afade=t=out:st={max(0, d - 0.03):.3f}:d=0.03[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append("".join(labels) + f"concat=n={len(cuts)}:v=1:a=1[cv][ca]")

    tmp = tempfile.mkdtemp()
    vf = GRADE
    if subtitles and words:
        ass = os.path.join(tmp, "subs.ass")
        write_subtitles(words, cuts, ass)
        vf += f",ass='{ass}'"
    parts.append(f"[cv]{vf}[outv]")
    parts.append("[ca]highpass=f=80,acompressor=threshold=-20dB:ratio=3:attack=5:release=80,"
                 "loudnorm=I=-14:TP=-1.5:LRA=9[outa]")
    script = os.path.join(tmp, "filter.txt")
    with open(script, "w") as f:
        f.write(";\n".join(parts))

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-stats", "-y", "-i", src, "-filter_complex_script", script,
                    "-map", "[outv]", "-map", "[outa]", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
                    "-maxrate", "10M", "-bufsize", "20M", "-profile:v", "high", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-movflags", "+faststart", dst], check=True)
    _, _, out_dur = probe(dst)
    log(f"listo: {dst} ({duration:.1f}s -> {out_dur:.1f}s, {len(cuts)} cortes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entrada")
    ap.add_argument("salida")
    ap.add_argument("--sin-subtitulos", action="store_true")
    ap.add_argument("--sin-transcripcion", action="store_true")
    a = ap.parse_args()
    build(a.entrada, a.salida, subtitles=not a.sin_subtitulos, use_transcription=not a.sin_transcripcion)


if __name__ == "__main__":
    main()
