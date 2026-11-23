// Handlers de MSW escritos contra el contrato `backend/contract/openapi.yaml`.
// Cada handler devuelve el esquema que el contrato declara para su perfil.
//
// El perfil se resuelve acá sobre el token que acompaña al pedido y nunca sobre
// lo que declare el cliente (RF-48). En el mock el token es un prefijo legible;
// en el backend real es un claim del JWT. Lo que importa que el mock reproduzca
// es que el recorte lo hace el servidor: si el dato viajara en el JSON y solo la
// pantalla lo escondiera, la restricción no existiría.

import { HttpResponse, http } from "msw"
import {
  CLAIMS,
  EXPLANATIONS,
  RECEIPT_4127,
  REVIEWER_CLAIMS,
  TRACES,
} from "./fixtures/claims"
import type { OracleOperation, PublicConfiguration, QueueState } from "@/types"
import { reconcileRequestSchema } from "@/types"
import {
  CONFIGURATION,
  QUEUE_STATE_EMPTY_ORACLE,
  REVIEW_INBOX,
  createQueueState,
} from "./fixtures/operations"
import {
  countCodePoints,
  normalizeDamageDescription,
  validateNormalizedDamageDescription,
} from "@/lib/damage-description"

type Profile = "reclamante" | "revisor" | "operador"

function resolveProfile(request: Request): Profile | null {
  const header = request.headers.get("Authorization")
  if (!header?.startsWith("Bearer ")) return null
  const token = header.slice("Bearer ".length)
  if (token.startsWith("revisor")) return "revisor"
  if (token.startsWith("operador")) return "operador"
  if (token.startsWith("token-de-capacidad")) return "reclamante"
  return null
}

const unauthorized = () =>
  HttpResponse.json(
    { title: "Token ausente, vencido o emitido para otro reclamo" },
    { status: 401 },
  )

const forbidden = () =>
  HttpResponse.json(
    { title: "El perfil del token no admite la operación" },
    { status: 403 },
  )

const notFound = () =>
  HttpResponse.json({ title: "No existe un reclamo con ese número" }, { status: 404 })


let mockQueueState: QueueState = createQueueState()

export function resetMockQueueState(state?: QueueState) {
  mockQueueState = state ? createQueueState(state) : createQueueState()
}
export const handlers = [
  // MK-01 y MK-02. Sin Authorization se conserva el alta anónima; con sesión
  // solo puede registrar el operador. Un bearer inválido no cae al modo anónimo.
  http.post("*/reclamos", async ({ request }) => {
    const authorization = request.headers.get("Authorization")
    if (authorization !== null) {
      const profile = resolveProfile(request)
      if (!profile) return unauthorized()
      if (profile !== "operador") return forbidden()
    }

    const form = await request.formData()
    const rawDescription = form.get("damageDescription")
    if (typeof rawDescription !== "string") {
      return HttpResponse.json(
        { title: "La descripción del daño es obligatoria." },
        { status: 422 },
      )
    }

    const damageDescription = normalizeDamageDescription(rawDescription)
    const descriptionError = validateNormalizedDamageDescription(
      damageDescription,
      countCodePoints(damageDescription),
    )
    if (descriptionError) {
      return HttpResponse.json({ title: descriptionError }, { status: 422 })
    }

    return HttpResponse.json(
      {
        ...RECEIPT_4127,
        submitted: {
          ...RECEIPT_4127.submitted,
          damageDescription,
        },
      },
      { status: 201 },
    )
  }),

  // MK-03. El cuerpo depende del perfil: es la respuesta que hace comprobable
  // RF-46, y la evidencia que adr-006 pide para la dimensión 7 sale de pedir el
  // mismo reclamo con dos tokens distintos.
  http.get("*/reclamos/:numero", ({ params, request }) => {
    const profile = resolveProfile(request)
    if (!profile) return unauthorized()
    const numero = String(params.numero)

    if (profile === "revisor") {
      const claim = REVIEWER_CLAIMS[numero]
      return claim ? HttpResponse.json(claim) : notFound()
    }

    if (profile === "operador") {
      const claim = REVIEWER_CLAIMS[numero]
      if (!claim) return notFound()
      // El operador recibe el mismo reclamo sin el vector de logits. El recorte
      // ocurre del lado del servidor y por campo, no por pantalla.
      const { inference, cause, availableResolutions, assignment, ...rest } = claim
      void cause
      void availableResolutions
      void assignment
      const { logits, ...inferenceWithoutLogits } = inference
      void logits
      return HttpResponse.json({
        ...rest,
        inference: inferenceWithoutLogits,
        settleable: rest.statusId === "Aprobable",
      })
    }

    const claim = CLAIMS[numero]
    return claim ? HttpResponse.json(claim) : notFound()
  }),

  // MK-04.
  http.get("*/reclamos/:numero/explicacion", ({ params, request }) => {
    if (!resolveProfile(request)) return unauthorized()
    const explanation = EXPLANATIONS[String(params.numero)]
    if (explanation) return HttpResponse.json(explanation)
    // Excepción 2a de CU-02: el reclamo todavía no fue clasificado y no hay
    // explicación que dar.
    return HttpResponse.json(
      { title: "El reclamo todavía no fue clasificado" },
      { status: 409 },
    )
  }),

  // MK-05.
  http.get("*/revision/bandeja", ({ request }) => {
    const profile = resolveProfile(request)
    if (!profile) return unauthorized()
    if (profile !== "revisor") return forbidden()
    return HttpResponse.json(REVIEW_INBOX)
  }),

  // MK-06, salida del revisor.
  http.post("*/reclamos/:numero/resolucion", async ({ params, request }) => {
    const profile = resolveProfile(request)
    if (!profile) return unauthorized()
    if (profile !== "revisor") return forbidden()
    const body = (await request.json()) as { favorable: boolean; justification: string }
    return HttpResponse.json({
      number: String(params.numero),
      statusId: body.favorable ? "Aprobado" : "Rechazado",
      transition: {
        from: "RequiereRevision",
        to: body.favorable ? "Aprobado" : "Rechazado",
        cause: body.favorable ? "ResolucionFavorable" : "ResolucionDesfavorable",
        occurredAt: new Date().toISOString(),
        proofHash: null,
      },
    })
  }),

  // MK-06, salida del operador. Un solo control y ningún campo de texto: CU-06
  // no tiene equivalente de RF-39 y esta transición no registra autor.
  http.post("*/reclamos/:numero/liquidacion", ({ params, request }) => {
    const profile = resolveProfile(request)
    if (!profile) return unauthorized()
    if (profile !== "operador") return forbidden()
    return HttpResponse.json({
      number: String(params.numero),
      statusId: "Aprobado",
      transition: {
        from: "Aprobable",
        to: "Aprobado",
        cause: "LiquidacionDelOperador",
        occurredAt: new Date().toISOString(),
        proofHash: null,
      },
    })
  }),

  // MK-07. Monitor de la cola: exclusivo del operador.
  http.get("*/cola", ({ request }) => {
    const profile = resolveProfile(request)
    if (!profile) return unauthorized()
    if (profile !== "operador") return forbidden()

    const url = new URL(request.url)
    if (url.searchParams.get("empty") === "true") {
      return HttpResponse.json(QUEUE_STATE_EMPTY_ORACLE)
    }
    if (url.searchParams.get("reset") === "true") {
      mockQueueState = createQueueState()
      return HttpResponse.json(mockQueueState)
    }

    return HttpResponse.json(mockQueueState)
  }),

  // PFI-138. Reconciliación de operaciones del oráculo (exclusivo operador).
  http.post("*/cola/reconciliacion", async ({ request }) => {
    const profile = resolveProfile(request)
    if (!profile) return unauthorized()
    if (profile !== "operador") return forbidden()

    let body: unknown
    try {
      body = await request.json()
    } catch {
      return HttpResponse.json(
        { title: "El cuerpo del pedido debe ser JSON válido." },
        { status: 400 },
      )
    }

    const parsed = reconcileRequestSchema.safeParse(body)
    if (!parsed.success) {
      return HttpResponse.json(
        {
          title: "El cuerpo del pedido debe contener únicamente operationId válido (UUID).",
          errors: parsed.error.issues,
        },
        { status: 422 },
      )
    }

    const { operationId } = parsed.data
    const existingIndex = mockQueueState.oracleOperations.findIndex(
      (op) => op.operationId === operationId,
    )
    if (existingIndex === -1) {
      return HttpResponse.json(
        { title: "No existe una operación con ese identificador" },
        { status: 404 },
      )
    }

    const currentOp = mockQueueState.oracleOperations[existingIndex]

    // Caso de prueba: demostrar resultado que continúa incierto
    if (operationId === "c3d4e5f6-a7b8-4012-8456-7890abcdef12") {
      const unresolvedResult: OracleOperation = {
        ...currentOp,
        status: "incierta",
        lastErrorCode: "RECEIPT_NOT_FOUND_STILL_PENDING",
      }
      mockQueueState.oracleOperations[existingIndex] = unresolvedResult
      return HttpResponse.json(unresolvedResult)
    }

    // Por defecto: la operación se confirma en la cadena
    const confirmedResult: OracleOperation = {
      ...currentOp,
      status: "confirmada",
      blockNumber: 6241908,
      lastErrorCode: null,
    }

    // Al confirmarse, sale de la lista de operaciones no resueltas de la cola
    mockQueueState.oracleOperations.splice(existingIndex, 1)
    if (mockQueueState.oracleOperations.length === 0) {
      mockQueueState.oracleBlocked = false
    }

    return HttpResponse.json(confirmedResult)
  }),

  http.get("*/configuracion/publica", () =>
    HttpResponse.json<PublicConfiguration>({
      maxFiles: CONFIGURATION.maxFiles,
      maxFileSizeMb: CONFIGURATION.maxFileSizeMb,
      amountLimit: CONFIGURATION.amountLimit,
      acceptedFormats: CONFIGURATION.acceptedFormats ?? ["image/jpeg", "image/png"],
    }),
  ),

  http.get("*/configuracion", ({ request }) => {
    if (!resolveProfile(request)) return unauthorized()
    return HttpResponse.json(CONFIGURATION)
  }),

  // MK-08. Sin acreditación de titularidad y con la lista de campos cerrada.
  http.get("*/publico/reclamos/:numero/traza", ({ params }) => {
    const trace = TRACES[String(params.numero)]
    return trace ? HttpResponse.json(trace) : notFound()
  }),
]
