"""RNF-15 — curva del umbral de confianza contra la tasa de derivación.

El sistema resuelve solo los reclamos cuya confianza alcanza un umbral y deriva
el resto a una persona. Ese umbral gobierna la utilidad operativa: bajarlo
resuelve más casos sin intervención y deja escapar más errores; subirlo protege
la resolución automática y descarga menos trabajo humano. El número no se elige
por criterio, se elige mirando la curva.

Este script no entrena nada. Toma el checkpoint de la configuración citable
—la del extractor ajustado, adoptada en adr-011— y mide sobre una partición que
el modelo no vio:

  * **tasa de derivación**: qué fracción de los casos queda por debajo del
    umbral y va a revisión humana;
  * **cobertura**: la fracción complementaria, que el sistema resuelve solo;
  * **precisión de lo automático**: qué tan seguido acierta el sistema *entre
    los casos que resolvió sin intervención*. Es la métrica que importa para
    fijar el umbral, porque un error acá se convierte en un reclamo mal
    liquidado;
  * **errores que escapan**: los mismos casos en número absoluto, que es como
    se leen cuando hay que decidir si el riesgo es tolerable.

La confianza es el máximo de la softmax sobre las seis clases, calculada fuera
del circuito (RF-16), que es exactamente lo que el backend compara contra el
umbral configurado.

Ejecutar (con `fase1/patches/` y el checkpoint en su lugar):

    uv run python -m fase1.sweep_threshold --split test
    uv run python -m fase1.sweep_threshold --split test --out fase1/rnf15_umbral.md
"""

from __future__ import annotations

import argparse
import os
import time

import torch
from torch import nn
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder

from fase1.finetune_afuera import build_finetunable_model, build_transforms

# Rejilla de umbrales. Arranca en 0,50 porque por debajo del azar informado la
# comparación deja de tener sentido con seis clases, y llega a 0,99 porque el
# extremo alto es el que muestra cuánto trabajo humano cuesta la prudencia.
UMBRALES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.99]


def collect_confidences(
    checkpoint_path: str, data_dir: str, split: str, batch_size: int, device: str
) -> tuple[torch.Tensor, torch.Tensor, list[str]]:
    """Devuelve (confianza, acierto, clases) para cada muestra de la partición."""
    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    clases = checkpoint["classes"]

    modelo, _ = build_finetunable_model(len(clases), checkpoint["config"]["unfreeze"])
    modelo.load_state_dict(checkpoint["model_state_dict"])
    modelo.to(device).eval()

    _, eval_tf = build_transforms()
    dataset = ImageFolder(os.path.join(data_dir, split), transform=eval_tf)
    if dataset.classes != clases:
        raise SystemExit(
            f"las clases de {split} ({dataset.classes}) no coinciden con las del "
            f"checkpoint ({clases}): el barrido mediría otra cosa"
        )
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)

    confianzas: list[torch.Tensor] = []
    aciertos: list[torch.Tensor] = []
    with torch.no_grad():
        for x, y in loader:
            logits = modelo(x.to(device))
            # Softmax fuera del circuito (RF-16): el circuito emite los logits
            # crudos y esta es la misma cuenta que hace el backend.
            probabilidades = torch.softmax(logits, dim=1)
            confianza, predicha = probabilidades.max(dim=1)
            confianzas.append(confianza.cpu())
            aciertos.append((predicha.cpu() == y).float())

    return torch.cat(confianzas), torch.cat(aciertos), clases


def sweep(confianza: torch.Tensor, acierto: torch.Tensor) -> list[dict]:
    total = confianza.numel()
    filas = []
    for umbral in UMBRALES:
        automatico = confianza >= umbral
        resueltos = int(automatico.sum())
        derivados = total - resueltos
        correctos = int(acierto[automatico].sum()) if resueltos else 0
        filas.append(
            {
                "umbral": umbral,
                "derivados": derivados,
                "tasa_derivacion": derivados / total,
                "cobertura": resueltos / total,
                # Sin ningún caso automático la precisión no está definida; se
                # informa como nula en lugar de como 1,0, que sería afirmar un
                # acierto perfecto sobre el conjunto vacío.
                "precision_automatica": (correctos / resueltos) if resueltos else None,
                "errores_que_escapan": resueltos - correctos,
            }
        )
    return filas


def render(filas: list[dict], contexto: dict) -> str:
    lineas = [
        "# RNF-15 — umbral de confianza contra tasa de derivación",
        "",
        f"Configuración: {contexto['checkpoint']} | partición `{contexto['split']}` "
        f"({contexto['muestras']} muestras, {len(contexto['clases'])} clases)",
        f"Dispositivo: {contexto['device']} | corrida: {contexto['fecha']} | "
        f"{contexto['segundos']:.1f} s",
        "",
        "La precisión automática es la que se mide **solo sobre los casos que el "
        "sistema resolvió sin intervención**; los errores que escapan son esos "
        "mismos casos en número absoluto.",
        "",
        "| umbral | tasa de derivación | cobertura | precisión automática | errores que escapan |",
        "|---|---|---|---|---|",
    ]
    for fila in filas:
        precision = (
            f"{fila['precision_automatica']:.3f}"
            if fila["precision_automatica"] is not None
            else "n/d"
        )
        lineas.append(
            f"| {fila['umbral']:.2f} | {fila['tasa_derivacion']:.3f} "
            f"({fila['derivados']}) | {fila['cobertura']:.3f} | {precision} | "
            f"{fila['errores_que_escapan']} |"
        )
    return "\n".join(lineas) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--checkpoint", default="fase1/checkpoints/finetune_mnv3_all.pt")
    p.add_argument("--data", default="fase1/patches")
    p.add_argument(
        "--split",
        default="test",
        choices=["test", "val"],
        help="Partición sobre la que se mide. Por defecto test, que el modelo no vio.",
    )
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    p.add_argument("--out", default="fase1/rnf15_umbral.md")
    args = p.parse_args()

    if not os.path.exists(args.checkpoint):
        raise SystemExit(
            f"no está el checkpoint {args.checkpoint}. Es el de la configuración "
            "citable (extractor ajustado, adr-011); se produce con "
            "`fase1.finetune_afuera --unfreeze all --eval-test`."
        )

    inicio = time.time()
    confianza, acierto, clases = collect_confidences(
        args.checkpoint, args.data, args.split, args.batch_size, args.device
    )
    filas = sweep(confianza, acierto)
    transcurrido = time.time() - inicio

    contexto = {
        "checkpoint": args.checkpoint,
        "split": args.split,
        "muestras": confianza.numel(),
        "clases": clases,
        "device": args.device,
        "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
        "segundos": transcurrido,
    }
    tabla = render(filas, contexto)
    print(tabla)
    print(f"accuracy global (sin umbral): {acierto.mean():.4f}")

    with open(args.out, "w", encoding="utf-8") as archivo:
        archivo.write(tabla)
    print(f"escrito en {args.out}")


if __name__ == "__main__":
    main()
