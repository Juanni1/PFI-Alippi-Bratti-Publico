// MK-03 — Estado del reclamo y línea de tiempo.
//
// Es la pantalla donde RNF-35 se defiende: ningún nombre interno de estado, y
// cada etiqueta con su causa. Las dos condiciones son distintas, y una etiqueta
// amable sin causa incumple igual que el identificador crudo. Las cadenas
// llegan ya redactadas del servidor; esta vista no traduce nada.

import { useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import type { ClaimantClaim } from "@/types"
import { apiGet } from "@/lib/api"
import { useAccessToken } from "@/lib/session"
import {
  CopyableField,
  Loading,
  Note,
  Screen,
  Section,
  StatusBadge,
  Unmeasured,
  VerificationSeal,
  describeError,
  formatDay,
  formatDateTime,
  formatMoney,
  formatPhotoCount,
  formatTime,
  parseInstant,
} from "@/components/screen"

// La liquidación automática deja tres segundos entre las dos últimas marcas, y
// la lámina de esa variante es la única que muestra segundos: sin ellos las dos
// escrituras parecerían simultáneas y el argumento de que son dos se perdería.
function needsSeconds(claim: ClaimantClaim): boolean {
  const marks = claim.timeline.map((entry) => parseInstant(entry.occurredAt).getTime())
  return marks.some((mark, i) => i > 0 && mark - marks[i - 1] < 60_000)
}

export function MK03ClaimStatus() {
  const { numero = "" } = useParams()
  const token = useAccessToken(numero)

  const { data, error, isPending } = useQuery({
    queryKey: ["reclamo", numero],
    queryFn: () => apiGet<ClaimantClaim>(`/reclamos/${numero}`, token),
    enabled: numero.length > 0,
    retry: false,
  })

  // Los tres desenlaces que no son la pantalla se dibujan y no se devuelven como
  // `null`: sin token, con un token que no abre este reclamo o con la API caída,
  // la vista quedaba en blanco y el lector no tenía qué corregir.
  if (!token || !numero) {
    return (
      <Screen context="Aseguradora" session="" title="Estado de tu reclamo">
        <Note>
          Para ver el estado hace falta el enlace que te dimos al registrar el reclamo:
          lleva el acceso adentro. El número solo, sin ese acceso, no abre nada.
        </Note>
      </Screen>
    )
  }
  if (error) {
    return (
      <Screen context="Aseguradora" session="" title="Estado de tu reclamo">
        <Note>
          {describeError(error, {
            401: "No pudimos abrir este reclamo con el enlace que usaste.",
            404: "No pudimos abrir este reclamo con el enlace que usaste.",
          })}
        </Note>
      </Screen>
    )
  }
  if (isPending || !data) {
    return (
      <Screen context="Aseguradora" session="" title="Estado de tu reclamo">
        <Loading>Buscando tu reclamo…</Loading>
      </Screen>
    )
  }

  const seconds = needsSeconds(data)
  const lastMark = data.timeline.at(-1)?.occurredAt

  return (
    <Screen
      context={`Aseguradora · Reclamo ${data.number}`}
      session="Póliza AB-3391"
      title="Estado de tu reclamo"
      subtitle={lastMark ? `Actualizado el ${formatDateTime(lastMark, seconds)}` : undefined}
    >
      {/* 1 — Bloque de estado actual: etiqueta del diccionario y su causa. */}
      <Section>
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-2xl font-semibold">{data.status.label}</p>
          <StatusBadge label={data.status.label} />
        </div>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
          {data.status.cause}
        </p>
      </Section>

      {/* 13 y 14 — Mientras no hay clasificación, la posición en la cola. La
          espera estimada no la acota ningún requerimiento: RNF-06 mide con la
          cola vacía, así que viaja sin medir y se dibuja como tal. */}
      {data.queue ? (
        <Section title="Tu turno en la cola">
          <p className="text-sm">
            Puesto {data.queue.position} · espera estimada{" "}
            {data.queue.estimatedWait ? (
              <Unmeasured value={data.queue.estimatedWait} />
            ) : (
              "sin estimar"
            )}
          </p>
          <div className="mt-2">
            <Note>Los reclamos se analizan de a uno, por orden de llegada.</Note>
          </div>
        </Section>
      ) : null}

      {/* 2, 3 y 4 — Línea de tiempo en orden de ocurrencia, con la marca
          temporal en el canal izquierdo. Las causas van en su forma breve:
          redactadas en presente se contradirían con la marca que tienen al lado. */}
      <Section title="Qué pasó con tu reclamo" hint="en orden cronológico">
        <ol className="space-y-5">
          {data.timeline.map((entry) => (
            <li key={entry.occurredAt} className="grid gap-2 sm:grid-cols-[5rem_1fr]">
              <div className="text-xs text-muted-foreground">
                <div>{formatDay(entry.occurredAt)}</div>
                <div>{formatTime(entry.occurredAt, seconds)}</div>
              </div>
              <div>
                <p className="text-sm font-medium">{entry.label}</p>
                <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
                  {entry.cause}
                </p>
              </div>
            </li>
          ))}
        </ol>
      </Section>

      {/* 5 — Datos del reclamo. */}
      <Section>
        <p className="text-sm text-muted-foreground">
          Siniestro del{" "}
          {data.claim.incidentDate ? formatDay(data.claim.incidentDate) : "sin fecha"} ·{" "}
          {formatPhotoCount(data.claim.photoCount)} · monto declarado{" "}
          {formatMoney(data.claim.declaredAmount)}
        </p>
      </Section>
      <Section title="Descripción del daño">
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
          {data.claim.damageDescription ?? "No se registró una descripción del daño"}
        </p>
      </Section>

      {/* 11 — Vía de apelación: enunciada en texto y sin control funcional. El
          punto resuelto 4 de adr-001 la modela como reclamo nuevo, con su caso de
          uso fuera del prototipo; omitirla contradiría al hallazgo 5 de la
          encuesta, y dibujarle un botón prometería lo que no existe. */}
      {data.appealNotice ? (
        <Section title="Si no estás de acuerdo con la resolución">
          <Note>{data.appealNotice}</Note>
        </Section>
      ) : null}

      {/* 6 — Salida a la explicación, con lo que va a encontrar ahí. Cuando el
          reclamo sigue sin clasificar se anuncia sin ofrecer acceso (MK-03c). */}
      <Section>
        {data.explanationAvailable ? (
          <Link
            to={`/explicacion/${data.number}#acceso=${token}`}
            className="text-sm font-medium underline underline-offset-4"
          >
            Cómo se analizó tu reclamo ›
          </Link>
        ) : (
          <p className="text-sm font-medium text-muted-foreground">
            Cómo se analizó tu reclamo
          </p>
        )}
        <div className="mt-1">
          <Note>daño detectado, confianza y reglas aplicadas</Note>
        </div>
      </Section>

      {/* 7 y 12 — Pie con el comprobante. Va en sección secundaria y no en la
          vista principal: quien no quiere verlo no debería tropezarse con él. */}
      <Section title="Comprobante y traza pública">
        <dl className="space-y-3">
          <CopyableField
            label="Identificador de la evidencia"
            value={data.receipt.evidenceId}
          />
          {data.receipt.transactionHashes.map((hash) => (
            <CopyableField key={hash} label="Transacción" value={hash} />
          ))}
          {data.resolution?.justificationHash ? (
            <CopyableField
              label="Hash de la justificación"
              value={data.resolution.justificationHash}
            />
          ) : null}
        </dl>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <Link
            to={`/traza/${data.number}`}
            className="text-sm underline underline-offset-4"
          >
            Ver traza pública ›
          </Link>
          {data.receipt.proofHash ? <VerificationSeal /> : null}
        </div>
      </Section>
    </Screen>
  )
}
