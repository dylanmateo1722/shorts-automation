"""Política técnica de procedencia: qué habilita el render y qué no.

Qué es esto y qué no es
-----------------------

Esta política decide si el pipeline **puede utilizar técnicamente** un recurso.
No es un dictamen jurídico. ``render_allowed`` significa:

    la procedencia registrada y la evidencia disponible satisfacen la política
    técnica de este sistema

y **no** significa ausencia de reclamaciones de copyright, compatibilidad con
Content ID, derecho a monetizar ni asesoramiento legal. Ese juicio es humano,
se declara aparte y su estado por defecto es ``NOT_ASSESSED``.

La versión vive aquí y solo aquí
--------------------------------

Cada decisión registra ``policy_version``. Cambiar las reglas exige subir la
versión, y las decisiones anteriores siguen diciendo bajo qué política se
tomaron. Por eso la constante está en un único sitio: repetirla haría que dos
copias divergieran y que un artefacto histórico mintiera sobre su origen.

Lo que esta política NO hace
----------------------------

No evalúa uso legítimo. ``fair_use_claim`` es una afirmación de alguien, no una
conclusión del sistema, y aquí siempre acaba en ``needs_review``. Tampoco existe
ninguna regla que convierta una URL en permiso: conocer la dirección de un vídeo
ajeno no autoriza a renderizarlo.

Dos caminos hasta una decisión
------------------------------

``reference_only`` no lo deduce la política: lo **declara** quien aporta el
recurso. Decir «esto lo consulté como referencia» es una afirmación sobre el uso
que se va a hacer, no sobre la licencia, y por eso no hace falta evidencia de
licencia para registrarlo: el recurso queda fuera del render por su clase.

``render_allowed``, ``needs_review`` y ``blocked`` sí salen de evaluar una base
contra su evidencia. La diferencia importa: marcar una referencia como
``needs_review`` insinuaría que alguien debería revisarla para desbloquearla,
cuando lo correcto es que nunca entre al render.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import (
    BASES_NUNCA_RENDERIZABLES,
    BaseLicencia,
    ClaseFuente,
    LicenseDecision,
    SourceAsset,
    TextoNoVacio,
    TipoEvidencia,
)

#: Identificador de esta política. Único punto donde se declara.
VERSION_POLITICA = "provenance_policy_v1"

#: Qué tipo de evidencia respalda cada base. Una evidencia de otro tipo no
#: invalida nada por sí sola, pero no cuenta como respaldo de esa base: apuntar
#: a la página de una licencia no demuestra que exista un permiso escrito.
#:
#: ``TipoEvidencia.otra`` no respalda ninguna base. Una evidencia sin clasificar
#: puede acompañar a un recurso y quedar registrada, pero no habilita por sí sola
#: una decisión: si no se sabe qué es, no se sabe qué demuestra.
EVIDENCIA_REQUERIDA: dict[BaseLicencia, frozenset[TipoEvidencia]] = {
    BaseLicencia.propia: frozenset({TipoEvidencia.registro_propiedad}),
    BaseLicencia.licenciada: frozenset(
        {TipoEvidencia.pagina_licencia, TipoEvidencia.registro_licencia_cc}
    ),
    BaseLicencia.cc_by: frozenset(
        {TipoEvidencia.registro_licencia_cc, TipoEvidencia.pagina_licencia}
    ),
    BaseLicencia.dominio_publico: frozenset({TipoEvidencia.registro_dominio_publico}),
    BaseLicencia.permiso: frozenset({TipoEvidencia.documento_permiso}),
}


@dataclass(frozen=True)
class Veredicto:
    """Lo que la política concluye sobre una fuente, y por qué."""

    decision: ClaseFuente
    reason: str
    evidence_ids: tuple[str, ...] = ()

    @property
    def permite_render(self) -> bool:
        return self.decision is ClaseFuente.render_permitido


def _evidencias_validas(fuente: SourceAsset, basis: BaseLicencia) -> list[str]:
    """Identificadores de la evidencia que respalda esa base concreta."""
    aceptadas = EVIDENCIA_REQUERIDA.get(basis, frozenset())
    return [e.evidence_id for e in fuente.evidence if e.kind in aceptadas]


def evaluar(fuente: SourceAsset, basis: BaseLicencia) -> Veredicto:
    """Decide qué se puede hacer con una fuente, según esta política.

    Nunca devuelve ``blocked`` por sí sola: bloquear es una decisión que alguien
    toma —una prohibición explícita, una licencia incompatible— y la política no
    la infiere de la ausencia de datos. Lo que la ausencia produce es
    ``needs_review``, que es lo que de verdad ocurre: falta información.
    """
    # 1. Bases que no habilitan el render por sí solas, pase lo que pase.
    if basis in BASES_NUNCA_RENDERIZABLES:
        if basis is BaseLicencia.uso_legitimo_alegado:
            return Veredicto(
                ClaseFuente.revision_pendiente,
                "uso legítimo alegado: es una afirmación por resolver, no una "
                "conclusión del sistema. Este pipeline no evalúa uso legítimo",
            )
        return Veredicto(
            ClaseFuente.revision_pendiente,
            "procedencia desconocida: sin base declarada no hay nada que verificar",
        )

    # 2. Sin evidencia del tipo que la base exige, no se decide: se revisa.
    respaldo = _evidencias_validas(fuente, basis)
    if not respaldo:
        tipos = sorted(t.value for t in EVIDENCIA_REQUERIDA.get(basis, frozenset()))
        return Veredicto(
            ClaseFuente.revision_pendiente,
            f"base {basis.value!r} sin evidencia de tipo {tipos}; "
            f"declarar la base no la respalda",
        )

    # 3. Una atribución exigida sin texto con el que atribuir deja el uso en el
    #    aire: no se inventa el crédito, se pide.
    if fuente.attribution_required and not (fuente.attribution_text or "").strip():
        return Veredicto(
            ClaseFuente.revision_pendiente,
            "la licencia exige atribución y no consta el texto con el que atribuir",
            tuple(respaldo),
        )

    # 4. Reglas propias de cada base.
    if basis is BaseLicencia.cc_by and not (fuente.license_id or "").strip():
        return Veredicto(
            ClaseFuente.revision_pendiente,
            "Creative Commons sin licencia concreta: la familia no dice si el uso "
            "previsto está permitido, y hay licencias CC que lo prohíben",
            tuple(respaldo),
        )
    if basis is BaseLicencia.licenciada and fuente.commercial_use_allowed is None:
        return Veredicto(
            ClaseFuente.revision_pendiente,
            "licencia sin constancia de si cubre el uso previsto; 'licensed' no "
            "implica uso comercial",
            tuple(respaldo),
        )
    if fuente.commercial_use_allowed is False:
        return Veredicto(
            ClaseFuente.bloqueado,
            "la licencia registrada excluye el uso previsto",
            tuple(respaldo),
        )

    return Veredicto(
        ClaseFuente.render_permitido,
        f"base {basis.value!r} respaldada por {len(respaldo)} evidencia(s) "
        f"según {VERSION_POLITICA}",
        tuple(respaldo),
    )


def decidir(fuente: SourceAsset, basis: BaseLicencia, *, run_id) -> LicenseDecision:
    """Aplica la política y devuelve la decisión ya firmada con su versión."""
    veredicto = evaluar(fuente, basis)
    return LicenseDecision(
        run_id=run_id,
        asset_id=fuente.asset_id,
        decision=veredicto.decision,
        basis=basis,
        evidence_ids=list(veredicto.evidence_ids),
        reason=veredicto.reason,
        policy_version=VERSION_POLITICA,
    )


def declarar_referencia(fuente: SourceAsset, *, run_id) -> LicenseDecision:
    """Registra un recurso como referencia editorial, sin evaluar su licencia.

    Es el único camino hasta ``reference_only``, y es una declaración, no un
    veredicto: ``decided_by`` lo dice. La base queda ``unknown`` porque es la
    verdad —nadie ha comprobado bajo qué licencia está— y no hace falta
    comprobarlo para leer algo y documentarse.

    Lo que este registro garantiza es lo otro: que el recurso no puede entrar al
    render, porque su clase no es ``render_allowed`` y el ledger se niega a
    validar un render que lo incluya. Pasar de aquí a ``render_allowed`` exige
    una decisión nueva, con base y evidencia; nada en el sistema lo hace solo.
    """
    return LicenseDecision(
        run_id=run_id,
        asset_id=fuente.asset_id,
        decision=ClaseFuente.solo_referencia,
        basis=BaseLicencia.desconocida,
        reason=(
            "declarado como referencia editorial: informa el análisis y el guion, "
            "y no puede utilizarse como material del render"
        ),
        policy_version=VERSION_POLITICA,
        decided_by="declaration",
    )


# ---------------------------------------------------------------------------
# Declaración manual de fuentes externas
# ---------------------------------------------------------------------------


class DeclaracionFuente(BaseModel):
    """Una fuente externa declarada a mano, con la base que se alega para ella.

    ``basis`` va fuera de ``asset`` a propósito: no es un atributo del recurso
    sino lo que alguien alega sobre él, y es lo que la política evalúa. Los
    campos del recurso no se repiten aquí —se validan construyendo el
    ``SourceAsset``— para que la lista de campos viva en un solo sitio.
    """

    model_config = ConfigDict(extra="forbid")

    basis: BaseLicencia
    asset: dict[str, Any]

    def fuente(self, run_id) -> SourceAsset:
        prohibidos = {"run_id", "schema_version", "created_at"} & set(self.asset)
        if prohibidos:
            raise ValueError(
                f"una declaración no puede fijar {sorted(prohibidos)}: esos campos "
                f"los pone la corrida, no el archivo de entrada"
            )
        return SourceAsset(run_id=run_id, **self.asset)


class ArchivoDeclaraciones(BaseModel):
    """Contenido del archivo JSON con el que se declaran fuentes externas.

    Existe porque G6 no implementa Discovery: para ejercitar bases distintas de
    ``own`` hace falta que alguien declare la fuente y su evidencia a mano. No
    descarga nada ni consulta nada.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[DeclaracionFuente] = Field(default_factory=list)
    note: TextoNoVacio | None = None

    @classmethod
    def leer(cls, ruta: Path) -> "ArchivoDeclaraciones":
        return cls.model_validate(json.loads(ruta.read_text(encoding="utf-8")))
