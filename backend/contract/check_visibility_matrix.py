"""Comprueba los esquemas del contrato contra la matriz de visibilidad.

La matriz de visibilidad define dieciocho elementos de información por cuatro
actores, con celdas que le niegan un dato a un perfil.
RF-46 la convierte en requerimiento y adr-006 decide que se aplique por campo en
la serialización, no por pantalla. Este script es lo que vuelve esa decisión
comprobable de forma mecánica en lugar de por lectura.

Falla si una celda no coincide, y con eso alcanza para usarlo en integración
continua.

Uso:
    python contract/check_visibility_matrix.py
"""

import sys
from pathlib import Path

import yaml

CONTRACT = Path(__file__).with_name("openapi.yaml")

PROFILES = ("reclamante", "revisor", "operador", "auditor")

# Esquemas de respuesta que recibe cada perfil, además de Configuration, que es
# un recurso propio y declara sus perfiles en las etiquetas de su operación.
SCHEMAS_BY_PROFILE = {
    "reclamante": ("ClaimantClaim", "ClaimExplanation", "RegistrationReceipt"),
    "revisor": ("ReviewerClaim", "ReviewInbox"),
    "operador": ("OperatorClaim", "QueueState"),
    "auditor": ("PublicTrace",),
}

# Las dieciocho filas de la matriz. El segundo elemento es la marca del campo que
# materializa la fila; el tercero, la celda esperada por perfil.
MATRIX = [
    ("Identificador del reclamo", "number", (1, 1, 1, 1)),
    ("Identificador de contenido de la evidencia", "evidenceId", (1, 1, 1, 1)),
    ("Imagen de la evidencia", "files[].url", (0, 1, 1, 0)),
    ("Hash de la prueba", "proofHash", (1, 1, 1, 1)),
    ("Hash de transaccion", "transactionHash", (1, 1, 1, 1)),
    ("Vector de logits", "logits", (0, 1, 0, 0)),
    ("Vector de features publicado", "featureVector", (0, 0, 0, 1)),
    ("Clase predicha", "predictedClass", (1, 1, 1, 0)),
    ("Confianza", "confidence", (1, 1, 1, 0)),
    ("Umbrales de confianza vigentes", "derivationThreshold", (1, 1, 1, 0)),
    ("Modo de politica vigente", "policyMode", (1, 1, 1, 0)),
    ("Identificador tecnico de estado", "statusId", (0, 1, 1, 1)),
    ("Causa de la transicion", "cause", (1, 1, 1, 1)),
    ("Monto declarado", "declaredAmount", (1, 1, 1, 0)),
    ("Descripción del daño", "damageDescription", (1, 1, 1, 0)),
    # Al reclamante le llega solo el rol y al personal interno la identidad; por
    # eso la marca es el campo `reviewer` y no `role`, que los dos tienen.
    ("Identidad del revisor", "reviewer", (0, 1, 1, 0)),
    # Quien transcribe es visible al personal interno, nunca al reclamante ni al
    # auditor de la traza pública.
    ("Identidad de quien carga", "registeredBy", (0, 1, 1, 0)),
    ("Operación pendiente o incierta del oracle y reconciliación", "oracleBlocked", (0, 0, 1, 0)),
]


def flatten(schemas: dict, node, prefix: str = "") -> set[str]:
    """Aplana las propiedades de un esquema resolviendo $ref, allOf y arreglos."""
    if isinstance(node, str):
        node = schemas[node]
    fields: set[str] = set()
    for part in node.get("allOf", []):
        fields |= flatten(schemas, part["$ref"].split("/")[-1] if "$ref" in part else part, prefix)
    for name, value in (node.get("properties") or {}).items():
        fields.add(prefix + name)
        child = schemas[value["$ref"].split("/")[-1]] if "$ref" in value else value
        if not isinstance(child, dict):
            continue
        if "properties" in child or "allOf" in child:
            fields |= flatten(schemas, child, f"{prefix}{name}.")
        items = child.get("items")
        if child.get("type") == "array" and isinstance(items, dict):
            if "$ref" in items:
                items = schemas[items["$ref"].split("/")[-1]]
            if "properties" in items or "allOf" in items:
                fields |= flatten(schemas, items, f"{prefix}{name}[].")
    return fields


def main() -> int:
    doc = yaml.safe_load(CONTRACT.read_text())
    schemas = doc["components"]["schemas"]

    reachable = {
        profile: set().union(*(flatten(schemas, name) for name in names))
        for profile, names in SCHEMAS_BY_PROFILE.items()
    }
    configuration = flatten(schemas, "Configuration")
    for profile in doc["paths"]["/configuracion"]["get"]["tags"]:
        reachable[profile] |= configuration

    header = f"{'fila':44}" + "".join(f"{p:>12}" for p in PROFILES)
    print(header)
    failures = []
    for label, marker, expected in MATRIX:
        line = f"{label:44}"
        for profile, want in zip(PROFILES, expected):
            present = any(marker in field for field in reachable[profile])
            ok = present == bool(want)
            if not ok:
                failures.append((label, profile, bool(want), present))
            line += f"{('si' if present else 'no') + ('' if ok else ' X'):>12}"
        print(line)

    print()
    if failures:
        print(f"{len(failures)} celdas no coinciden con la matriz:")
        for label, profile, want, present in failures:
            print(f"  {label} / {profile}: la matriz dice {want}, el contrato {present}")
        return 1
    print(f"Las {len(MATRIX)} filas coinciden con la matriz para los cuatro perfiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
