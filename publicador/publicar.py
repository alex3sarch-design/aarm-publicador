"""Publicador automático de Instagram para AARM Arquitectos.

Lee las carpetas de Google Drive (Semana > Fila > Post) y publica en Instagram
los posts cuyo nombre trae la fecha de hoy, en orden y a las horas de config.json.

Comandos:
  mantener   Renueva el permiso de Instagram cuando toca (se ejecuta a diario).
  preparar   Descarga y prepara los posts de hoy en _site/ para servirlos por GitHub Pages.
  publicar   Publica en Instagram lo preparado, en orden y a su hora.
  revisar    Revisa las semanas activas sin publicar nada y deja un resumen.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageOps

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
if os.environ.get("SEMANA_REVISAR", "").strip():
    # Revisión puntual de una semana sin activarla.
    CONFIG["semanas_activas"] = [os.environ["SEMANA_REVISAR"].strip()]
TZ = ZoneInfo(CONFIG["zona_horaria"])
ESTADO_DIR = RAIZ / "estado"
PUBLICADOS = ESTADO_DIR / "publicados.json"
TOKEN_ENC = ESTADO_DIR / "token.enc"
SITE = RAIZ / "_site"
PLAN = RAIZ / "_trabajo" / "plan.json"
VERSION = CONFIG.get("api_version", "v26.0")


def es_token_instagram(token: str) -> bool:
    """Los tokens de "Instagram Login" empiezan con IG; los de Unified login (usuario del sistema), no."""
    return token.startswith("IG")


def api_base(token: str) -> str:
    host = "graph.instagram.com" if es_token_instagram(token) else "graph.facebook.com"
    return f"https://{host}/{VERSION}"

EXT_IMAGEN = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
EXT_VIDEO = {".mp4", ".mov", ".m4v"}
MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
         "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}
RATIO_MIN, RATIO_MAX = 4 / 5, 1.91  # rango que acepta la API de Instagram


# ----------------------------------------------------------------- utilidades

def log(msg: str) -> None:
    print(msg, flush=True)


def resumen(msg: str) -> None:
    """Escribe en el resumen visible de la ejecución en GitHub."""
    log(msg)
    ruta = os.environ.get("GITHUB_STEP_SUMMARY")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def salida(nombre: str, valor: str) -> None:
    ruta = os.environ.get("GITHUB_OUTPUT")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"{nombre}={valor}\n")


def sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def orden_natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def ahora() -> dt.datetime:
    return dt.datetime.now(TZ)


def hoy() -> dt.date:
    return ahora().date()


def leer_json(ruta: Path, defecto):
    if ruta.exists():
        return json.loads(ruta.read_text(encoding="utf-8"))
    return defecto


def escribir_json(ruta: Path, datos) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def guardar_en_repo(mensaje: str, *rutas: Path) -> None:
    """Hace commit y push de los archivos de estado (solo dentro de GitHub Actions)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    rel = [str(r.relative_to(RAIZ)) for r in rutas]
    subprocess.run(["git", "add", *rel], cwd=RAIZ, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=RAIZ).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", mensaje], cwd=RAIZ, check=True)
    for intento in range(4):
        subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=RAIZ)
        if subprocess.run(["git", "push", "--quiet"], cwd=RAIZ).returncode == 0:
            return
        time.sleep(5 * (intento + 1))
    raise RuntimeError("No se pudo guardar el estado en el repositorio.")


def hora_de_slot(fecha: dt.date, indice: int) -> dt.datetime:
    horas = CONFIG["horas"]
    paso = int(CONFIG.get("minutos_entre_posts", 5))
    if indice < len(horas):
        h, m = map(int, horas[indice].split(":"))
        return dt.datetime(fecha.year, fecha.month, fecha.day, h, m, tzinfo=TZ)
    ultima = hora_de_slot(fecha, len(horas) - 1)
    return ultima + dt.timedelta(minutes=paso * (indice - len(horas) + 1))


# ----------------------------------------------------------------- Google Drive

_drive = None


def drive():
    global _drive
    if _drive is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = json.loads(os.environ["GOOGLE_SA_JSON"])
        cred = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        _drive = build("drive", "v3", credentials=cred, cache_discovery=False)
    return _drive


def listar(carpeta_id: str) -> list[dict]:
    items, token = [], None
    while True:
        r = drive().files().list(
            q=f"'{carpeta_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, shortcutDetails)",
            pageSize=1000, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in r.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.shortcut":
                d = f.get("shortcutDetails", {})
                f = {**f, "id": d.get("targetId", f["id"]), "mimeType": d.get("targetMimeType", "")}
            items.append(f)
        token = r.get("nextPageToken")
        if not token:
            return sorted(items, key=lambda x: orden_natural(x["name"]))


def es_carpeta(f: dict) -> bool:
    return f["mimeType"] == "application/vnd.google-apps.folder"


def descargar(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    req = drive().files().get_media(fileId=file_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req, chunksize=20 * 1024 * 1024)
    hecho = False
    while not hecho:
        _, hecho = dl.next_chunk(num_retries=3)
    return buf.getvalue()


RE_POST = re.compile(r"^\s*(\d+)\s*-\s*[^\d\s-]+\.?\s+(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", re.UNICODE)


def leer_fecha(nombre: str):
    """'3 - Mar 17 nov 2026 - 15 Carrusel - ...' -> (3, date(2026, 11, 17))"""
    m = RE_POST.match(sin_acentos(nombre))
    if not m:
        return None
    num, dia, mes, anio = m.groups()
    mes_n = MESES.get(mes.lower()[:3])
    if not mes_n:
        return None
    try:
        return int(num), dt.date(int(anio), mes_n, int(dia))
    except ValueError:
        return None


def posts_de_semanas() -> tuple[list[dict], list[str]]:
    """Devuelve todos los posts de las semanas activas y los avisos encontrados."""
    avisos: list[str] = []
    activas = CONFIG.get("semanas_activas", [])
    if not activas:
        return [], ["No hay semanas activas en config.json."]
    raiz = listar(CONFIG["drive_carpeta_raiz"])
    posts = []
    for prefijo in activas:
        semanas = [f for f in raiz if es_carpeta(f) and f["name"].lower().startswith(prefijo.lower())]
        if len(semanas) != 1:
            avisos.append(f"No encontré una única carpeta que empiece con «{prefijo}» (encontradas: {len(semanas)}).")
            continue
        semana = semanas[0]
        for fila in (f for f in listar(semana["id"]) if es_carpeta(f)):
            for post in (f for f in listar(fila["id"]) if es_carpeta(f)):
                fecha = leer_fecha(post["name"])
                if not fecha:
                    avisos.append(f"No pude leer número y fecha de «{semana['name']} / {fila['name']} / {post['name']}».")
                    continue
                posts.append({"id": post["id"], "nombre": post["name"], "numero": fecha[0],
                              "fecha": fecha[1].isoformat(), "semana": semana["name"], "fila": fila["name"]})
    # índice de horario dentro de cada día
    por_dia: dict[str, list[dict]] = {}
    for p in posts:
        por_dia.setdefault(p["fecha"], []).append(p)
    for fecha, lista in por_dia.items():
        lista.sort(key=lambda p: p["numero"])
        numeros = [p["numero"] for p in lista]
        if len(set(numeros)) != len(numeros):
            avisos.append(f"El {fecha} hay posts con el mismo número: {numeros}.")
        for i, p in enumerate(lista):
            p["slot"] = i
    posts.sort(key=lambda p: (p["fecha"], p["numero"]))
    return posts, avisos


def contenido_post(post_id: str) -> dict:
    archivos = [f for f in listar(post_id) if not es_carpeta(f)]
    textos = [f for f in archivos if Path(f["name"]).suffix.lower() == ".txt"]
    portada = [f for f in archivos if Path(f["name"]).stem.lower() in ("portada", "cover")]
    medios = [f for f in archivos if f not in portada and Path(f["name"]).suffix.lower() in EXT_IMAGEN | EXT_VIDEO]
    return {"texto": textos, "medios": medios, "portada": portada[:1]}


def leer_texto(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            texto = data.decode(enc)
            if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return texto.replace("\r\n", "\n").strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def tipo_de(medios: list[dict]) -> str:
    if len(medios) == 1:
        return "REEL" if Path(medios[0]["name"]).suffix.lower() in EXT_VIDEO else "IMAGEN"
    return "CARRUSEL"


# ----------------------------------------------------------------- imágenes

def color_rgb(hexa: str) -> tuple[int, int, int]:
    h = hexa.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def a_jpeg(data: bytes) -> tuple[bytes, bytes | None, tuple[int, int]]:
    """Convierte a JPEG. Devuelve (original, versión con franjas si hace falta, tamaño)."""
    try:
        import pillow_heif  # noqa: F401
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    fondo = color_rgb(CONFIG.get("color_franjas", "#FFFFFF"))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        base = Image.new("RGB", img.size, fondo)
        base.paste(img, mask=img.split()[-1])
        img = base
    else:
        img = img.convert("RGB")
    if img.width > 1440:
        img = img.resize((1440, round(img.height * 1440 / img.width)), Image.LANCZOS)

    def guardar(im: Image.Image) -> bytes:
        for calidad in (95, 90, 85, 80, 75):
            b = io.BytesIO()
            im.save(b, "JPEG", quality=calidad, optimize=True, subsampling=0 if calidad >= 90 else 2)
            if b.tell() < 8 * 1024 * 1024:
                return b.getvalue()
        return b.getvalue()

    original = guardar(img)
    ratio = img.width / img.height
    con_franjas = None
    if ratio < RATIO_MIN:  # más alta que 4:5 -> franjas laterales
        ancho = math.ceil(img.height * RATIO_MIN)
        lienzo = Image.new("RGB", (ancho, img.height), fondo)
        lienzo.paste(img, ((ancho - img.width) // 2, 0))
        if lienzo.width > 1440:
            lienzo = lienzo.resize((1440, round(lienzo.height * 1440 / lienzo.width)), Image.LANCZOS)
        con_franjas = guardar(lienzo)
    elif ratio > RATIO_MAX:  # más ancha que 1.91:1 -> franjas arriba y abajo
        alto = math.ceil(img.width / RATIO_MAX)
        lienzo = Image.new("RGB", (img.width, alto), fondo)
        lienzo.paste(img, (0, (alto - img.height) // 2))
        con_franjas = guardar(lienzo)
    return original, con_franjas, img.size


# ----------------------------------------------------------------- token de Instagram

def _clave() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    base = os.environ.get("CLAVE_TOKEN") or os.environ.get("GOOGLE_SA_JSON", "")
    if not base:
        raise RuntimeError("Falta el secreto GOOGLE_SA_JSON (o CLAVE_TOKEN) para cifrar el permiso.")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"aarm-publicador", iterations=390000)
    return base64.urlsafe_b64encode(kdf.derive(base.encode()))


def _huella(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cargar_token() -> dict:
    from cryptography.fernet import Fernet, InvalidToken

    secreto = os.environ.get("IG_TOKEN", "").strip()
    guardado = None
    if TOKEN_ENC.exists():
        try:
            guardado = json.loads(Fernet(_clave()).decrypt(TOKEN_ENC.read_bytes()))
        except (InvalidToken, ValueError):
            log("Aviso: no se pudo leer estado/token.enc; uso el secreto IG_TOKEN.")
    if guardado and (not secreto or guardado.get("huella_secreto") == _huella(secreto)):
        return guardado
    if not secreto:
        raise RuntimeError("Falta el secreto IG_TOKEN en GitHub.")
    # Secreto nuevo o primera vez: se toma tal cual.
    return {"token": secreto, "obtenido": None, "expira": None, "huella_secreto": _huella(secreto)}


def guardar_token(datos: dict) -> None:
    from cryptography.fernet import Fernet

    TOKEN_ENC.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_ENC.write_bytes(Fernet(_clave()).encrypt(json.dumps(datos).encode()))


def mantener() -> None:
    datos = cargar_token()
    if not es_token_instagram(datos["token"]):
        # Token de usuario del sistema (Unified login): no caduca. Solo se comprueba que siga válido.
        cuenta(datos["token"])
        log("Permiso de Instagram (usuario del sistema) válido; no necesita renovarse.")
        return
    obtenido = dt.datetime.fromisoformat(datos["obtenido"]) if datos.get("obtenido") else None
    if obtenido is None:
        # Permiso recién guardado en GitHub: se anota y se renueva dentro de 3 días
        # (Instagram no deja renovar uno con menos de 24 h).
        datos["obtenido"] = ahora().isoformat()
        guardar_token(datos)
        guardar_en_repo("Registrar permiso de Instagram", TOKEN_ENC)
        log("Permiso de Instagram nuevo registrado.")
        return
    if ahora() - obtenido < dt.timedelta(days=3):
        log(f"Permiso de Instagram vigente (renovado {obtenido:%Y-%m-%d}).")
        return
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": datos["token"]}, timeout=60)
    if r.status_code != 200:
        msg = r.text[:500]
        raise RuntimeError(f"No se pudo renovar el permiso de Instagram: {msg}")
    j = r.json()
    datos.update(token=j["access_token"], obtenido=ahora().isoformat(),
                 expira=(ahora() + dt.timedelta(seconds=int(j.get("expires_in", 0)))).isoformat())
    guardar_token(datos)
    guardar_en_repo("Renovar permiso de Instagram", TOKEN_ENC)
    log(f"Permiso de Instagram renovado; vence {datos['expira'][:10]}.")


# ----------------------------------------------------------------- API de Instagram

class ErrorIG(Exception):
    def __init__(self, respuesta: dict | str):
        self.respuesta = respuesta
        super().__init__(json.dumps(respuesta, ensure_ascii=False) if isinstance(respuesta, dict) else respuesta)

    @property
    def es_proporcion(self) -> bool:
        e = self.respuesta.get("error", {}) if isinstance(self.respuesta, dict) else {}
        texto = json.dumps(e).lower()
        return e.get("error_subcode") in (2207009,) or "aspect ratio" in texto


def ig(metodo: str, ruta: str, token: str, **params) -> dict:
    url = f"{api_base(token)}/{ruta}"
    for intento in range(3):
        try:
            if metodo == "GET":
                r = requests.get(url, params={**params, "access_token": token}, timeout=120)
            else:
                r = requests.post(url, data={**params, "access_token": token}, timeout=120)
        except requests.RequestException as e:
            if intento == 2:
                raise ErrorIG(str(e))
            time.sleep(10)
            continue
        try:
            j = r.json()
        except ValueError:
            j = {"error": {"message": r.text[:300]}}
        if r.status_code == 200 and "error" not in j:
            return j
        err = j.get("error", {})
        if err.get("is_transient") and intento < 2:
            time.sleep(20)
            continue
        raise ErrorIG(j)
    raise ErrorIG("Sin respuesta de Instagram")


def cuenta(token: str) -> tuple[str, str]:
    """Devuelve (id de la cuenta de Instagram, usuario) según el tipo de token."""
    if es_token_instagram(token):
        yo = ig("GET", "me", token, fields="user_id,username")
        return str(yo.get("user_id") or yo.get("id")), yo.get("username", "")
    fijo = str(CONFIG.get("ig_user_id") or "").strip()
    if fijo:
        info = ig("GET", fijo, token, fields="id,username")
        return fijo, info.get("username", "")
    try:
        datos = ig("GET", "me/assigned_instagram_accounts", token, fields="id,username").get("data", [])
        if datos:
            return str(datos[0]["id"]), datos[0].get("username", "")
    except ErrorIG:
        pass
    paginas = ig("GET", "me/accounts", token, fields="instagram_business_account{id,username}").get("data", [])
    for pg in paginas:
        iba = pg.get("instagram_business_account")
        if iba:
            return str(iba["id"]), iba.get("username", "")
    raise ErrorIG("No encontré la cuenta de Instagram con este permiso; agrega su id en config.json → ig_user_id.")


def esperar_contenedor(cid: str, token: str, minutos: int) -> None:
    fin = time.time() + minutos * 60
    while True:
        estado = ig("GET", cid, token, fields="status_code,status").get("status_code")
        if estado == "FINISHED":
            return
        if estado in ("ERROR", "EXPIRED"):
            raise ErrorIG(ig("GET", cid, token, fields="status_code,status"))
        if time.time() > fin:
            raise ErrorIG(f"Instagram tardó demasiado en procesar el contenedor {cid} (estado {estado}).")
        time.sleep(15)


def crear_contenedores(post: dict, ig_id: str, token: str, usar_franjas: bool) -> str:
    def url_imagen(m):
        return m["url_franjas"] if usar_franjas and m.get("url_franjas") else m["url"]

    medios = post["medios"]
    texto = post["texto"]
    if post["tipo"] == "IMAGEN":
        cid = ig("POST", f"{ig_id}/media", token, image_url=url_imagen(medios[0]), caption=texto)["id"]
        esperar_contenedor(cid, token, 5)
        return cid
    if post["tipo"] == "REEL":
        params = {"media_type": "REELS", "video_url": medios[0]["url"], "caption": texto, "share_to_feed": "true"}
        if post.get("portada_url"):
            params["cover_url"] = post["portada_url"]
        cid = ig("POST", f"{ig_id}/media", token, **params)["id"]
        esperar_contenedor(cid, token, 20)
        return cid
    hijos = []
    for m in medios:
        if m["es_video"]:
            hid = ig("POST", f"{ig_id}/media", token, media_type="VIDEO", video_url=m["url"],
                     is_carousel_item="true")["id"]
            esperar_contenedor(hid, token, 20)
        else:
            hid = ig("POST", f"{ig_id}/media", token, image_url=url_imagen(m), is_carousel_item="true")["id"]
            esperar_contenedor(hid, token, 5)
        hijos.append(hid)
    cid = ig("POST", f"{ig_id}/media", token, media_type="CAROUSEL", children=",".join(hijos), caption=texto)["id"]
    esperar_contenedor(cid, token, 5)
    return cid


def publicar_un_post(post: dict, ig_id: str, token: str) -> str:
    hay_franjas = any(m.get("url_franjas") for m in post["medios"])
    try:
        cid = crear_contenedores(post, ig_id, token, usar_franjas=False)
    except ErrorIG as e:
        if not (hay_franjas and e.es_proporcion):
            raise
        log("  Instagram no aceptó la proporción original; uso la versión con franjas laterales.")
        cid = crear_contenedores(post, ig_id, token, usar_franjas=True)
    for intento in range(3):
        try:
            return ig("POST", f"{ig_id}/media_publish", token, creation_id=cid)["id"]
        except ErrorIG:
            # Antes de reintentar, comprobar que no se haya publicado ya (evita duplicados).
            time.sleep(30)
            if ig("GET", cid, token, fields="status_code").get("status_code") == "PUBLISHED":
                return ig("GET", cid, token, fields="id").get("id", cid)
            if intento == 2:
                raise
    raise ErrorIG("No se pudo publicar")


# ----------------------------------------------------------------- comandos

def preparar() -> None:
    publicados = leer_json(PUBLICADOS, {})
    posts, avisos = posts_de_semanas()
    for a in avisos:
        resumen(f"⚠️ {a}")
    fecha = hoy()
    del_dia = [p for p in posts if p["fecha"] == fecha.isoformat()]
    vencidos = [p for p in posts if p["fecha"] < fecha.isoformat() and p["id"] not in publicados]
    for p in vencidos:
        resumen(f"⚠️ Sin publicar de un día anterior: {p['semana']} / {p['fila']} / {p['nombre']}")
    pendientes = [p for p in del_dia if p["id"] not in publicados]
    if not pendientes:
        resumen(f"Nada que publicar hoy ({fecha:%d/%m/%Y}).")
        salida("hay", "false")
        return

    clave = secrets.token_hex(16)  # carpeta aleatoria: nadie puede adivinar las direcciones
    destino = SITE / clave
    destino.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text("", encoding="utf-8")
    base = CONFIG["pages_url"].rstrip("/") + f"/{clave}"
    plan = []
    for p in pendientes:
        c = contenido_post(p["id"])
        if not c["medios"]:
            raise RuntimeError(f"El post «{p['nombre']}» no tiene imágenes ni video.")
        if len(c["medios"]) > 10:
            raise RuntimeError(f"El carrusel «{p['nombre']}» tiene {len(c['medios'])} archivos; Instagram admite 10.")
        texto = leer_texto(descargar(c["texto"][0]["id"])) if c["texto"] else ""
        if not c["texto"]:
            resumen(f"⚠️ «{p['nombre']}» no tiene descripcion.txt; se publicará sin texto.")
        if len(texto) > 2200:
            raise RuntimeError(f"El texto de «{p['nombre']}» tiene {len(texto)} caracteres; Instagram admite 2200.")
        medios = []
        for i, m in enumerate(c["medios"], 1):
            ext = Path(m["name"]).suffix.lower()
            data = descargar(m["id"])
            nombre_base = f"p{p['numero']}_{i:02d}"
            if ext in EXT_VIDEO:
                ruta = destino / f"{nombre_base}{'.mp4' if ext == '.m4v' else ext}"
                ruta.write_bytes(data)
                medios.append({"url": f"{base}/{ruta.name}", "es_video": True, "archivo": m["name"]})
            else:
                original, franjas, tam = a_jpeg(data)
                (destino / f"{nombre_base}.jpg").write_bytes(original)
                item = {"url": f"{base}/{nombre_base}.jpg", "es_video": False, "archivo": m["name"], "tamano": tam}
                if franjas:
                    (destino / f"{nombre_base}_f.jpg").write_bytes(franjas)
                    item["url_franjas"] = f"{base}/{nombre_base}_f.jpg"
                medios.append(item)
        portada_url = None
        if c["portada"]:
            original, _, _ = a_jpeg(descargar(c["portada"][0]["id"]))
            (destino / f"p{p['numero']}_portada.jpg").write_bytes(original)
            portada_url = f"{base}/p{p['numero']}_portada.jpg"
        tipo = tipo_de(c["medios"])
        plan.append({**p, "tipo": tipo, "texto": texto, "medios": medios, "portada_url": portada_url,
                     "hora": hora_de_slot(fecha, p["slot"]).isoformat()})
        resumen(f"- Preparado {p['numero']} · {tipo} · {len(medios)} archivo(s) · {hora_de_slot(fecha, p['slot']):%H:%M} · {p['nombre']}")
    escribir_json(PLAN, plan)
    salida("hay", "true")


def esperar_urls(plan: list[dict]) -> None:
    urls = [m["url"] for p in plan for m in p["medios"]]
    fin = time.time() + 600
    pendientes = list(urls)
    while pendientes and time.time() < fin:
        pendientes = [u for u in pendientes if requests.head(u, timeout=30, allow_redirects=True).status_code != 200]
        if pendientes:
            time.sleep(15)
    if pendientes:
        raise RuntimeError(f"Las imágenes no están disponibles en GitHub Pages: {pendientes[:3]}")


def publicar() -> None:
    plan = leer_json(PLAN, [])
    if not plan:
        log("No hay plan que publicar.")
        return
    publicados = leer_json(PUBLICADOS, {})
    token = cargar_token()["token"]
    ig_id, usuario = cuenta(token)
    log(f"Cuenta de Instagram: @{usuario}")
    esperar_urls(plan)
    ultimo = 0.0
    for post in plan:
        if post["id"] in publicados:
            continue
        objetivo = dt.datetime.fromisoformat(post["hora"])
        espera = (objetivo - ahora()).total_seconds()
        if espera > 0:
            log(f"Esperando hasta las {objetivo:%H:%M} para el post {post['numero']}…")
            time.sleep(espera)
        # Aunque vayamos tarde, deja al menos 1 minuto entre posts para respetar el orden.
        if ultimo and time.time() - ultimo < 60:
            time.sleep(60 - (time.time() - ultimo))
        log(f"Publicando {post['numero']} · {post['tipo']} · {post['nombre']}")
        try:
            media_id = publicar_un_post(post, ig_id, token)
        except Exception as e:
            resumen(f"❌ Falló el post {post['numero']} «{post['nombre']}»: {e}")
            resumen("Se detiene aquí para no publicar los siguientes fuera de orden.")
            sys.exit(1)
        ultimo = time.time()
        publicados[post["id"]] = {"nombre": post["nombre"], "semana": post["semana"], "fecha": post["fecha"],
                                  "media_id": media_id, "publicado": ahora().isoformat(timespec="seconds")}
        escribir_json(PUBLICADOS, publicados)
        guardar_en_repo(f"Publicado: {post['nombre']}", PUBLICADOS)
        resumen(f"✅ {ahora():%H:%M} · publicado {post['numero']} «{post['nombre']}»")


def probar_preparar() -> None:
    """Genera una imagen de prueba 3:4 en _site/ (no toca Drive)."""
    clave = secrets.token_hex(16)
    destino = SITE / clave
    destino.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text("", encoding="utf-8")
    img = Image.new("RGB", (1080, 1440), (233, 226, 216))
    b = io.BytesIO()
    img.save(b, "JPEG", quality=90)
    (destino / "prueba_3x4.jpg").write_bytes(b.getvalue())
    original, franjas, _ = a_jpeg(b.getvalue())
    (destino / "prueba_4x5.jpg").write_bytes(franjas or original)
    base = CONFIG["pages_url"].rstrip("/") + f"/{clave}"
    escribir_json(PLAN, [{"prueba": True, "medios": [{"url": f"{base}/prueba_3x4.jpg"},
                                                     {"url": f"{base}/prueba_4x5.jpg"}]}])
    salida("hay", "true")


def probar() -> None:
    """Comprueba el permiso y si Instagram acepta 3:4. Crea contenedores pero NUNCA publica."""
    plan = leer_json(PLAN, [])
    token = cargar_token()["token"]
    ig_id, usuario = cuenta(token)
    resumen(f"✅ Permiso válido para @{usuario} (id {ig_id}, {'Instagram Login' if es_token_instagram(token) else 'usuario del sistema'})")
    try:
        lim = ig("GET", f"{ig_id}/content_publishing_limit", token, fields="quota_usage,config")
        resumen(f"✅ Permiso de publicación activo (uso en 24 h: {lim.get('data', [{}])[0].get('quota_usage', '?')})")
    except ErrorIG as e:
        resumen(f"⚠️ No pude consultar el límite de publicación: {e}")
    esperar_urls(plan)
    u34, u45 = (m["url"] for m in plan[0]["medios"])
    for nombre, url in (("3:4 (1080×1440)", u34), ("4:5 con franjas", u45)):
        try:
            cid = ig("POST", f"{ig_id}/media", token, image_url=url, caption="prueba")["id"]
            esperar_contenedor(cid, token, 3)
            resumen(f"✅ Instagram ACEPTA {nombre} (contenedor de prueba {cid}, no se publicó)")
        except ErrorIG as e:
            resumen(f"❌ Instagram RECHAZA {nombre}: {e}")


def revisar() -> None:
    """Revisa todas las semanas activas sin publicar: fechas, tipos, textos y proporciones."""
    if not CONFIG.get("semanas_activas"):
        resumen("No hay semanas activas todavía; nada que revisar.")
        return
    publicados = leer_json(PUBLICADOS, {})
    posts, avisos = posts_de_semanas()
    resumen(f"## Revisión de semanas activas: {', '.join(CONFIG.get('semanas_activas', [])) or '(ninguna)'}")
    for a in avisos:
        resumen(f"⚠️ {a}")
    problemas = len(avisos)
    dia_actual = None
    for p in posts:
        if p["fecha"] != dia_actual:
            dia_actual = p["fecha"]
            f = dt.date.fromisoformat(dia_actual)
            resumen(f"\n**{f:%A %d/%m/%Y}**")
        c = contenido_post(p["id"])
        notas = []
        if not c["medios"]:
            notas.append("sin imágenes ni video"); problemas += 1
        if len(c["medios"]) > 10:
            notas.append(f"{len(c['medios'])} archivos (máx. 10)"); problemas += 1
        if not c["texto"]:
            notas.append("sin descripcion.txt")
        else:
            n = len(leer_texto(descargar(c["texto"][0]["id"])))
            notas.append(f"texto {n} caracteres")
            if n > 2200:
                notas.append("texto demasiado largo (máx. 2200)"); problemas += 1
        if c["medios"] and not Path(c["medios"][0]["name"]).suffix.lower() in EXT_VIDEO:
            try:
                _, franjas, tam = a_jpeg(descargar(c["medios"][0]["id"]))
                notas.append(f"{tam[0]}×{tam[1]}" + (" (franjas si Instagram lo pide)" if franjas else ""))
            except Exception as e:
                notas.append(f"imagen ilegible: {e}"); problemas += 1
        estado = "✅ ya publicado" if p["id"] in publicados else hora_de_slot(dt.date.fromisoformat(p["fecha"]), p["slot"]).strftime("%H:%M")
        tipo = tipo_de(c["medios"]) if c["medios"] else "?"
        resumen(f"- {estado} · {p['numero']} · {tipo} · {len(c['medios'])} archivo(s) · {', '.join(notas)} · {p['nombre']}")
    resumen(f"\nTotal: {len(posts)} posts · problemas: {problemas}")
    if problemas:
        sys.exit(1)


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    acciones = {"mantener": mantener, "preparar": preparar, "publicar": publicar, "revisar": revisar,
                "probar-preparar": probar_preparar, "probar": probar}
    if comando not in acciones:
        print(__doc__)
        sys.exit(2)
    acciones[comando]()
