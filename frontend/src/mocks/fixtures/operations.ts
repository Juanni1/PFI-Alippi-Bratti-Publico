// Fixtures de configuración, bandeja de revisión y cola, copiados de MK-05 y
// MK-07. Las marcas [VERIFICAR] de las láminas viajan como valor nulo con su
// medición declarada; la pantalla las dibuja y no las reemplaza por un número.

import type { Configuration, OracleOperation, QueueState, ReviewInbox } from "@/types"

export const CONFIGURATION: Configuration = {
  version: "config-2026-08-09",
  policyMode: "automatico_con_escalamiento",
  policyModeLabel: "resolución automática con escalamiento por excepción",
  // Los dos umbrales de confianza siguen sin valor elegido: al de derivación lo
  // gobierna la curva de RNF-15 y al de liquidación automática la de RNF-38, y
  // ninguna de las dos mediciones se corrió. Elegirlos antes de medir sería más
  // grave en el segundo caso, porque de ese lado el error del clasificador no lo
  // atrapa nadie.
  derivationThreshold: { value: null, measured: false, requirement: "RNF-15", unit: null },
  autoSettlementThreshold: { value: null, measured: false, requirement: "RNF-38", unit: null },
  // Los tres de abajo son decisión de configuración pendiente y no medición.
  amountLimit: { value: null, measured: false, requirement: "RF-34", unit: "ARS" },
  maxFileSizeMb: { value: null, measured: false, requirement: "RNF-10", unit: "MB" },
  maxFiles: { value: 5, measured: true, requirement: "RF-01", unit: null },
  maxRetries: { value: null, measured: false, requirement: "RF-13", unit: null },
  acceptedFormats: ["image/jpeg", "image/png"],
}

export const REVIEW_INBOX: ReviewInbox = {
  assignedTotal: 4,
  unopened: 2,
  ordering: "llegada",
  humanConfirmationBanner: null,
  rows: [
    {
      number: "SN-4088",
      statusId: "InconsistenciaEvidencia",
      cause: {
        id: "InconsistenciaConReglas",
        text: "La clase predicha contradice las reglas del caso [VERIFICAR: qué regla]",
        amountWasNotTheCause: false,
      },
      predictedClass: "rotura_vidrio",
      confidence: 0.88,
      declaredAmount: 95000,
      unopened: false,
      arrivedAt: "2026-08-09T11:09:00-03:00",
    },
    {
      number: "SN-4102",
      statusId: "RequiereRevision",
      cause: {
        id: "MontoSobreUmbral",
        text: "Monto declarado sobre el límite configurado",
        amountWasNotTheCause: false,
      },
      predictedClass: "abolladura",
      confidence: 0.91,
      declaredAmount: 1240000,
      unopened: false,
      arrivedAt: "2026-08-09T12:26:00-03:00",
    },
    {
      number: "SN-4119",
      statusId: "RequiereRevision",
      cause: {
        id: "EscalamientoObligatorio",
        text: "Escalamiento por verificación fallida · sin reintento automático de la inferencia",
        amountWasNotTheCause: false,
      },
      predictedClass: "rayon",
      confidence: 0.71,
      // La comprobación criptográfica no se completó, así que la fila rotula la
      // clase como sin acreditar (RNF-17).
      accredited: false,
      declaredAmount: 240000,
      unopened: true,
      arrivedAt: "2026-08-09T13:58:00-03:00",
    },
    {
      number: "SN-4127",
      statusId: "RequiereRevision",
      cause: {
        id: "ConfianzaBajoUmbral",
        text: "Confianza por debajo del umbral",
        amountWasNotTheCause: true,
      },
      predictedClass: "rayon",
      confidence: 0.63,
      runnerUpClass: "abolladura",
      declaredAmount: 180000,
      unopened: true,
      arrivedAt: "2026-08-09T14:37:00-03:00",
    },
  ],
  configuration: CONFIGURATION,
}

export const ORACLE_OPERATIONS_FIXTURE: OracleOperation[] = [
  {
    operationId: "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    number: "SN-4131",
    operation: "submit_inference",
    status: "incierta",
    transactionHash: "0x7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
    blockNumber: null,
    lastErrorCode: "RPC_TIMEOUT_AFTER_BROADCAST",
  },
  {
    operationId: "c3d4e5f6-a7b8-4012-8456-7890abcdef12",
    number: "SN-4133",
    operation: "resolve",
    status: "incierta",
    transactionHash: "0x3a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890abcdef12",
    blockNumber: null,
    lastErrorCode: "NONCE_TOO_LOW_OR_REPLACED",
  },
  {
    operationId: "b2c3d4e5-f6a7-4901-8345-6789abcdef01",
    number: "SN-4127",
    operation: "register_claim",
    status: "enviada",
    transactionHash: "0x4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b",
    blockNumber: null,
    lastErrorCode: null,
  },
]

export const QUEUE_STATE: QueueState = {
  counters: {
    pending: 3,
    running: 1,
    failed: 4,
    // Estimado a diez segundos por prueba, que es el tiempo medido de
    // preparación y generación juntas (RNF-01). El valor existe; lo que ningún
    // requerimiento acota es cuánto puede crecer, porque RNF-06 mide con la cola
    // vacía. La marca [VERIFICAR] de la espera es la de MK-03c, del lado del
    // reclamante, y no esta.
    lastPositionWait: { value: "≈ 25 s", measured: true, requirement: "RNF-01" },
  },
  // Uno por decisión de diseño de la cola y no por la memoria: el pico es de
  // 0,79 GB por prueba y la imposición aritmética de RNF-09 desapareció.
  concurrency: 1,
  running: [
    {
      number: "SN-4127",
      stage: "generacion",
      elapsedSeconds: 6,
      typicalSeconds: 10,
    },
  ],
  pending: [
    {
      number: "SN-4131",
      position: 1,
      enqueuedAt: "2026-08-09T14:47:04-03:00",
      estimatedStartAt: "2026-08-09T14:47:17-03:00",
    },
    {
      number: "SN-4133",
      position: 2,
      enqueuedAt: "2026-08-09T14:47:08-03:00",
      estimatedStartAt: "2026-08-09T14:47:27-03:00",
    },
    {
      number: "SN-4136",
      position: 3,
      enqueuedAt: "2026-08-09T14:47:11-03:00",
      estimatedStartAt: "2026-08-09T14:47:37-03:00",
    },
  ],
  // Las cuatro formas de fallar que prevé el procesamiento. La primera es
  // transitoria y las otras tres dan el mismo resultado en cada corrida: RF-13
  // no las distingue, y la lámina dibuja esa indistinción a propósito.
  failed: [
    {
      number: "SN-4104",
      reason: "evidencia_no_recuperada",
      attempt: 2,
      maxAttempts: { value: null, measured: false, requirement: "RF-13", unit: null },
      claimStatusId: "Registrado",
      definitive: false,
    },
    {
      number: "SN-4110",
      reason: "artefacto_no_declarado",
      attempt: 3,
      maxAttempts: { value: null, measured: false, requirement: "RF-13", unit: null },
      claimStatusId: "Registrado",
      definitive: true,
    },
    {
      number: "SN-4092",
      reason: "memoria_agotada",
      attempt: 3,
      maxAttempts: { value: null, measured: false, requirement: "RF-13", unit: null },
      claimStatusId: "Registrado",
      definitive: true,
    },
    {
      number: "SN-4115",
      reason: "verificacion_local_fallida",
      attempt: 3,
      maxAttempts: { value: null, measured: false, requirement: "RF-13", unit: null },
      claimStatusId: "Registrado",
      definitive: true,
    },
  ],
  processorStopped: false,
  oracleBlocked: true,
  oracleOperations: ORACLE_OPERATIONS_FIXTURE,
}

export function createQueueState(overrides: Partial<QueueState> = {}): QueueState {
  return {
    ...QUEUE_STATE,
    ...overrides,
    oracleOperations: overrides.oracleOperations
      ? [...overrides.oracleOperations]
      : [...QUEUE_STATE.oracleOperations],
  }
}

export const QUEUE_STATE_EMPTY_ORACLE: QueueState = {
  ...QUEUE_STATE,
  oracleBlocked: false,
  oracleOperations: [],
}
