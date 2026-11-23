#!/usr/bin/env python3
"""Carga reclamos de demostración por la API, sin insertar una sola fila a mano.

El prototipo desplegado no se puede recorrer con la base vacía: la bandeja del
revisor, el monitor del operador y las pantallas del asegurado no tienen qué
mostrar. Lo que falta son datos, y hay dos maneras de conseguirlos.

Esta es la que no fabrica nada. Cada caso entra por `POST /reclamos` con un
parche real del conjunto de prueba de CarDD y lo procesa el sistema completo:
extractor ONNX, cabeza, prueba EZKL, transiciones. Lo que queda en la base es lo
que el sistema produjo, no lo que este script quiso que produjera.

De ahí sale la única cautela que importa leer. El monto es palanca determinista,
porque el límite de RF-34 lo evalúa el sistema sobre un número que elegimos; la
confianza no lo es, porque la produce el clasificador. Así que los casos declaran
qué estado ESPERAN y el script informa el que SALIÓ, sin corregir la diferencia.

Uso:
    scripts/seed-demo-claims.py --parches ../PFI-MachineLearning/fase1/patches/test
    scripts/seed-demo-claims.py --api https://… --parches … --salida demo.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import httpx

# Los cuatro casos y qué pantalla puebla cada uno. La clase es la carpeta del
# conjunto de prueba y no una etiqueta que el sistema vaya a aceptar sin más: el
# clasificador dirá lo suyo, y si difiere de la carpeta eso también es un dato.
CASOS = (
    {
        "clase": "dent",
        "monto": "1850000.00",
        "espera": "RequiereRevision",
        "por": "monto sobre el límite de RF-34",
        "puebla": "bandeja del revisor",
    },
    {
        "clase": "crack",
        "monto": "240000.00",
        "espera": "RequiereRevision",
        "por": "confianza bajo el umbral de derivación, si el clasificador duda",
        "puebla": "bandeja del revisor",
    },
    {
        "clase": "glass_shatter",
        "monto": "185000.00",
        "espera": "Aprobable",
        "por": "confianza sobre el umbral de derivación y monto dentro del límite",
        "puebla": "liquidación del operador",
    },
    {
        "clase": "tire_flat",
        "monto": "95000.00",
        "espera": "Aprobado",
        "por": "confianza sobre el umbral de liquidación automática (RF-50)",
        "puebla": "línea de tiempo y explicación del asegurado",
    },
)

ESPERA_MAXIMA_S = 180
INTERVALO_S = 3

# Los dos casos que se ejercen después de sembrar, para que el recorrido no
# quede entero en estados intermedios. No son datos puestos a mano: son CU-06 y
# CU-05 ejecutados por la API con las sesiones internas, con su transición
# asentada y su justificación registrada como cualquier otra.
#
# Se ejerce UNO de cada clase y no todos: si se cerraran los cuatro, la bandeja
# del revisor y la lista del operador volverían a quedar vacías, que es el
# problema que la siembra viene a resolver.
JUSTIFICACION = (
    "Las fotografías muestran un daño compatible con lo declarado y el monto "
    "está dentro de lo previsto para la pieza."
)


def elegir_parche(raiz: pathlib.Path, clase: str, indice: int) -> pathlib.Path:
    carpeta = raiz / clase
    if not carpeta.is_dir():
        raise SystemExit(f"no existe la carpeta de parches {carpeta}")
    archivos = sorted(p for p in carpeta.iterdir() if p.suffix.lower() in {".jpg", ".png"})
    if not archivos:
        raise SystemExit(f"{carpeta} no tiene parches")
    # Determinista: la misma corrida elige los mismos parches, de modo que
    # repetir la siembra sobre una base limpia da el mismo conjunto.
    return archivos[indice % len(archivos)]


def registrar(cliente: httpx.Client, parche: pathlib.Path, monto: str) -> dict:
    tipo = "image/png" if parche.suffix.lower() == ".png" else "image/jpeg"
    with parche.open("rb") as archivo:
        respuesta = cliente.post(
            "/reclamos",
            files=[("photos", (parche.name, archivo.read(), tipo))],
            data={"declaredAmount": monto},
        )
    if respuesta.status_code != 201:
        raise SystemExit(f"el registro falló ({respuesta.status_code}): {respuesta.text}")
    return respuesta.json()


def esperar_resolucion(cliente: httpx.Client, numero: str, token: str) -> dict:
    """Espera a que el reclamo salga de Registrado.

    Queda en Registrado mientras la prueba está en la cola, así que un reclamo
    recién cargado no tiene todavía el estado que va a tener. Si la espera se
    agota se informa el estado alcanzado en vez de suponer el siguiente.

    Un 502 en el medio no es el final del intento: el servicio de producción se
    suspende, se reinicia al desplegar y tarda en volver, y durante ese rato la
    pasarela responde por él. Cortar ahí abandonaría un reclamo ya creado, que es
    lo que pasó la primera vez que esto corrió contra Railway.
    """
    limite = time.monotonic() + ESPERA_MAXIMA_S
    ultimo: dict = {}
    while time.monotonic() < limite:
        try:
            respuesta = cliente.get(
                f"/reclamos/{numero}", headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as error:
            print(f"    (reintentando: {type(error).__name__})", flush=True)
            time.sleep(INTERVALO_S)
            continue
        if respuesta.status_code >= 500:
            print(f"    (reintentando: {respuesta.status_code})", flush=True)
            time.sleep(INTERVALO_S)
            continue
        respuesta.raise_for_status()
        ultimo = respuesta.json()
        etiqueta = ultimo.get("status", {}).get("label", "")
        if "análisis" not in etiqueta.lower():
            return ultimo
        time.sleep(INTERVALO_S)
    return ultimo


def estado_interno(cliente: httpx.Client, numero: str) -> str | None:
    """El identificador técnico, que el perfil del reclamante no recibe (RNF-35).

    Sale de la traza pública, que sí lo expone y no pide acreditar titularidad.
    """
    respuesta = cliente.get(f"/publico/reclamos/{numero}/traza")
    if respuesta.status_code != 200:
        return None
    return respuesta.json().get("statusId")


def ejercer(cliente: httpx.Client, sembrados: list[dict], *, revisor: str, operador: str) -> None:
    """Cierra un derivado y un aprobable, por las rutas que usan las pantallas.

    Deja a propósito uno de cada clase sin cerrar: la bandeja y la lista de
    liquidación tienen que quedar con algo adentro.
    """
    derivados = [f for f in sembrados if f["statusId"] == "RequiereRevision"]
    aprobables = [f for f in sembrados if f["statusId"] == "Aprobable"]

    if len(derivados) > 1:
        fila = derivados[-1]
        respuesta = cliente.post(
            f"/reclamos/{fila['number']}/resolucion",
            headers={"Authorization": f"Bearer {revisor}"},
            json={"favorable": True, "justification": JUSTIFICACION},
        )
        if respuesta.status_code == 200:
            fila["statusId"] = respuesta.json()["statusId"]
            fila["closedBy"] = "resolución favorable del revisor (CU-05)"
            print(f"  {fila['number']}  resuelto por el revisor -> {fila['statusId']}")
        else:
            print(f"  {fila['number']}  la resolución falló ({respuesta.status_code})")

    if len(aprobables) > 1:
        fila = aprobables[0]
        respuesta = cliente.post(
            f"/reclamos/{fila['number']}/liquidacion",
            headers={"Authorization": f"Bearer {operador}"},
            json={},
        )
        if respuesta.status_code == 200:
            fila["statusId"] = respuesta.json()["statusId"]
            fila["closedBy"] = "liquidación registrada por el operador (CU-06)"
            print(f"  {fila['number']}  liquidado por el operador -> {fila['statusId']}")
        else:
            print(f"  {fila['number']}  la liquidación falló ({respuesta.status_code})")


def guardar(salida: pathlib.Path, sembrados: list[dict]) -> None:
    """Escribe la salida después de CADA reclamo y no al final.

    El token en claro existe una sola vez, en la respuesta del registro (RF-47).
    Guardarlo al final significa que cualquier corte deja reclamos creados a los
    que ya nadie puede entrar, y eso ocurrió: la primera corrida contra producción
    se cortó con un 502 y perdió el acceso a un reclamo que había creado.
    """
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(
        json.dumps(sembrados, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="URL base de la API")
    parser.add_argument(
        "--parches",
        required=True,
        type=pathlib.Path,
        help="carpeta con las subcarpetas de clase del conjunto de prueba de CarDD",
    )
    parser.add_argument(
        "--salida",
        type=pathlib.Path,
        default=pathlib.Path("datos/reclamos-demo.json"),
        help="dónde escribir los números y tokens que la siembra produjo",
    )
    parser.add_argument(
        "--forzar",
        action="store_true",
        help="sembrar de nuevo aunque el archivo de salida ya exista",
    )
    parser.add_argument(
        "--revisor",
        default="revisor-rev-04",
        help="token de la sesión de revisor (SESIONES_INTERNAS del backend)",
    )
    parser.add_argument(
        "--operador",
        default="operador-op-02",
        help="token de la sesión de operador (SESIONES_INTERNAS del backend)",
    )
    parser.add_argument(
        "--sin-ejercer",
        action="store_true",
        help="no cerrar ningún caso: deja los cuatro como los dejó el sistema",
    )
    args = parser.parse_args()

    if args.salida.exists() and not args.forzar:
        print(f"{args.salida} ya existe: la siembra ya corrió. Con --forzar se repite.")
        return 0

    sembrados = []
    with httpx.Client(base_url=args.api.rstrip("/"), timeout=120.0) as cliente:
        for indice, caso in enumerate(CASOS):
            parche = elegir_parche(args.parches, caso["clase"], indice)
            print(f"→ {caso['clase']}/{parche.name}  monto {caso['monto']}", flush=True)
            constancia = registrar(cliente, parche, caso["monto"])
            numero, token = constancia["number"], constancia["accessToken"]
            reclamo = esperar_resolucion(cliente, numero, token)
            interno = estado_interno(cliente, numero)
            fila = {
                "number": numero,
                "accessToken": token,
                "statusId": interno,
                # El estado con el que el sistema lo dejó, antes de que se
                # ejerza nada sobre él. Es contra este y no contra el final que
                # se mide si el caso cayó donde se esperaba.
                "statusAfterProcessing": interno,
                "statusLabel": reclamo.get("status", {}).get("label"),
                "expected": caso["espera"],
                "patch": f"{caso['clase']}/{parche.name}",
                "declaredAmount": caso["monto"],
                "populates": caso["puebla"],
            }
            sembrados.append(fila)
            guardar(args.salida, sembrados)
            coincide = "=" if interno == caso["espera"] else "≠"
            print(
                f"  {numero}  {interno} {coincide} esperado {caso['espera']}"
                f"  ({caso['por']})",
                flush=True,
            )

        if not args.sin_ejercer:
            print("\nCerrando un caso de cada clase por sus rutas:", flush=True)
            ejercer(cliente, sembrados, revisor=args.revisor, operador=args.operador)

    guardar(args.salida, sembrados)
    print(f"\n{len(sembrados)} reclamos sembrados; números y tokens en {args.salida}")

    distintos = [f for f in sembrados if f["statusAfterProcessing"] != f["expected"]]
    if distintos:
        print(
            "\nAviso: "
            f"{len(distintos)} de {len(sembrados)} no cayeron en el estado esperado. "
            "La confianza la produce el clasificador y no la elige este script; "
            "el dato queda como salió."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
