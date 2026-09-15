"""Protocolo de subida resumable de YouTube. Solo el transporte.

Este módulo sabe hablar el protocolo y **nada más**: no decide si una subida
debe empezar, no orquesta reintentos y no interpreta el resultado en términos de
publicación. Eso es de ``publisher.py``, y la separación es la que exige el
Gate: un módulo que supiera de las dos cosas acabaría reintentando por su cuenta
una subida cuyo desenlace no consta, que es justo lo que no puede ocurrir.

Qué es el protocolo, en la parte que aquí se usa:

* **Iniciar.** ``POST`` a ``/upload/youtube/v3/videos?uploadType=resumable`` con
  la metadata en el cuerpo y las cabeceras ``X-Upload-Content-Length`` y
  ``X-Upload-Content-Type``. La respuesta trae el URL de sesión en ``Location``.
* **Subir un fragmento.** ``PUT`` al URL de sesión con
  ``Content-Range: bytes <inicio>-<fin>/<total>``. Un fragmento intermedio
  aceptado responde ``308`` y la cabecera ``Range: bytes=0-<n>`` dice hasta qué
  byte consta recibido. El último fragmento aceptado responde ``200`` o ``201``
  con el recurso del vídeo, del que sale el ``video_id``.
* **Consultar el progreso.** ``PUT`` al URL de sesión con
  ``Content-Range: bytes */<total>`` y cuerpo vacío. Responde ``308`` con el
  ``Range`` recibido, o ``200``/``201`` si la subida ya se había completado.

**Esa consulta es el único mecanismo de reconciliación documentado, y exige
tener el URL de sesión.** No existe ninguna llamada que responda «¿se completó
la sesión X?» sin él. Lo que este módulo no puede averiguar, no lo deduce.

Sobre el transporte: se inyecta. La implementación real usa ``urllib`` de la
biblioteca estándar, igual que ``auth.py`` y el adaptador de LLM, para no
introducir una segunda forma de hacer HTTP en el proyecto. Los tests inyectan un
transporte propio y así el protocolo se comprueba entero sin red.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from uuid import UUID

from app.contracts.models import PublishMetadata, UploadSession
from app.core.errors import (
    ErrorPermanente,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
    TiempoAgotado,
)
from app.adapters.youtube.auth import (
    CODIGOS_TRANSITORIOS,
    RAZON_ALCANCE_INSUFICIENTE,
    RAZON_CUOTA,
    TIMEOUT_POR_DEFECTO_S,
    AutorizacionInvalida,
    TokenAcceso,
)

#: Endpoint de subida. Es el único de este módulo que escribe en YouTube.
URL_SUBIDA = "https://www.googleapis.com/upload/youtube/v3/videos"

#: Endpoint de lectura, para la verificación posterior.
URL_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"

#: Tamaño de fragmento por defecto. Múltiplo de 256 KiB, como pide el protocolo
#: para los fragmentos que no son el último.
TAMANO_FRAGMENTO_BYTES = 8 * 1024 * 1024

#: Múltiplo obligatorio del tamaño de un fragmento intermedio.
MULTIPLO_FRAGMENTO = 256 * 1024

#: Códigos con los que el proveedor da la subida por terminada.
CODIGOS_COMPLETADO = frozenset({200, 201})

#: Código con el que el proveedor dice «sigo esperando bytes».
CODIGO_INCOMPLETO = 308

#: Códigos con los que una sesión deja de existir. **No dicen si el vídeo llegó
#: a crearse**, así que no autorizan ninguna conclusión sobre el desenlace.
CODIGOS_SESION_MUERTA = frozenset({404, 410})


class SesionDesconocida(ErrorPermanente):
    """La sesión ya no existe para el proveedor.

    Es permanente porque insistir sobre la misma sesión no la revive. **No
    significa que la subida fallara:** una sesión puede desaparecer después de
    haberse completado, así que quien reciba esto no puede concluir nada sobre
    si el vídeo existe. Esa incertidumbre se resuelve en ``reconciliation.py``,
    y su desenlace honesto es ``UNKNOWN``.
    """


class RespuestaPerdida(ErrorTransitorio):
    """Se envió la petición y no se supo qué contestó el proveedor.

    Es el caso central de este Gate. El transporte murió después de mandar los
    bytes, así que **el proveedor pudo haberlos aceptado**. Tratarlo como un
    fallo sería el error que Gate 7.2 existe para impedir.

    ``fragmento_final`` dice si lo que se perdió era la respuesta al último
    fragmento, que es cuando el vídeo pudo quedar creado. ``bytes_enviados``
    dice cuántos bytes se habían dado por confirmados antes de la pérdida.

    Hereda de ``ErrorTransitorio`` por su naturaleza —la red falló—, pero quien
    lo capture **no debe reintentar la subida**: debe reconciliar. La distinción
    está escrita en ``publisher.py``.
    """

    def __init__(
        self,
        mensaje: str,
        *,
        fragmento_final: bool = False,
        bytes_enviados: int = 0,
        stage: str | None = None,
    ) -> None:
        super().__init__(mensaje, stage=stage)
        self.fragmento_final = fragmento_final
        self.bytes_enviados = bytes_enviados


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RespuestaHTTP:
    """Lo mínimo del protocolo: código, cabeceras y cuerpo."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def cabecera(self, nombre: str) -> str:
        """Lee una cabecera sin distinguir mayúsculas."""
        objetivo = nombre.lower()
        for clave, valor in self.headers.items():
            if clave.lower() == objetivo:
                return valor
        return ""

    def json(self) -> dict:
        try:
            datos = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RespuestaInvalida(
                "la respuesta del proveedor no es JSON válido"
            ) from exc
        if not isinstance(datos, dict):
            raise RespuestaInvalida("la respuesta del proveedor no es un objeto JSON")
        return datos


class Transporte(Protocol):
    """Cómo se habla con el proveedor.

    Se inyecta para que el protocolo pueda comprobarse entero sin red. Una
    implementación que quiera simular una respuesta perdida levanta
    ``RespuestaPerdida``; una que quiera simular un timeout levanta
    ``TiempoAgotado``.
    """

    def peticion(
        self,
        metodo: str,
        url: str,
        *,
        headers: Mapping[str, str],
        cuerpo: bytes | None = None,
        timeout_s: int = TIMEOUT_POR_DEFECTO_S,
    ) -> RespuestaHTTP: ...


class TransporteUrllib:
    """Transporte real, sobre ``urllib``. Sin dependencias nuevas."""

    def peticion(
        self,
        metodo: str,
        url: str,
        *,
        headers: Mapping[str, str],
        cuerpo: bytes | None = None,
        timeout_s: int = TIMEOUT_POR_DEFECTO_S,
    ) -> RespuestaHTTP:
        peticion = urllib.request.Request(
            url, data=cuerpo, headers=dict(headers), method=metodo
        )
        try:
            with urllib.request.urlopen(peticion, timeout=timeout_s) as respuesta:
                return RespuestaHTTP(
                    status=respuesta.status,
                    headers=dict(respuesta.headers.items()),
                    body=respuesta.read(),
                )
        except urllib.error.HTTPError as exc:
            # El 308 llega como HTTPError: es la respuesta normal de un fragmento
            # intermedio aceptado, no un fallo.
            cuerpo_error = b""
            try:
                cuerpo_error = exc.read()
            except (OSError, AttributeError):  # pragma: no cover - defensivo
                pass
            return RespuestaHTTP(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=cuerpo_error,
            )
        except socket.timeout as exc:
            raise TiempoAgotado(
                f"el proveedor no respondió en {timeout_s}s"
            ) from exc
        except urllib.error.URLError as exc:
            raise ErrorTransitorio("no se pudo contactar con el proveedor") from exc


# ---------------------------------------------------------------------------
# Clasificación de respuestas
# ---------------------------------------------------------------------------


def _etiqueta_de_error(respuesta: RespuestaHTTP) -> str:
    """Saca la etiqueta corta del error y **nada más** del cuerpo.

    Mismo criterio que ``auth.py``: el cuerpo puede repetir lo que se envió, así
    que solo se conserva la razón, que es un valor corto y conocido.
    """
    try:
        datos = json.loads(respuesta.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    error = datos.get("error") if isinstance(datos, dict) else None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for detalle in error.get("errors") or []:
            if isinstance(detalle, dict) and detalle.get("reason"):
                return str(detalle["reason"])
        if error.get("status"):
            return str(error["status"])
    return ""


def _clasificar_fallo(respuesta: RespuestaHTTP, *, que: str) -> None:
    """Convierte una respuesta de error en la excepción que le corresponde.

    Ningún mensaje lleva el cuerpo de la respuesta, ni el URL de sesión, ni la
    credencial: solo el código y la etiqueta corta.
    """
    codigo = respuesta.status
    etiqueta = _etiqueta_de_error(respuesta)
    sufijo = f", {etiqueta}" if etiqueta else ""

    if codigo == 429 or etiqueta == RAZON_CUOTA:
        raise LimiteDeTasa(f"el proveedor limitó la petición de {que} ({codigo}{sufijo})")
    if codigo in CODIGOS_TRANSITORIOS:
        raise ErrorTransitorio(f"el proveedor respondió {codigo} a {que}")
    if codigo in CODIGOS_SESION_MUERTA:
        raise SesionDesconocida(
            f"el proveedor no reconoce la sesión al {que} ({codigo}{sufijo}); "
            f"esto no dice si el vídeo llegó a crearse"
        )
    if etiqueta == RAZON_ALCANCE_INSUFICIENTE:
        raise AutorizacionInvalida(
            f"el alcance del token no cubre {que} ({codigo} {etiqueta})"
        )
    if codigo in (401, 403):
        raise AutorizacionInvalida(
            f"la autorización no es utilizable para {que} ({codigo}{sufijo})"
        )
    raise RespuestaInvalida(f"el proveedor rechazó {que} con {codigo}{sufijo}")


def _bytes_confirmados(respuesta: RespuestaHTTP) -> int:
    """Lee cuántos bytes constan recibidos, de la cabecera ``Range``.

    Su ausencia significa cero, y es la respuesta normal a una sesión recién
    abierta: el protocolo omite ``Range`` cuando no ha llegado ningún byte.
    """
    rango = respuesta.cabecera("Range").strip()
    if not rango:
        return 0
    # Formato: ``bytes=0-<ultimo>``. El valor es el índice del último byte
    # recibido, así que la cuenta es uno más.
    _, _, tramo = rango.partition("=")
    _, _, ultimo = tramo.partition("-")
    try:
        return int(ultimo) + 1
    except ValueError:
        return 0


def _cuerpo_metadata(metadata: PublishMetadata) -> bytes:
    """El recurso ``video`` que espera ``videos.insert``, en JSON.

    Solo lleva lo que la metadata declara. No se inventa ningún campo, y
    ``privacyStatus`` sale de la intención registrada, nunca de un valor por
    defecto de este módulo.
    """
    snippet: dict = {
        "title": metadata.title,
        "description": metadata.description,
        "defaultLanguage": metadata.language,
    }
    if metadata.tags:
        snippet["tags"] = list(metadata.tags)
    if metadata.category_id:
        snippet["categoryId"] = metadata.category_id
    recurso = {
        "snippet": snippet,
        "status": {
            "privacyStatus": metadata.privacy_status.value,
            "selfDeclaredMadeForKids": metadata.made_for_kids,
        },
    }
    return json.dumps(recurso, ensure_ascii=False).encode("utf-8")


def huella_metadata(metadata: PublishMetadata) -> str:
    """SHA-256 del cuerpo que se enviará. Ata la sesión a una intención concreta.

    Se calcula sobre el JSON serializado y no sobre el modelo, porque lo que el
    proveedor recibe es exactamente eso: si cambia el título, cambia la huella.
    """
    import hashlib

    return hashlib.sha256(_cuerpo_metadata(metadata)).hexdigest()


# ---------------------------------------------------------------------------
# Iniciar la sesión
# ---------------------------------------------------------------------------


def iniciar_sesion(
    token: TokenAcceso,
    metadata: PublishMetadata,
    *,
    run_id: UUID,
    attempt: int,
    total_bytes: int,
    video_sha256: str,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
) -> UploadSession:
    """Abre una sesión resumable y devuelve su ``UploadSession``.

    No sube ningún byte: solo negocia dónde subirlos. Si la respuesta se pierde
    aquí, **no se creó ningún vídeo** —no se envió contenido—, y eso es lo que
    permite que el desenlace sea ``CONFIRMED_NOT_UPLOADED`` en vez de ``UNKNOWN``.
    """
    if total_bytes <= 0:
        raise ErrorPermanente("no se abre una sesión para un archivo vacío")

    cuerpo = _cuerpo_metadata(metadata)
    parametros = urllib.parse.urlencode(
        {"uploadType": "resumable", "part": "snippet,status"}
    )
    respuesta = transporte.peticion(
        "POST",
        f"{URL_SUBIDA}?{parametros}",
        headers={
            "Authorization": f"Bearer {token.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(total_bytes),
            "X-Upload-Content-Type": "video/mp4",
        },
        cuerpo=cuerpo,
        timeout_s=timeout_s,
    )

    if respuesta.status not in CODIGOS_COMPLETADO:
        _clasificar_fallo(respuesta, que="iniciar la sesión de subida")

    session_url = respuesta.cabecera("Location")
    if not session_url.strip():
        raise RespuestaInvalida(
            "el proveedor aceptó abrir la sesión pero no devolvió su Location"
        )

    import hashlib

    return UploadSession(
        session_url=session_url,
        run_id=run_id,
        attempt=attempt,
        video_sha256=video_sha256,
        metadata_sha256=hashlib.sha256(cuerpo).hexdigest(),
        total_bytes=total_bytes,
        bytes_confirmed=0,
    )


# ---------------------------------------------------------------------------
# Progreso y fragmentos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EstadoRemoto:
    """Lo que el proveedor dice de una sesión.

    ``completada`` y ``video_id`` van juntos: el identificador solo existe
    cuando el proveedor dio la subida por terminada.
    """

    completada: bool
    bytes_confirmed: int
    video_id: str | None = None

    @property
    def confirmado(self) -> bool:
        return self.completada and bool((self.video_id or "").strip())


def consultar_progreso(
    sesion: UploadSession,
    *,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
) -> EstadoRemoto:
    """Pregunta al proveedor por dónde va la sesión.

    Es la consulta documentada del protocolo: ``PUT`` con
    ``Content-Range: bytes */<total>`` y cuerpo vacío. **Es el único mecanismo
    con el que este sistema puede saber si una subida se completó**, y necesita
    el ``session_url``. Sin él no hay pregunta que hacer.
    """
    respuesta = transporte.peticion(
        "PUT",
        sesion.session_url,
        headers={
            "Content-Length": "0",
            "Content-Range": f"bytes */{sesion.total_bytes}",
        },
        cuerpo=b"",
        timeout_s=timeout_s,
    )

    if respuesta.status in CODIGOS_COMPLETADO:
        datos = respuesta.json()
        video_id = str(datos.get("id") or "").strip()
        if not video_id:
            raise RespuestaInvalida(
                "el proveedor dio la sesión por completada sin devolver el id "
                "del vídeo"
            )
        return EstadoRemoto(
            completada=True, bytes_confirmed=sesion.total_bytes, video_id=video_id
        )

    if respuesta.status == CODIGO_INCOMPLETO:
        return EstadoRemoto(
            completada=False, bytes_confirmed=_bytes_confirmados(respuesta)
        )

    _clasificar_fallo(respuesta, que="consultar el progreso de la sesión")
    raise AssertionError("inalcanzable")  # pragma: no cover


def subir_fragmento(
    sesion: UploadSession,
    datos: bytes,
    *,
    offset: int,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
) -> tuple[UploadSession, EstadoRemoto]:
    """Envía un fragmento y devuelve la sesión actualizada y lo que contestó.

    Un fragmento que no es el último debe ser múltiplo de 256 KiB: el protocolo
    lo exige y saltárselo hace fallar la sesión entera a mitad.

    Si la respuesta se pierde, se levanta ``RespuestaPerdida`` indicando si lo
    enviado era el último fragmento. Quien la capture **no debe reintentar**:
    debe reconciliar.
    """
    if not datos:
        raise ErrorPermanente("no se envía un fragmento vacío")
    if offset != sesion.bytes_confirmed:
        raise ErrorPermanente(
            f"el fragmento empieza en {offset} y la sesión tiene "
            f"{sesion.bytes_confirmed} bytes confirmados: enviarlo dejaría un "
            f"hueco o repetiría contenido"
        )

    fin = offset + len(datos) - 1
    es_final = fin == sesion.total_bytes - 1
    if not es_final and len(datos) % MULTIPLO_FRAGMENTO != 0:
        raise ErrorPermanente(
            f"un fragmento intermedio debe ser múltiplo de {MULTIPLO_FRAGMENTO} "
            f"bytes y mide {len(datos)}"
        )
    if fin >= sesion.total_bytes:
        raise ErrorPermanente(
            f"el fragmento termina en {fin} y el archivo mide "
            f"{sesion.total_bytes} bytes"
        )

    try:
        respuesta = transporte.peticion(
            "PUT",
            sesion.session_url,
            headers={
                "Content-Length": str(len(datos)),
                "Content-Range": f"bytes {offset}-{fin}/{sesion.total_bytes}",
            },
            cuerpo=datos,
            timeout_s=timeout_s,
        )
    except RespuestaPerdida as exc:
        # El transporte ya sabe que mandó los bytes; se completa el contexto que
        # la reconciliación necesitará.
        raise RespuestaPerdida(
            str(exc),
            fragmento_final=es_final,
            bytes_enviados=sesion.bytes_confirmed,
        ) from exc
    except (TiempoAgotado, ErrorTransitorio) as exc:
        # Un corte después de enviar el cuerpo es indistinguible de uno anterior:
        # el proveedor pudo haberlo recibido. Se trata como respuesta perdida.
        raise RespuestaPerdida(
            f"se perdió la respuesta al fragmento: {exc}",
            fragmento_final=es_final,
            bytes_enviados=sesion.bytes_confirmed,
        ) from exc

    if respuesta.status in CODIGOS_COMPLETADO:
        datos_json = respuesta.json()
        video_id = str(datos_json.get("id") or "").strip()
        if not video_id:
            raise RespuestaInvalida(
                "el proveedor aceptó el último fragmento sin devolver el id"
            )
        estado = EstadoRemoto(
            completada=True,
            bytes_confirmed=sesion.total_bytes,
            video_id=video_id,
        )
        return _avanzar(sesion, sesion.total_bytes), estado

    if respuesta.status == CODIGO_INCOMPLETO:
        confirmados = _bytes_confirmados(respuesta)
        estado = EstadoRemoto(completada=False, bytes_confirmed=confirmados)
        return _avanzar(sesion, confirmados), estado

    _clasificar_fallo(respuesta, que="subir un fragmento")
    raise AssertionError("inalcanzable")  # pragma: no cover


def _avanzar(sesion: UploadSession, confirmados: int) -> UploadSession:
    """Sesión nueva con el progreso actualizado. No muta la anterior."""
    datos = sesion.model_dump()
    datos["session_url"] = sesion.session_url
    datos["bytes_confirmed"] = confirmados
    from datetime import datetime, timezone

    datos["updated_at"] = datetime.now(timezone.utc)
    return UploadSession.model_validate(datos)


# ---------------------------------------------------------------------------
# Verificación posterior
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecursoRemoto:
    """El vídeo tal como YouTube lo describe, ya subido."""

    video_id: str
    title: str
    description: str
    privacy_status: str
    upload_status: str = ""


def leer_video(
    token: TokenAcceso,
    video_id: str,
    *,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
) -> RecursoRemoto:
    """Lee el recurso remoto para poder comparar lo pedido con lo que hay.

    Usa ``videos.list`` con el identificador. **Si el alcance del token no
    cubriera esta lectura**, el fallo sale como ``AutorizacionInvalida``
    nombrando el remedio, igual que hace el AUTH CHECK de G7.1; no se disfraza
    de vídeo verificado. La suficiencia de ``youtube.upload`` para leer está
    documentada como incierta en el README y no se presume aquí.
    """
    parametros = urllib.parse.urlencode({"part": "snippet,status", "id": video_id})
    respuesta = transporte.peticion(
        "GET",
        f"{URL_VIDEOS}?{parametros}",
        headers={"Authorization": f"Bearer {token.access_token}"},
        timeout_s=timeout_s,
    )
    if respuesta.status not in CODIGOS_COMPLETADO:
        _clasificar_fallo(respuesta, que="leer el vídeo publicado")

    datos = respuesta.json()
    elementos = datos.get("items") or []
    if not isinstance(elementos, list) or not elementos:
        raise RespuestaInvalida(
            f"el proveedor no devolvió ningún vídeo con el id solicitado"
        )
    item = elementos[0]
    if not isinstance(item, dict):
        raise RespuestaInvalida("el elemento devuelto no es un objeto")
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    return RecursoRemoto(
        video_id=str(item.get("id") or "").strip(),
        title=str(snippet.get("title") or ""),
        description=str(snippet.get("description") or ""),
        privacy_status=str(status.get("privacyStatus") or ""),
        upload_status=str(status.get("uploadStatus") or ""),
    )


def url_publica(video_id: str) -> str:
    """Dónde queda el vídeo. Se construye aquí, no se recibe del proveedor."""
    return f"https://www.youtube.com/watch?v={video_id}"
