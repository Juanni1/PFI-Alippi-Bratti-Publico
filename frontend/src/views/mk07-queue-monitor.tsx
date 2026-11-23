// MK-07 — Monitor de la cola de generación de pruebas.
//
// El monitor observa y no ofrece controles: CU-10 no produce cambios de estado.
// La ausencia de un control de reencolado no es olvido del dibujo: ningún
// requerimiento habilita devolver a la cola un trabajo fallido definitivo, y ese
// hueco está anotado en 00-criterios.md §4.

import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { OracleOperation, QueueState } from "@/types"
import { Link } from "react-router-dom"
import { apiGet, apiPost } from "@/lib/api"
import { useStaffToken } from "@/lib/session"
import {
  CopyableValue,
  Empty,
  Loading,
  Note,
  Screen,
  Section,
  Unmeasured,
  describeError,
  formatDateTime,
  formatMoney,
  formatTime,
} from "@/components/screen"
import { cn } from "@/lib/utils"

const FAILURE_TEXT: Record<string, string> = {
  evidencia_no_recuperada: "No se pudieron recuperar las fotos desde IPFS.",
  artefacto_no_declarado: "El hash del modelo ONNX no coincide con el publicado.",
  memoria_agotada: "La generación de la prueba agotó la memoria disponible.",
  verificacion_local_fallida:
    "Falló la verificación local de la prueba antes del envío.",
}

const STAGE_TEXT: Record<string, string> = {
  recuperacion: "Recuperando evidencia",
  extraccion: "Extrayendo características",
  generacion: "Generando prueba zk",
  verificacion_local: "Verificando en local",
  envio: "Enviando a la red",
}

const OPERATION_LABEL: Record<string, string> = {
  register_claim: "Registro del reclamo",
  submit_inference: "Envío de inferencia",
  resolve: "Resolución del revisor",
  settle: "Liquidación",
}

const STATUS_LABEL: Record<string, string> = {
  preparada: "Preparada",
  firmada: "Firmada",
  enviada: "Enviada",
  confirmada: "Confirmada",
  revertida: "Revertida",
  incierta: "Incierta",
}

const STATUS_BADGE: Record<string, string> = {
  preparada: "bg-muted text-muted-foreground",
  firmada: "bg-muted text-muted-foreground",
  enviada: "bg-blue-500/10 text-blue-700 dark:text-blue-400",
  confirmada: "bg-[#12A57A]/10 text-[#0B6B50] dark:text-[#12A57A]",
  revertida: "bg-destructive/10 text-destructive",
  incierta: "bg-[#A16207]/15 text-[#8A5306] dark:text-[#F59E0B] font-semibold",
}

export function MK07QueueMonitor() {
  const token = useStaffToken()

  const queryClient = useQueryClient()
  const [reconcilingId, setReconcilingId] = useState<string | null>(null)
  const [lastOutcome, setLastOutcome] = useState<{
    operationId: string
    result: OracleOperation | null
    error: string | null
  } | null>(null)

  const reconcileMutation = useMutation({
    mutationFn: (operationId: string) =>
      apiPost<OracleOperation>("/cola/reconciliacion", { operationId }, token),
    onSuccess: (result, operationId) => {
      setLastOutcome({
        operationId,
        result,
        error: null,
      })
      queryClient.invalidateQueries({ queryKey: ["cola"] })
    },
    onError: (err, operationId) => {
      setLastOutcome({
        operationId,
        result: null,
        error: describeError(err, {
          401: "El token de la sesión no lo reconoce el servidor.",
          403: "La sesión no tiene el perfil de operador.",
          404: "La operación no existe en el registro.",
          409: "La operación continúa en conflicto o estado incierto.",
        }),
      })
      queryClient.invalidateQueries({ queryKey: ["cola"] })
    },
    onSettled: () => {
      setReconcilingId(null)
    },
  })

  function handleReconcile(operationId: string) {
    setReconcilingId(operationId)
    setLastOutcome(null)
    reconcileMutation.mutate(operationId)
  }
  const { data, error, isPending } = useQuery({
    queryKey: ["cola", token],
    queryFn: () => apiGet<QueueState>("/cola", token),
    enabled: Boolean(token),
    retry: false,
  })

  if (error || !token) {
    return (
      <Screen context="Aseguradora · Procesamiento" session="" title="Monitor de la cola">
        <Note>
          {token
            ? describeError(error, {
                401: "El token de la sesión no lo reconoce el servidor.",
                403: "La sesión no tiene el perfil de operador.",
              })
            : "La sesión no tiene token: se elige en la barra lateral."}
        </Note>
      </Screen>
    )
  }
  if (isPending || !data) {
    return (
      <Screen context="Aseguradora · Procesamiento" session="" title="Monitor de la cola">
        <Loading>Leyendo el estado de la cola…</Loading>
      </Screen>
    )
  }

  return (
    <Screen
      context="Aseguradora · Procesamiento"
      session={`Sesión: ${token.replace("operador:", "")}`}
      title="Monitor de la cola"
      subtitle="Generación de pruebas"
    >
      {/* 1 y 11 — Contadores. La espera del último de la lista es el número que
          ningún requerimiento acota: RNF-06 mide con la cola vacía. */}
      <Section>
        <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-xs text-muted-foreground">Pendientes</dt>
            <dd className="text-2xl font-semibold">{data.counters.pending}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">En curso</dt>
            <dd className="text-2xl font-semibold">{data.counters.running}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Fallidos</dt>
            <dd className="text-2xl font-semibold">{data.counters.failed}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Espera del último</dt>
            <dd className="text-2xl font-semibold">
              {data.counters.lastPositionWait ? (
                <Unmeasured value={data.counters.lastPositionWait} />
              ) : (
                "sin estimar"
              )}
            </dd>
          </div>
        </dl>
      </Section>

      {/* 12 y 13 — Con el procesador detenido el trabajo vuelve a la cola y
          conserva su lugar; reanudarlo es operación de infraestructura y queda
          fuera de la pantalla. */}
      {data.processorStopped ? (
        <Section>
          <Note>
            El procesador está detenido. El trabajo que estaba en curso volvió a la cola y
            conserva su lugar.
          </Note>
        </Section>
      ) : null}

      {/* Aviso de billetera del oráculo bloqueada (PFI-138) */}
      {data.oracleBlocked ? (
        <Section>
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-300">
            <p className="font-semibold">Billetera del oráculo bloqueada</p>
            <p className="mt-1 leading-relaxed text-muted-foreground">
              Existen operaciones en la cadena pendientes de confirmación o en estado
              incierto. Las nuevas emisiones permanecen en espera hasta su
              reconciliación para evitar colisión de nonces o duplicación de efectos.
            </p>
          </div>
        </Section>
      ) : null}

      {/* 2, 3 y 5 — Trabajo en curso y el fundamento de la concurrencia uno. */}
      <Section title="Generando ahora" hint={`concurrencia ${data.concurrency}`}>
        {data.running.length === 0 ? (
          <Note>Sin trabajo en curso.</Note>
        ) : (
          data.running.map((job) => (
            <p key={job.number} className="text-sm">
              {job.number} · {STAGE_TEXT[job.stage] ?? job.stage} ·{" "}
              {job.elapsedSeconds} s transcurridos
              {job.typicalSeconds ? ` · duración típica ${job.typicalSeconds} s` : null}
            </p>
          ))
        )}
        <div className="mt-3">
          <Note>
            Las pruebas se generan de a una, por diseño de la cola. El reclamo sigue en
            Registrado hasta que haya prueba que enviar.
          </Note>
        </div>
      </Section>

      {/* 4 — Lista de pendientes con su comienzo estimado. */}
      <Section title="En cola" hint="orden de llegada: el planificador toma el más antiguo">
        {data.pending.length === 0 ? <Empty>La cola no tiene pendientes.</Empty> : null}
        <ol className="space-y-2 text-sm">
          {data.pending.map((job) => (
            <li key={job.number} className="flex flex-wrap justify-between gap-2">
              <span>
                {job.position}. {job.number}
              </span>
              <span className="text-xs text-muted-foreground">
                encolado {formatTime(job.enqueuedAt, true)}
                {job.estimatedStartAt
                  ? ` · comienza ≈ ${formatTime(job.estimatedStartAt, true)}`
                  : null}
              </span>
            </li>
          ))}
        </ol>
      </Section>

      {/* 6 a 9 — Las cuatro formas de fallar, con el estado del reclamo al lado,
          que es el elemento que materializa RF-13. Las cuatro llevan la misma
          leyenda de reintento porque el requerimiento no distingue los fallos
          que el reintento corrige de los que dan idéntico resultado en cada
          corrida. */}
      <Section title="Fallidos" hint="dónde se cortó la generación">
        {data.failed.length === 0 ? (
          <Empty>Ningún trabajo falló todavía.</Empty>
        ) : null}
        <ul className="space-y-3 text-sm">
          {data.failed.map((job) => (
            <li key={job.number}>
              <div className="flex flex-wrap justify-between gap-2">
                <span className="font-medium">{job.number}</span>
                <span className="text-xs text-muted-foreground">
                  {job.definitive ? "Fallido definitivo" : "Reintentando"}
                </span>
              </div>
              <p className="text-xs text-muted-foreground">{FAILURE_TEXT[job.reason]}</p>
              <p className="text-xs text-muted-foreground">
                intento {job.attempt}
                {job.definitive && job.maxAttempts ? (
                  <>
                    {" "}
                    · alcanzó el máximo de reintentos configurado (
                    <Unmeasured value={job.maxAttempts} />)
                  </>
                ) : null}{" "}
                · reclamo en {job.claimStatusId}
              </p>
            </li>
          ))}
        </ul>
      </Section>

      {/* Los que esperan la liquidación de RF-37. No son trabajos de la cola y
          por eso van después de los tres bloques que sí lo son. El operador no
          tenía por dónde llegar a liquidar: la bandeja de RF-36 es del revisor y
          CU-06 no pide su equivalente, y ese hueco estaba anotado en
          00-criterios.md §4 desde que se dibujaron las pantallas. Listar no es
          un control: sigue sin haber nada acá que cambie un estado. */}
      <Section
        title="Pendientes de liquidación"
        hint="análisis favorable, esperando que la aseguradora registre la liquidación"
      >
        {(data.settleable ?? []).length === 0 ? (
          <Empty>Ningún reclamo espera liquidación.</Empty>
        ) : (
          <ul className="divide-y text-sm">
            {(data.settleable ?? []).map((reclamo) => (
              <li
                key={reclamo.number}
                className="flex flex-wrap items-baseline justify-between gap-2 py-3"
              >
                <span className="font-medium">{reclamo.number}</span>
                <span className="text-xs text-muted-foreground">
                  {reclamo.declaredAmount != null
                    ? `monto declarado ${formatMoney(reclamo.declaredAmount)}`
                    : null}
                  {reclamo.approvableSince
                    ? ` · desde ${formatDateTime(reclamo.approvableSince)}`
                    : null}
                </span>
                <Link
                  to={`/resolucion/${reclamo.number}`}
                  className="text-xs underline underline-offset-4"
                >
                  abrir ›
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {/* PFI-138: Reconciliación de operaciones del oráculo */}
      <Section
        title="Operaciones del oráculo"
        hint="emisiones hacia la cadena y reconciliación de la billetera"
      >
        {lastOutcome ? (
          <div
            className={cn(
              "mb-4 rounded-md border p-3 text-xs leading-relaxed",
              lastOutcome.error
                ? "border-destructive/30 bg-destructive/10 text-destructive"
                : lastOutcome.result?.status === "confirmada"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300"
                  : lastOutcome.result?.status === "revertida"
                    ? "border-destructive/30 bg-destructive/10 text-destructive"
                    : "border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-300",
            )}
          >
            {lastOutcome.error ? (
              <p>
                <span className="font-semibold">Fallo al reconciliar:</span>{" "}
                {lastOutcome.error}
              </p>
            ) : lastOutcome.result?.status === "confirmada" ? (
              <p>
                <span className="font-semibold">Reconciliación exitosa ({lastOutcome.result.number}):</span>{" "}
                la operación fue confirmada en la cadena
                {lastOutcome.result.blockNumber
                  ? ` (bloque ${lastOutcome.result.blockNumber})`
                  : null}
                .
              </p>
            ) : lastOutcome.result?.status === "revertida" ? (
              <p>
                <span className="font-semibold">Reconciliación completada ({lastOutcome.result.number}):</span>{" "}
                la transacción fue revertida en la cadena.
              </p>
            ) : lastOutcome.result?.status === "incierta" ? (
              <p>
                <span className="font-semibold">Reconciliación ejecutada ({lastOutcome.result.number}):</span>{" "}
                el estado de la operación continúa incierto en la cadena
                {lastOutcome.result.lastErrorCode
                  ? ` (${lastOutcome.result.lastErrorCode})`
                  : null}
                . La transacción no fue confirmada ni descartada definitivamente.
              </p>
            ) : (
              <p>
                <span className="font-semibold">Reconciliación ejecutada ({lastOutcome.result?.number}):</span>{" "}
                la operación permanece en estado {lastOutcome.result?.status}.
              </p>
            )}
          </div>
        ) : null}

        {data.oracleOperations.length === 0 ? (
          <Empty>No hay operaciones del oráculo pendientes de reconciliación.</Empty>
        ) : (
          <ul className="divide-y text-sm">
            {data.oracleOperations.map((op) => {
              const isReconcilingThis =
                reconcileMutation.isPending && reconcilingId === op.operationId
              return (
                <li
                  key={op.operationId}
                  className="space-y-2 py-3 first:pt-0 last:pb-0"
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{op.number}</span>
                      <span className="text-xs text-muted-foreground">
                        · {OPERATION_LABEL[op.operation] ?? op.operation}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          "rounded px-2 py-0.5 text-xs",
                          STATUS_BADGE[op.status] ?? "bg-muted text-muted-foreground",
                        )}
                      >
                        {STATUS_LABEL[op.status] ?? op.status}
                      </span>
                      {op.blockNumber != null ? (
                        <span className="text-xs text-muted-foreground">
                          bloque {op.blockNumber}
                        </span>
                      ) : null}
                    </div>
                  </div>

                  {op.transactionHash ? (
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="text-muted-foreground">Hash de transacción:</span>
                      <CopyableValue className="text-xs" value={op.transactionHash} />
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      Sin hash de transacción (no transmitida)
                    </p>
                  )}

                  {op.lastErrorCode ? (
                    <p className="text-xs text-destructive">
                      Último error: <span className="font-mono">{op.lastErrorCode}</span>
                    </p>
                  ) : null}

                  <div className="pt-1">
                    <button
                      type="button"
                      disabled={isReconcilingThis || !token}
                      onClick={() => handleReconcile(op.operationId)}
                      className="rounded-md border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-accent disabled:opacity-40"
                    >
                      {isReconcilingThis ? "Reconciliando…" : "Reconciliar"}
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Section>

      {/* 10 y 14 — Pie. */}
      <Section>
        <Note>
          La cola termina cuando la prueba se envió. Lo que sigue vive en la bandeja de
          revisión o, si el análisis fue favorable, en la lista de pendientes de
          liquidación de más arriba.
        </Note>
      </Section>
    </Screen>
  )
}
