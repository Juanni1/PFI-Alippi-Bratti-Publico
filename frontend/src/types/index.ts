// Alias de los esquemas del contrato, para no escribir components["schemas"][…]
// en cada archivo.
//
// `api.ts` se GENERA desde PFI-Backend/contract/openapi.yaml con
// `npm run contract:types`; no se edita a mano. La herramienta se invoca por npx
// en lugar de instalarse porque su rango de pares pide TypeScript ^5 y este repo
// está en 7; la generación es un paso puntual y su salida se versiona.

import { z } from "zod"
import type { components } from "./api"

type Schemas = components["schemas"]

export type ClaimantClaim = Schemas["ClaimantClaim"]
export type ReviewerClaim = Schemas["ReviewerClaim"]
export type OperatorClaim = Schemas["OperatorClaim"]
export type PublicTrace = Schemas["PublicTrace"]

export type ClaimExplanation = Schemas["ClaimExplanation"]
export type RegistrationReceipt = Schemas["RegistrationReceipt"]
export type EvidenceRejection = Schemas["EvidenceRejection"]
export type ReviewInbox = Schemas["ReviewInbox"]
export type Configuration = Schemas["Configuration"]
export type PublicConfiguration = Schemas["PublicConfiguration"]

export type ClaimStatusId = Schemas["ClaimStatusId"]
export type TransitionCauseId = Schemas["TransitionCauseId"]
export type DamageClass = Schemas["DamageClass"]
export type ClaimantStatus = Schemas["ClaimantStatus"]
export type TimelineEntry = Schemas["TimelineEntry"]
export type Transition = Schemas["Transition"]
export type Receipt = Schemas["Receipt"]
export type UnmeasuredNumber = Schemas["UnmeasuredNumber"]
export type UnmeasuredString = Schemas["UnmeasuredString"]

// PFI-138: Reconciliación de operaciones del oráculo (ADR-018)
export const oracleOperationTypeSchema = z.enum([
  "register_claim",
  "submit_inference",
  "resolve",
  "settle",
])

export const oracleOperationStatusSchema = z.enum([
  "preparada",
  "firmada",
  "enviada",
  "confirmada",
  "revertida",
  "incierta",
])

export const oracleOperationSchema = z.object({
  operationId: z.string().uuid(),
  number: z.string(),
  operation: oracleOperationTypeSchema,
  status: oracleOperationStatusSchema,
  transactionHash: z.string().nullable(),
  blockNumber: z.number().int().nullable(),
  lastErrorCode: z.string().nullable(),
})

export const reconcileRequestSchema = z
  .object({
    operationId: z.string().uuid(),
  })
  .strict()

export type OracleOperationType = z.infer<typeof oracleOperationTypeSchema>
export type OracleOperationStatus = z.infer<typeof oracleOperationStatusSchema>
export type OracleOperation = z.infer<typeof oracleOperationSchema>
export type ReconcileRequest = z.infer<typeof reconcileRequestSchema>

export type QueueState = Omit<Schemas["QueueState"], "oracleBlocked" | "oracleOperations"> & {
  oracleBlocked: boolean
  oracleOperations: OracleOperation[]
}
