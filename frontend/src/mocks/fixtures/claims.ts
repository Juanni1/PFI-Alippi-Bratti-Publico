// Fixtures del recorrido del reclamante y de la traza pública.
//
// Los estados y valores visibles siguen el diccionario de estados y las vistas
// de especificación del prototipo. Los relatos de daño son texto sintético para
// la demostración MSW: no describen personas ni reclamos reales.
// SN-4103 recorre la línea de tiempo, la liquidación y la traza pública; SN-4155
// es el caso de liquidación automática que cruza los tres actores; SN-4088 va
// de la bandeja al rechazo por inconsistencia; SN-4119 aparece escalado con su
// fallo asentado en MK-08b; SN-4127 es el caso de la constancia y la explicación.
//
// Los identificadores de contenido, los hashes, la dirección del contrato y los
// números de bloque están copiados de las láminas, que los declaran valores de
// ejemplo. Aparecen abreviados porque así están dibujados; completarlos sería
// fabricar datos con apariencia de reales.
//
// Las cadenas de etiqueta y causa se copian del diccionario de
// 00-criterios.md §1 y no se redactan de nuevo.

import type {
  ClaimExplanation,
  ClaimantClaim,
  PublicTrace,
  RegistrationReceipt,
  ReviewerClaim,
} from "@/types"

const CONTRACT_ADDRESS = "0x5b1e…9042"
const PROVENANCE = `Base Sepolia · contrato ${CONTRACT_ADDRESS} · reconstruida desde los eventos del contrato`
const DAMAGE_DESCRIPTION_4127 =
  "Noté rayones y una abolladura en la parte trasera, cerca del paragolpes."

const NOT_EXPOSED = [
  "monto declarado",
  "imagen de la evidencia",
  "clase predicha",
  "confianza",
  "identidad de quien resolvió",
  "identidad de quien carga",
  "texto de la justificación",
  "descripción del daño",
]

const IMAGE_DISCLOSURE =
  "El identificador permite recuperar el archivo desde IPFS: que la vista no lo muestre es " +
  "una decisión de presentación y no una restricción de acceso."

// El vector tiene 576 posiciones y el contrato las exige todas. Las de los
// fixtures son un relleno declarado y no una medición: la única corrida con
// vector versionado vive en PFI-MachineLearning, y copiarla acá la presentaría
// como salida de un reclamo que no existe.
const PLACEHOLDER_FEATURE_VECTOR = Array.from({ length: 576 }, (_, i) =>
  Number((Math.sin(i) * 0.5 + 0.5).toFixed(4)),
)

const PROOF_ARTIFACTS = {
  proofUrl: "/artefactos/prueba.json",
  publicInputsUrl: "/artefactos/entradas-publicas.json",
  circuitParameters: { logrows: 17, esquema: "Halo2/KZG", curva: "BN254" },
  verifyingKeyUrl: "/artefactos/vk.key",
  verifierAddress: CONTRACT_ADDRESS,
}

// Artefactos publicados en IPFS para la replicación externa (RF-53, ADR-011).
const REPLICATION_ARTIFACTS = {
  extractorOnnxUrl:
    "https://gateway.pinata.cloud/ipfs/bafybeiczwkityamcb73l5lgvleom3zapa7grfny3abov3kp5ygu5ebffgu",
  extractorOnnxHash:
    "96c616e82bbf9617c9b6cdb935f9fdc9c4505727a9742c8413e2e90c83c50a6a",
  preprocessingSpecUrl:
    "https://gateway.pinata.cloud/ipfs/bafkreie2sfkzvshkdqybyujyrk4zzcdzrczqvxuhlvnzcn7t2phl5uiyom",
  runtimeVersion: "mnv3_ft-r224",
}

// ---------------------------------------------------------------------------
// SN-4103 — aprobado con liquidación registrada por la aseguradora (MK-03, MK-08)
// ---------------------------------------------------------------------------

export const CLAIM_4103: ClaimantClaim = {
  number: "SN-4103",
  status: {
    label: "Aprobado",
    cause:
      "El análisis dio favorable y la aseguradora registró la liquidación.",
  },
  timeline: [
    {
      occurredAt: "2026-08-09T14:31:00-03:00",
      label: "Recibido, en análisis",
      cause: "Recibimos tus fotos y entraron a la cola de análisis.",
    },
    {
      occurredAt: "2026-08-09T14:36:00-03:00",
      label: "Análisis favorable, pendiente de liquidación",
      cause:
        "El análisis dio favorable. Quedó pendiente el registro del pago.",
    },
    {
      occurredAt: "2026-08-09T15:12:00-03:00",
      label: "Aprobado",
      cause: "La aseguradora registró la liquidación.",
    },
  ],
  claim: {
    incidentDate: "2026-08-08",
    damageDescription: "Después de estacionar, noté rayones en la puerta delantera.",
    photoCount: 3,
    declaredAmount: 180000,
  },
  queue: null,
  resolution: null,
  appealNotice: null,
  explanationAvailable: true,
  receipt: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    transactionHashes: ["0x9c31…a7f2", "0x4e77…10bc", "0xb0d2…7e39"],
    proofHash: "0x2a8f…c451",
    publicTraceUrl: "/traza/SN-4103",
  },
}

export const TRACE_4103: PublicTrace = {
  number: "SN-4103",
  statusId: "Aprobado",
  statusIsTerminal: true,
  provenance: PROVENANCE,
  transitions: [
    {
      from: null,
      to: "Registrado",
      cause: null,
      occurredAt: "2026-08-09T14:31:00-03:00",
      blockNumber: 8914203,
      transactionHash: "0x9c31…a7f2",
      proofHash: null,
    },
    {
      from: "Registrado",
      to: "Aprobable",
      cause: "ClasificacionFavorable",
      occurredAt: "2026-08-09T14:36:00-03:00",
      blockNumber: 8914361,
      transactionHash: "0x4e77…10bc",
      proofHash: "0x2a8f…c451",
    },
    {
      from: "Aprobable",
      to: "Aprobado",
      cause: "LiquidacionDelOperador",
      occurredAt: "2026-08-09T15:12:00-03:00",
      blockNumber: 8915088,
      transactionHash: "0xb0d2…7e39",
      proofHash: null,
    },
  ],
  evidence: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    retrievable: true,
    digestMatches: true,
  },
  featureVector: PLACEHOLDER_FEATURE_VECTOR,
  artifacts: { proof: PROOF_ARTIFACTS, replication: REPLICATION_ARTIFACTS },
  notExposed: NOT_EXPOSED,
  imageDisclosureNotice: IMAGE_DISCLOSURE,
}

// ---------------------------------------------------------------------------
// SN-4127 — derivado por confianza bajo el umbral (MK-02, MK-04, MK-05, MK-07)
// ---------------------------------------------------------------------------

export const CLAIM_4127: ClaimantClaim = {
  number: "SN-4127",
  status: {
    label: "En revisión por un analista",
    cause:
      "La certeza del análisis automático no alcanzó para resolver de forma directa.",
  },
  timeline: [
    {
      occurredAt: "2026-08-09T14:31:00-03:00",
      label: "Recibido, en análisis",
      cause: "Recibimos tus fotos y entraron a la cola de análisis.",
    },
    {
      occurredAt: "2026-08-09T14:37:00-03:00",
      label: "En revisión por un analista",
      cause:
        "La certeza del análisis automático no alcanzó para resolver de forma directa.",
    },
  ],
  claim: {
    incidentDate: "2026-08-08",
    damageDescription: DAMAGE_DESCRIPTION_4127,
    photoCount: 3,
    declaredAmount: 180000,
  },
  queue: null,
  resolution: null,
  appealNotice: null,
  explanationAvailable: true,
  receipt: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    transactionHashes: ["0x7f9c4e2a…b83e21"],
    proofHash: "0x2a8f…c451",
    publicTraceUrl: "/traza/SN-4127",
  },
}

export const RECEIPT_4127: RegistrationReceipt = {
  number: "SN-4127",
  registeredAt: "2026-08-09T14:31:00-03:00",
  status: {
    label: "Recibido, en análisis",
    cause:
      "Recibimos tus fotos y las estamos analizando. Demora unos minutos.",
  },
  submitted: {
    photoCount: 3,
    declaredAmount: 180000,
    damageDescription: DAMAGE_DESCRIPTION_4127,
  },
  receipt: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    transactionHashes: ["0x7f9c4e2a…b83e21"],
    proofHash: null,
    publicTraceUrl: "/traza/SN-4127",
  },
  accessToken: "token-de-capacidad-de-ejemplo-SN-4127",
  onChainScope:
    "En la cadena quedó registrado el identificador de las fotos, no las fotos. " +
    "Las imágenes viven en IPFS y se recuperan con ese identificador.",
}

export const EXPLANATION_4127: ClaimExplanation = {
  evidence: {
    fileCount: 3,
    submittedAt: "2026-08-09T14:31:00-03:00",
    publishedFeatureVector: {
      published: true,
      dimension: 576,
      notice:
        "Las fotos quedan accesibles por su identificador en IPFS. El análisis utiliza un resumen numérico calculado a partir de ellas.",
    },
  },
  model: { predictedClass: "rayon", confidence: 0.63, accredited: true },
  proof: {
    provenSegment:
      "Que el análisis se ejecutó tal como está descripto acá, y que lo hizo sobre el resumen numérico que se extrajo de tus fotos y no sobre otro.",
    replicatedSegment:
      "Eso queda fuera de la comprobación automática. Se rehace el cálculo con la herramienta publicada y se compara; cualquiera puede hacerlo.",
    disclaimer:
      "Por qué el modelo reconoció un rayón: comprueba la ejecución, no el criterio con el que el modelo decidió.",
    verificationFailed: false,
  },
  rule: {
    // El umbral de derivación espera la medición de RNF-15 y por eso viaja
    // nulo: la pantalla dibuja [VERIFICAR] y no un número plausible.
    confidenceAgainstThreshold: {
      observed: 0.63,
      threshold: { value: null, measured: false, requirement: "RNF-15", unit: null },
      satisfied: null,
    },
    amountAgainstLimit: {
      observed: 180000,
      threshold: { value: null, measured: false, requirement: "RF-34", unit: "ARS" },
      satisfied: true,
    },
    autoSettlementApplied: false,
  },
  outcome: {
    label: "En revisión de un analista",
    cause:
      "El análisis automático no alcanzó la certeza mínima para resolver solo. Un analista de la aseguradora va a revisar tu caso.",
  },
}

export const TRACE_4127: PublicTrace = {
  number: "SN-4127",
  statusId: "RequiereRevision",
  statusIsTerminal: false,
  provenance: PROVENANCE,
  transitions: [
    {
      from: null,
      to: "Registrado",
      cause: null,
      occurredAt: "2026-08-09T14:31:00-03:00",
      blockNumber: 8914205,
      transactionHash: "0x7f9c4e2a…b83e21",
      proofHash: null,
    },
    {
      from: "Registrado",
      to: "RequiereRevision",
      cause: "ConfianzaBajoUmbral",
      occurredAt: "2026-08-09T14:37:00-03:00",
      blockNumber: 8914372,
      transactionHash: "0x63b8…f019",
      proofHash: "0x2a8f…c451",
    },
  ],
  evidence: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    retrievable: true,
    digestMatches: true,
  },
  featureVector: PLACEHOLDER_FEATURE_VECTOR,
  artifacts: { proof: PROOF_ARTIFACTS, replication: REPLICATION_ARTIFACTS },
  notExposed: NOT_EXPOSED,
  imageDisclosureNotice: IMAGE_DISCLOSURE,
}

// El único fixture con vector de logits: es la celda de la matriz que da sentido
// a filtrar por campo y no por pantalla.
export const REVIEWER_CLAIM_4127: ReviewerClaim = {
  number: "SN-4127",
  statusId: "RequiereRevision",
  status: {
    label: "En revisión de un analista",
    cause:
      "El análisis automático no alcanzó la certeza mínima para resolver solo.",
  },
  damageDescription: DAMAGE_DESCRIPTION_4127,
  registeredBy: "op-02",
  evidence: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    files: [
      // Ilustraciones PNG sintéticas locales; los nombres originales del
      // expediente de ejemplo no determinan el formato servido por la URL.
      { filename: "frente-izq.jpg", sha256: "…", sizeBytes: 2_400_000, url: "/evidence-fixtures/vehicle-front.png" },
      { filename: "paragolpes.jpg", sha256: "…", sizeBytes: 3_100_000, url: "/evidence-fixtures/vehicle-rear.png" },
      { filename: "detalle-faro.png", sha256: "…", sizeBytes: 1_800_000, url: "/evidence-fixtures/vehicle-detail.png" },
    ],
  },
  inference: {
    predictedClass: "rayon",
    confidence: 0.63,
    // En el orden de las seis clases: abolladura, rayón, rotura de vidrio, faro
    // roto, neumático desinflado y corrosión. El softmax de estos valores da
    // rayón 63% y abolladura 28%, que es lo que dibujan MK-04 y MK-05.
    logits: [1.19, 2.0, -1.2, -1.33, -1.45, -1.6],
    modelVersion: "cabeza-ajustada-2026-08-14",
    artifactHashes: { extractor: "…", head: "…" },
  },
  cause: {
    id: "ConfianzaBajoUmbral",
    text: "El análisis automático no alcanzó la certeza mínima para resolver solo.",
    amountWasNotTheCause: true,
  },
  trace: TRACE_4127.transitions,
  declaredAmount: 180000,
  assignment: { unopened: true, assignedAt: "2026-08-09T14:37:00-03:00" },
  availableResolutions: ["favorable", "desfavorable"],
}

// ---------------------------------------------------------------------------
// SN-4155 — liquidación automática (MK-03d, MK-04c, MK-08d)
// ---------------------------------------------------------------------------

export const CLAIM_4155: ClaimantClaim = {
  number: "SN-4155",
  status: {
    label: "Aprobado",
    cause:
      "El análisis superó el umbral de certeza y la liquidación se registró de forma automática.",
  },
  timeline: [
    {
      occurredAt: "2026-08-10T09:12:04-03:00",
      label: "Recibido, en análisis",
      cause: "Recibimos tus fotos y entraron a la cola de análisis.",
    },
    {
      occurredAt: "2026-08-10T09:16:41-03:00",
      label: "Análisis favorable, pendiente de liquidación",
      cause:
        "El análisis dio favorable. Quedó pendiente el registro del pago.",
    },
    {
      occurredAt: "2026-08-10T09:16:44-03:00",
      label: "Aprobado",
      cause:
        "El análisis superó el umbral de certeza y la liquidación se registró de forma automática.",
    },
  ],
  claim: {
    incidentDate: "2026-08-09",
    photoCount: 3,
    declaredAmount: 140000,
    damageDescription: "La parte delantera del vehículo quedó dañada.",
  },
  queue: null,
  resolution: null,
  appealNotice: null,
  explanationAvailable: true,
  receipt: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    transactionHashes: ["0x71ac…3d08", "0xc4e9…88b1", "0x1d85…4a60"],
    proofHash: "0x6f30…ad72",
    publicTraceUrl: "/traza/SN-4155",
  },
}

export const TRACE_4155: PublicTrace = {
  number: "SN-4155",
  statusId: "Aprobado",
  statusIsTerminal: true,
  provenance: PROVENANCE,
  transitions: [
    {
      from: null,
      to: "Registrado",
      cause: null,
      occurredAt: "2026-08-10T09:12:04-03:00",
      blockNumber: 8921774,
      transactionHash: "0x71ac…3d08",
      proofHash: null,
    },
    {
      from: "Registrado",
      to: "Aprobable",
      cause: "ClasificacionFavorable",
      occurredAt: "2026-08-10T09:16:41-03:00",
      blockNumber: 8921912,
      transactionHash: "0xc4e9…88b1",
      proofHash: "0x6f30…ad72",
    },
    // La causa que distingue el camino, en el mismo campo de RF-26. Es la que
    // ClaimRegistry.sol todavía no puede emitir por separado.
    {
      from: "Aprobable",
      to: "Aprobado",
      cause: "LiquidacionAutomatica",
      occurredAt: "2026-08-10T09:16:44-03:00",
      blockNumber: 8921913,
      transactionHash: "0x1d85…4a60",
      proofHash: null,
    },
  ],
  evidence: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    retrievable: true,
    digestMatches: true,
  },
  featureVector: PLACEHOLDER_FEATURE_VECTOR,
  artifacts: { proof: PROOF_ARTIFACTS, replication: REPLICATION_ARTIFACTS },
  notExposed: NOT_EXPOSED,
  imageDisclosureNotice: IMAGE_DISCLOSURE,
}

// ---------------------------------------------------------------------------
// SN-4088 — rechazado tras inconsistencia de evidencia (MK-03b, MK-05)
// Este reclamo es anterior al campo y sirve para recorrer la variante legacy.
// ---------------------------------------------------------------------------

export const CLAIM_4088: ClaimantClaim = {
  number: "SN-4088",
  status: {
    label: "Rechazado",
    cause:
      "Un analista revisó el caso y registró la resolución. Justificación: se confirmó que el daño clasificado no es compatible con las reglas del reclamo. [VERIFICAR: qué regla]",
  },
  timeline: [
    {
      occurredAt: "2026-08-09T11:02:00-03:00",
      label: "Recibido, en análisis",
      cause: "Recibimos tus fotos y entraron a la cola de análisis.",
    },
    {
      occurredAt: "2026-08-09T11:09:00-03:00",
      label: "En revisión: daño incompatible",
      cause:
        "El sistema detectó rotura de vidrio, incompatible con lo declarado.",
    },
    {
      occurredAt: "2026-08-09T17:40:00-03:00",
      label: "Rechazado",
      cause: "Un analista de la aseguradora registró la resolución.",
    },
  ],
  claim: {
    incidentDate: "2026-08-08",
    photoCount: 2,
    declaredAmount: 95000,
    damageDescription: null,
  },
  queue: null,
  resolution: {
    role: "analista",
    justification:
      "Se confirmó que el daño clasificado no es compatible con las reglas del reclamo. [VERIFICAR: qué regla]",
    justificationHash: "0x3ad1…9b04",
  },
  // Enunciada en texto y sin control funcional: el punto resuelto 4 de adr-001
  // modela la apelación como reclamo nuevo, con su caso de uso fuera del
  // prototipo.
  appealNotice:
    "Podés iniciar un reclamo nuevo sobre el mismo siniestro comunicándote con la aseguradora. Este reclamo queda cerrado y su registro no cambia.",
  explanationAvailable: true,
  receipt: {
    evidenceId: "bafybeigdyrzt5sfp7ud…lgtqy55fbzdi",
    transactionHashes: ["0x5c02…11ae", "0x88fe…4dd7"],
    proofHash: "0x1b74…6e0a",
    publicTraceUrl: "/traza/SN-4088",
  },
}

// ---------------------------------------------------------------------------
// SN-4119 — escalado por verificación fallida (MK-05, MK-08b)
// ---------------------------------------------------------------------------

export const TRACE_4119: PublicTrace = {
  number: "SN-4119",
  statusId: "Aprobado",
  statusIsTerminal: true,
  provenance: PROVENANCE,
  transitions: [
    {
      from: null,
      to: "Registrado",
      cause: null,
      occurredAt: "2026-08-09T13:51:00-03:00",
      blockNumber: 8913902,
      transactionHash: "0x1f04\u202688ad",
      proofHash: null,
    },
    // El verificador devolvió un resultado negativo y el contrato lo asentó sin
    // revertir (RF-24). Es el evento que más interesa dejar registrado: si el
    // contrato revirtiera, sería el único del ciclo que no deja rastro.
    {
      from: "Registrado",
      to: "VerificacionFallida",
      cause: "FalloDeVerificacion",
      occurredAt: "2026-08-09T13:57:00-03:00",
      blockNumber: 8913988,
      transactionHash: "0x77ba\u20263e10",
      proofHash: "0x6d19\u2026f0a2",
    },
    {
      from: "VerificacionFallida",
      to: "RequiereRevision",
      cause: "EscalamientoObligatorio",
      occurredAt: "2026-08-09T13:58:00-03:00",
      blockNumber: 8913997,
      transactionHash: "0xc402\u20269b71",
      proofHash: null,
    },
    // Invariante 5 de adr-001: un reclamo que pasó por VerificacionFallida no
    // llega a Aprobado sin intervención humana registrada.
    {
      from: "RequiereRevision",
      to: "Aprobado",
      cause: "ResolucionFavorable",
      occurredAt: "2026-08-09T16:20:00-03:00",
      blockNumber: 8916410,
      transactionHash: "0x0b53\u2026e7c4",
      proofHash: null,
    },
  ],
  evidence: {
    evidenceId: "bafybeigdyrzt5sfp7ud\u2026lgtqy55fbzdi",
    retrievable: true,
    digestMatches: true,
  },
  featureVector: PLACEHOLDER_FEATURE_VECTOR,
  artifacts: { proof: PROOF_ARTIFACTS, replication: REPLICATION_ARTIFACTS },
  notExposed: NOT_EXPOSED,
  imageDisclosureNotice: IMAGE_DISCLOSURE,
}

export const CLAIMS: Record<string, ClaimantClaim> = {
  "SN-4103": CLAIM_4103,
  "SN-4127": CLAIM_4127,
  "SN-4155": CLAIM_4155,
  "SN-4088": CLAIM_4088,
}

export const TRACES: Record<string, PublicTrace> = {
  "SN-4103": TRACE_4103,
  "SN-4127": TRACE_4127,
  "SN-4155": TRACE_4155,
  "SN-4119": TRACE_4119,
}

export const EXPLANATIONS: Record<string, ClaimExplanation> = {
  "SN-4127": EXPLANATION_4127,
}

export const REVIEWER_CLAIMS: Record<string, ReviewerClaim> = {
  "SN-4127": REVIEWER_CLAIM_4127,
}
