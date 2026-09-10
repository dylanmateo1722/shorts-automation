"""Orquestador del Proof of Concept de Gate 0.5.

Ejecuta el camino mínimo que valida las hipótesis aprobadas:

    guion ES → Edge TTS (+WordBoundary) → SRT propio → MPT → burn-in → QA → MP4

No hay Discovery, ni YouTube, ni publicación, ni proveedores adicionales. El
PoC no requiere ninguna credencial: Edge TTS no usa API key y el material de
video se genera localmente con FFmpeg.

Uso:
    python -m poc.run_poc --out-dir runs/poc
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from app.adapters.mpt import MPTAdapter
from app.config.settings import Settings
from app.contracts.models import EstadoRender, RenderJob
from app.core.workspace import Workspace

from . import burn_in, evidence, qa, segmenter, srt, tts_es

RAIZ = Path(__file__).resolve().parent.parent
RAIZ_MPT = RAIZ / "vendor" / "moneyprinterturbo"

# Frase de prueba exigida por Gate 0.5: puntuación, signos ¿? y ¡!, números,
# acentos y ñ, y longitud suficiente para producir varios cues.
GUION_PRUEBA = (
    "¿Sabías que el 73 % de los videos cortos se abandonan antes de los 3 segundos? "
    "¡Increíble! En este video te explico, paso a paso, cómo diseñar un gancho que retenga "
    "a tu audiencia. Analizaremos 5 técnicas comprobadas con ejemplos reales de canales en "
    "español. La señora Muñoz, experta en marketing digital, comparte además su método "
    "favorito. ¿Listo para empezar?"
)


def _log(mensaje: str) -> None:
    print(mensaje, flush=True)


def generar_materiales(destino: Path, duracion_s: float) -> list[Path]:
    """Crea el material visual local del PoC con FFmpeg.

    Se genera en vez de descargarse para que el PoC no dependa de ninguna API
    key ni de la red. En el sistema real este material vendría de stock o de
    producción propia (decisión D7: el MVP no incorpora metraje de la fuente).
    """
    destino.mkdir(parents=True, exist_ok=True)
    ffmpeg = burn_in.binario_ffmpeg()

    base = destino / "material_base.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"gradients=size=1280x720:rate=30:duration={max(6, int(duracion_s / 2))}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base)],
        check=True, timeout=600,
    )

    # Placa de título: elemento de transformación propio (kind=visual_overlay).
    # Se compone con libass y no con drawtext, porque el FFmpeg empaquetado por
    # imageio-ffmpeg no incluye ese filtro.
    placa = destino / "placa_titulo.mp4"
    ass_placa = destino / "placa_titulo.ass"
    ass_placa.write_text(srt.ass_titulo("GANCHOS QUE\nRETIENEN", 3.0), encoding="utf-8")
    ruta_filtro = str(ass_placa.resolve()).replace("\\", "/").replace(":", r"\:")
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x101820:size=1080x1920:rate=30:duration=3",
         "-vf", f"ass='{ruta_filtro}'",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(placa)],
        check=True, timeout=600,
    )
    return [placa, base]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Proof of Concept de Gate 0.5")
    parser.add_argument("--out-dir", default="runs/poc", help="directorio de la corrida")
    parser.add_argument("--voice", default=tts_es.VOZ_POR_DEFECTO, help="voz de Edge TTS")
    parser.add_argument(
        "--run-id",
        default=None,
        help="UUID de la corrida; se genera uno si se omite",
    )
    args = parser.parse_args(argv)

    # El run_id es también el --task-id de MPT (contrato D12), y MPT exige que
    # sea un UUID válido. Se valida aquí para fallar en el primer segundo y no
    # a mitad del render.
    if args.run_id:
        try:
            uuid.UUID(args.run_id)
        except ValueError:
            parser.error(
                f"--run-id debe ser un UUID válido (MPT lo exige como --task-id); "
                f"recibido {args.run_id!r}"
            )
    run_id = args.run_id or str(uuid.uuid4())
    salida = Path(args.out_dir).resolve()
    salida.mkdir(parents=True, exist_ok=True)
    ev = evidence.Evidencia(run_id=run_id)

    _log(f"== PoC Gate 0.5 | run_id={run_id} ==")

    try:
        import edge_tts as _e
        import imageio_ffmpeg as _i
        ev.capturar_entorno(
            RAIZ_MPT,
            {"edge-tts": getattr(_e, "__version__", "?"), "imageio-ffmpeg": _i.__version__},
        )
        _log(f"   MPT fijado en {ev.entorno.get('mpt_commit', '?')[:12]}")

        # --- Etapa 1: TTS con tiempos por palabra -------------------------
        with ev.etapa("tts") as reg:
            _log("-> TTS en español con Edge TTS")
            res_tts = tts_es.sintetizar(GUION_PRUEBA, salida / "narracion.mp3", args.voice)
            reg["eventos_wordboundary"] = sum(
                1 for e in res_tts.eventos if e["type"] == "WordBoundary"
            )
            reg["bytes_audio"] = res_tts.bytes_audio
            reg["fin_ultimo_evento_s"] = round(res_tts.fin_ultimo_evento_s, 3)
            reg["duracion_real_s"] = round(res_tts.duracion_real_s, 3)
            reg["cola_tras_ultimo_evento_s"] = round(
                res_tts.duracion_real_s - res_tts.fin_ultimo_evento_s, 3
            )
        (salida / "wordboundaries.json").write_text(
            json.dumps(res_tts.eventos, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ev.registrar_artefacto("narracion.mp3", res_tts.audio_path)
        ev.registrar_artefacto("wordboundaries.json", salida / "wordboundaries.json")

        # --- Etapa 2: segmentación y subtítulos ---------------------------
        with ev.etapa("subtitulos") as reg:
            _log("-> Segmentación en español y generación de SRT/ASS")
            cues, incidencias = segmenter.segmentar(GUION_PRUEBA, res_tts.eventos)
            ruta_srt = salida / "subtitulos.srt"
            ruta_ass = salida / "subtitulos.ass"
            ruta_srt.write_text(srt.a_srt(cues), encoding="utf-8")
            ruta_ass.write_text(srt.a_ass(cues), encoding="utf-8")
            validacion = srt.validar(cues, res_tts.duracion_real_s, guion=GUION_PRUEBA)
            reg["cues"] = len(cues)
            reg["incidencias"] = incidencias
            reg["validacion_ok"] = validacion.ok
        _log(f"   {len(cues)} cues | validación: {'PASS' if validacion.ok else 'FAIL'}")
        _log(validacion.resumen())
        ev.resultados["validacion_subtitulos"] = validacion.comprobaciones
        ev.registrar_artefacto("subtitulos.srt", ruta_srt)
        ev.registrar_artefacto("subtitulos.ass", ruta_ass)
        if not validacion.ok:
            raise RuntimeError("la validación de subtítulos no pasó")

        # --- Etapa 3: material visual -------------------------------------
        with ev.etapa("materiales") as reg:
            _log("-> Generando material visual local")
            materiales = generar_materiales(salida / "materiales", res_tts.duracion_real_s)
            reg["materiales"] = [m.name for m in materiales]

        # --- Etapa 4: render con MoneyPrinterTurbo ------------------------
        # El render pasa por el adaptador único de app/adapters: ningún otro
        # módulo del repositorio invoca MoneyPrinterTurbo directamente.
        with ev.etapa("render_mpt") as reg:
            _log("-> Render con MoneyPrinterTurbo (CLI, audio externo, sin subtítulos)")
            settings = Settings.desde_entorno()
            ws = Workspace.en_directorio(salida, uuid.UUID(run_id))
            job = RenderJob(
                run_id=ws.run_id,
                task_id=ws.run_id,
                script=GUION_PRUEBA,
                audio_path=ws.relativa(res_tts.audio_path),
                materials=[ws.relativa(m) for m in materiales],
            )
            render = MPTAdapter(settings, ws).render(job)
            if render.status is EstadoRender.fallo:
                raise RuntimeError(f"MPT exit={render.exit_code}: {render.error}")
            reg["exit_code"] = render.exit_code
            reg["audio_duration_s"] = render.duration_s
        video_mpt = ws.ruta(render.output_path)
        _log(f"   video de MPT: {render.output_path}")
        ev.registrar_artefacto("mpt_final.mp4", video_mpt)

        # --- Etapa 5: burn-in de nuestros subtítulos ----------------------
        with ev.etapa("burn_in") as reg:
            _log("-> Quemando subtítulos propios con FFmpeg")
            final = burn_in.quemar(
                entrada=video_mpt, ass=ruta_ass, salida=salida / "final.mp4"
            )
            reg["salida"] = final.name
        ev.registrar_artefacto("final.mp4", final)

        # --- Etapa 6: QA técnico ------------------------------------------
        elementos = [
            {"kind": "own_narration", "rationale": "narración en español generada por el sistema",
             "asset_ref": "narracion.mp3"},
            {"kind": "own_subtitles", "rationale": "SRT propio con segmentación española",
             "asset_ref": "subtitulos.srt"},
            {"kind": "visual_overlay", "rationale": "placa de título propia",
             "asset_ref": "materiales/placa_titulo.mp4"},
        ]
        with ev.etapa("qa") as reg:
            _log("-> QA técnico")
            informe = qa.evaluar(final, res_tts.duracion_real_s, elementos)
            reg["technical_qa_ok"] = informe.technical_qa_ok
        _log(informe.resumen())
        ev.resultados["technical_qa"] = informe.comprobaciones
        ev.resultados["technical_qa_ok"] = informe.technical_qa_ok
        ev.resultados["elementos_transformacion"] = elementos
        ev.resultados["editorial_legal_assessment"] = informe.editorial_legal_assessment

        # Registro de contenido sintético (D8): se registra, no se decide.
        ev.synthetic_media = {
            "proveedor_tts": res_tts.proveedor,
            "voz": res_tts.voz,
            "modelo_llm": None,
            "prompt_version": None,
            "elementos_generados_por_ia": ["own_narration"],
            "disclosure_decision": "UNDECIDED",
            "disclosure_basis": (
                "REQUIERE VERIFICACIÓN ACTUAL. La política de divulgación no se "
                "automatiza en Gate 0.5; solo se registra la información necesaria "
                "para decidirla más adelante."
            ),
        }

        ev.resultados["exito"] = informe.technical_qa_ok
        return 0 if informe.technical_qa_ok else 1

    except Exception as exc:
        _log(f"!! PoC detenido: {type(exc).__name__}: {exc}")
        ev.resultados["exito"] = False
        return 1
    finally:
        destino_ev = ev.volcar(RAIZ / "evidence" / f"evidence-{run_id}.json")
        _log(f"== evidencia: {destino_ev} ==")


if __name__ == "__main__":
    sys.exit(main())
