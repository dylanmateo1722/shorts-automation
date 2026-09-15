"""Utilidades compartidas por los tests de Gate 7.2.

El transporte falso es el que permite comprobar el protocolo entero sin red y,
sobre todo, escenificar lo que de otro modo no podría probarse: que una respuesta
se pierda justo después de que el proveedor aceptara los bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping
from uuid import UUID, uuid4

from app.contracts.models import (
    DivulgacionIA,
    EstadoDivulgacionIA,
    Privacidad,
    PublishMetadata,
)
from app.adapters.youtube.auth import TokenAcceso
from app.adapters.youtube.upload import RespuestaHTTP, RespuestaPerdida

SESSION_URL = "https://upload.example/resumable?upload_id=SESION-SECRETA-123"
ACCESS_TOKEN = "ya29.TOKEN-DE-PRUEBA-NO-REAL"
VIDEO_ID = "dQw4w9WgXcQ"


def token() -> TokenAcceso:
    return TokenAcceso(access_token=ACCESS_TOKEN, expires_in_s=3600)


def metadata(run_id: UUID | None = None, **extra) -> PublishMetadata:
    campos = dict(
        run_id=run_id or uuid4(),
        title="Título de prueba",
        description="Descripción de prueba",
        privacy_status=Privacidad.privado,
        made_for_kids=False,
        ai_disclosure=DivulgacionIA(
            status=EstadoDivulgacionIA.no_requerida,
            reason="pieza de prueba",
            decided_by="tests",
        ),
    )
    campos.update(extra)
    return PublishMetadata(**campos)


@dataclass
class Llamada:
    metodo: str
    url: str
    headers: dict
    cuerpo: bytes | None


@dataclass
class TransporteFalso:
    """Transporte programable. Cada respuesta se declara de antemano.

    ``guion`` es una lista de funciones que reciben la llamada y devuelven la
    respuesta, o levantan la excepción que toque. Se consumen en orden; agotado
    el guion, se repite la última.
    """

    guion: list[Callable[[Llamada], RespuestaHTTP]] = field(default_factory=list)
    llamadas: list[Llamada] = field(default_factory=list)

    def peticion(
        self,
        metodo: str,
        url: str,
        *,
        headers: Mapping[str, str],
        cuerpo: bytes | None = None,
        timeout_s: int = 30,
    ) -> RespuestaHTTP:
        llamada = Llamada(metodo, url, dict(headers), cuerpo)
        self.llamadas.append(llamada)
        indice = min(len(self.llamadas) - 1, len(self.guion) - 1)
        if indice < 0:
            raise AssertionError("transporte sin guion")
        return self.guion[indice](llamada)

    # --- consultas sobre lo ocurrido ---------------------------------------

    @property
    def sesiones_iniciadas(self) -> int:
        """Cuántas veces se abrió una sesión resumable nueva."""
        return sum(
            1
            for ll in self.llamadas
            if ll.metodo == "POST" and "uploadType=resumable" in ll.url
        )

    @property
    def fragmentos_enviados(self) -> int:
        return sum(
            1
            for ll in self.llamadas
            if ll.metodo == "PUT" and ll.cuerpo not in (None, b"")
        )


# --- respuestas de conveniencia --------------------------------------------


def sesion_abierta(url: str = SESSION_URL) -> Callable[[Llamada], RespuestaHTTP]:
    return lambda _: RespuestaHTTP(status=200, headers={"Location": url})


def incompleto(bytes_recibidos: int) -> Callable[[Llamada], RespuestaHTTP]:
    """``308`` con el ``Range`` que el protocolo devuelve."""
    if bytes_recibidos <= 0:
        return lambda _: RespuestaHTTP(status=308, headers={})
    return lambda _: RespuestaHTTP(
        status=308, headers={"Range": f"bytes=0-{bytes_recibidos - 1}"}
    )


def completado(video_id: str = VIDEO_ID) -> Callable[[Llamada], RespuestaHTTP]:
    import json

    cuerpo = json.dumps({"id": video_id}).encode("utf-8")
    return lambda _: RespuestaHTTP(status=200, body=cuerpo)


def video_remoto(
    *,
    video_id: str = VIDEO_ID,
    title: str = "Título de prueba",
    description: str = "Descripción de prueba",
    privacy: str = "private",
    upload_status: str = "uploaded",
) -> Callable[[Llamada], RespuestaHTTP]:
    import json

    cuerpo = json.dumps(
        {
            "items": [
                {
                    "id": video_id,
                    "snippet": {"title": title, "description": description},
                    "status": {
                        "privacyStatus": privacy,
                        "uploadStatus": upload_status,
                    },
                }
            ]
        }
    ).encode("utf-8")
    return lambda _: RespuestaHTTP(status=200, body=cuerpo)


def error(status: int, razon: str = "") -> Callable[[Llamada], RespuestaHTTP]:
    import json

    cuerpo = b""
    if razon:
        cuerpo = json.dumps(
            {"error": {"errors": [{"reason": razon}]}}
        ).encode("utf-8")
    return lambda _: RespuestaHTTP(status=status, body=cuerpo)


def respuesta_perdida(*, final: bool = False, bytes_enviados: int = 0):
    """El caso que define el Gate: se mandaron los bytes y no se supo nada más."""

    def _lanzar(_: Llamada) -> RespuestaHTTP:
        raise RespuestaPerdida(
            "la conexión murió tras enviar el cuerpo",
            fragmento_final=final,
            bytes_enviados=bytes_enviados,
        )

    return _lanzar


def sin_dormir(_: float) -> None:
    """Sustituye a ``time.sleep`` para que los tests no esperen de verdad."""


def jitter_fijo() -> float:
    return 0.0
