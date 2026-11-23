// Vocabulario visible, en un solo lugar. Las cadenas de estado y de causa que
// lee el RECLAMANTE no están acá: las sirve el backend ya redactadas desde el
// diccionario de 00-criterios.md §1, porque una restricción que solo sostiene la
// presentación no es una restricción (adr-006, adr-013).
//
// Lo que sí vive acá es el rótulo de las seis clases y el de cada causa para las
// pantallas internas y para el auditor, que son audiencias distintas de la del
// diccionario.

import type { DamageClass, TransitionCauseId } from "@/types"

export const CLASSES: DamageClass[] = [
  "abolladura",
  "rayon",
  "rotura_vidrio",
  "faro_roto",
  "neumatico_desinflado",
  "fisura",
]

export const CLASS_LABEL: Record<DamageClass, string> = {
  abolladura: "abolladura",
  rayon: "rayón",
  rotura_vidrio: "rotura de vidrio",
  faro_roto: "faro roto",
  neumatico_desinflado: "neumático desinflado",
  fisura: "fisura",
}

// Los seis primeros están copiados de las láminas de MK-08, MK-08b y MK-08d; los
// restantes, de las filas de MK-05, porque ninguna lámina de la traza los dibuja.
export const CAUSE_LABEL: Record<TransitionCauseId, string> = {
  ClasificacionFavorable: "prueba verificada · condiciones favorables",
  FalloDeVerificacion: "el verificador devolvió un resultado negativo",
  EscalamientoObligatorio: "escalamiento obligatorio · sin reintento de la inferencia",
  LiquidacionDelOperador: "liquidación registrada por la aseguradora",
  LiquidacionAutomatica: "liquidación automática · confianza sobre el umbral",
  ResolucionFavorable: "resolución humana registrada",
  ResolucionDesfavorable: "resolución humana registrada",
  ConfianzaBajoUmbral: "confianza por debajo del umbral",
  MontoSobreUmbral: "monto declarado sobre el límite configurado",
  PoliticaConfirmacionHumana: "política de confirmación humana obligatoria",
  InconsistenciaConReglas: "la clase predicha contradice las reglas del caso",
}

// La creación del reclamo no es una transición de la matriz de adr-001 y el
// contrato la emite con su propio evento, sin causa.
export const ENTRY_LABEL = "creación del reclamo con el identificador de evidencia"

/**
 * El circuito emite logits y el porcentaje se calcula fuera de la prueba
 * (RF-16): el cálculo vive acá y no forma parte de lo que la prueba acredita.
 */
export function softmax(logits: number[]): number[] {
  const max = Math.max(...logits)
  const exponentials = logits.map((value) => Math.exp(value - max))
  const total = exponentials.reduce((a, b) => a + b, 0)
  return exponentials.map((value) => value / total)
}
