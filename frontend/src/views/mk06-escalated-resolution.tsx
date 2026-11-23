// MK-06 — Resolución de un reclamo derivado.
//
// Dos perfiles operan sobre la misma pantalla y reciben cuerpos distintos: el
// vector de logits es del revisor y no del operador. El recorte lo hace el
// servidor por campo (RF-46); acá simplemente no llega.
//
// Desde InconsistenciaEvidencia el contrato habilita una sola salida, la
// desfavorable. Un revisor a quien la evidencia convenza de lo contrario no
// tiene qué apretar, y la restricción se escribe al lado del control en vez de
// disimularse: la asimetría es consecuencia de la matriz de transiciones y solo
// se cierra revisando adr-001.

import { useState } from "react"
import { EvidenceGallery } from "@/components/evidence-gallery"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import type { OperatorClaim, ReviewerClaim } from "@/types"
import { apiGet, apiPost } from "@/lib/api"
import { useStaffToken } from "@/lib/session"
import { CAUSE_LABEL, CLASSES, CLASS_LABEL, ENTRY_LABEL, softmax } from "@/lib/vocabulary"
import {
  CopyableValue,
  Loading,
  Note,
  Screen,
  Section,
  describeError,
  formatDateTime,
  formatMoney,
} from "@/components/screen"

export function MK06EscalatedResolution() {
  const { numero = "" } = useParams()
  const token = useStaffToken()
  const isOperator = Boolean(token?.startsWith("operador"))
  const [justification, setJustification] = useState("")

  const { data, error, isPending, refetch } = useQuery({
    queryKey: ["reclamo-interno", numero, token],
    queryFn: () => apiGet<ReviewerClaim | OperatorClaim>(`/reclamos/${numero}`, token),
    enabled: Boolean(token) && numero.length > 0,
    retry: false,
  })

  const resolve = useMutation({
    mutationFn: (favorable: boolean) =>
      apiPost(`/reclamos/${numero}/resolucion`, { favorable, justification }, token),
    onSuccess: () => refetch(),
  })

  const settle = useMutation({
    mutationFn: () => apiPost(`/reclamos/${numero}/liquidacion`, {}, token),
    onSuccess: () => refetch(),
  })

  if (error || !token || !numero) {
    return (
      <Screen context="Aseguradora" session="" title="Resolución del reclamo">
        <Note>
          {!token
            ? "La sesión no tiene token: se elige en la barra lateral."
            : !numero
              ? "Falta el número del reclamo: se abre desde la bandeja o se fija en la barra lateral."
              : describeError(error, {
                  403: "La sesión no tiene perfil para operar sobre este reclamo.",
                })}
        </Note>
      </Screen>
    )
  }
  if (isPending || !data) {
    return (
      <Screen context="Aseguradora" session="" title="Resolución del reclamo">
        <Loading>Abriendo el reclamo…</Loading>
      </Screen>
    )
  }

  // El perfil sale del token y no de olfatear la respuesta. Deducirlo por la
  // presencia de `logits` obligaba a que `inference` estuviera siempre, y no lo
  // está: un reclamo que todavía no se clasificó no tiene inferencia, y el
  // contrato la declara obligatoria sin contemplar ese estado.
  const reviewer = isOperator ? null : (data as ReviewerClaim)
  const operator = isOperator ? (data as OperatorClaim) : null
  const inference = data.inference ?? null
  const onlyUnfavorable =
    reviewer?.availableResolutions?.length === 1 &&
    reviewer.availableResolutions[0] === "desfavorable"

  return (
    <Screen
      context={`Aseguradora · Reclamo ${data.number} · ${data.statusId}`}
      session={`Sesión: ${token.replace(/^(revisor|operador)-/, "")}`}
      title={isOperator
        ? operator?.settleable ? "Liquidación del reclamo" : "Detalle del reclamo"
        : "Resolución del reclamo"}
    >
      {/* 2 — Evidencia cargada, con su identificador de contenido. */}
      <Section title="Evidencia">
        <CopyableValue className="text-xs" value={data.evidence.evidenceId} />
        <EvidenceGallery files={data.evidence.files} token={token} />
      </Section>
      <Section title="Relato declarado por el reclamante">
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
          {data.damageDescription ?? "No se registró una descripción del daño"}
        </p>
      </Section>
      <Section title="Identidad de quien carga">
        <p className="text-sm">{data.registeredBy ?? "No consta en este reclamo"}</p>
      </Section>

      {/* 3, 4, 5 y 19 — Salida del modelo. El vector completo sobre las seis
          clases solo aparece para el revisor: cuando la confianza cae bajo el
          umbral, la segunda clase más probable es la información que sostiene su
          decisión. Al operador no le llega. */}
      <Section title="Salida del modelo">
        {!inference ? (
          <Note>
            El servidor no devolvió una salida del modelo para esta consulta.
          </Note>
        ) : (
        <p className="text-sm">
          Clase predicha: <strong>{CLASS_LABEL[inference.predictedClass]}</strong> ·
          confianza {Math.round(inference.confidence * 100)}%
        </p>
        )}
        {reviewer && reviewer.inference ? (
          <>
            <ol className="mt-3 space-y-1 text-xs">
              {CLASSES.map((damageClass, index) => ({
                damageClass,
                share: softmax(reviewer.inference.logits)[index],
              }))
                .sort((a, b) => b.share - a.share)
                .map(({ damageClass, share }) => (
                  <li key={damageClass} className="flex justify-between gap-4">
                    <span>{CLASS_LABEL[damageClass]}</span>
                    <span className="font-mono">{(share * 100).toFixed(1)}%</span>
                  </li>
                ))}
            </ol>
            <div className="mt-3">
              <Note>
                El circuito emite los valores crudos. El porcentaje se calcula fuera de la
                prueba y no forma parte de lo que la prueba acredita.
              </Note>
            </div>
          </>
        ) : (
          <div className="mt-2">
            <Note>{operator
              ? "El detalle por clase no está disponible para el perfil de operador."
              : "El detalle por clase no está disponible en esta consulta."}</Note>
          </div>
        )}
      </Section>

      {/* 6 y 20 — Causa de derivación, o las condiciones del estado Aprobable. */}
      {reviewer ? (
        <Section title="Causa de la derivación">
          <p className="text-sm">{reviewer.cause?.text ?? data.status.cause}</p>
          {reviewer.cause?.amountWasNotTheCause ? (
            <div className="mt-2">
              <Note>
                El monto declarado, {formatMoney(reviewer.declaredAmount)}, quedó dentro del
                límite: no fue esta la causa.
              </Note>
            </div>
          ) : null}
        </Section>
      ) : operator?.settleable ? (
        <Section title="Por qué el cierre no fue automático">
          <Note>
            La confianza quedó entre los dos umbrales: alcanzó el de derivación y no el de
            liquidación automática, así que el reclamo espera una liquidación registrada.
          </Note>
        </Section>
      ) : (
        <Section title="Estado del reclamo">
          <p className="text-sm">{data.status.cause}</p>
          <div className="mt-2">
            <Note>El estado vigente no habilita la liquidación por operador.</Note>
          </div>
        </Section>
      )}

      {/* 7 — Traza del reclamo, con su causa y su hash por transición. */}
      {data.trace ? (
        <Section title="Traza del reclamo">
          <ul className="space-y-2 text-xs">
            {data.trace.map((transition) => (
              <li key={`${transition.occurredAt}-${transition.to}`}>
                <span className="text-muted-foreground">
                  {formatDateTime(transition.occurredAt)}
                </span>{" "}
                {transition.from ?? "(inicio)"} → {transition.to} ·{" "}
                {transition.cause ? CAUSE_LABEL[transition.cause] : ENTRY_LABEL}
                {transition.transactionHash ? (
                  <CopyableValue className="mt-0.5" value={transition.transactionHash} />
                ) : null}
                {transition.proofHash ? (
                  <CopyableValue className="mt-0.5" value={transition.proofHash} />
                ) : null}
              </li>
            ))}
          </ul>
          <div className="mt-3">
            <Link
              to={`/traza/${data.number}`}
              className="text-sm underline underline-offset-4"
            >
              Ver traza pública ›
            </Link>
          </div>
        </Section>
      ) : null}

      {/* 8, 9, 15 y 21 — Las salidas. Del lado del operador es un solo control y
          ningún campo de texto: CU-06 no tiene equivalente de RF-39, de modo que
          esa transición no registra autor. La separación entre los dos estados
          existe justamente para poder auditar eso. */}
      {operator ? (
        <Section title="Liquidar">
          <button
            type="button"
            disabled={!operator.settleable || settle.isPending}
            onClick={() => settle.mutate()}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
          >
            Registrar liquidación
          </button>
          <div className="mt-2">
            <Note>
              Salir sin liquidar deja el reclamo en el estado actual. La liquidación queda
              asentada en la red bajo la cuenta de la aseguradora.
            </Note>
          </div>
        </Section>
      ) : (
        <Section title="Registrar la resolución">
          <label className="block text-sm">
            <span className="font-medium">Justificación</span>
            <textarea
              className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
              rows={4}
              value={justification}
              onChange={(event) => setJustification(event.target.value)}
            />
          </label>
          <div className="mt-2">
            <Note>
              Obligatoria. Se registran tu identidad, la fecha y el texto. A la
              red pública va solo el hash.
            </Note>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={!reviewer?.availableResolutions?.includes("favorable") || !justification.trim() || resolve.isPending}
              onClick={() => resolve.mutate(true)}
              className="rounded-md border px-4 py-2 text-sm font-medium disabled:opacity-40"
            >
              Aprobar reclamo
            </button>
            <button
              type="button"
              disabled={!reviewer?.availableResolutions?.includes("desfavorable") || !justification.trim() || resolve.isPending}
              onClick={() => resolve.mutate(false)}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
            >
              Rechazar reclamo
            </button>
          </div>
          {onlyUnfavorable ? (
            <div className="mt-2">
              <Note>
                Desde este estado la única salida habilitada es el rechazo.
                Si el asegurado no está de acuerdo, puede iniciar un reclamo nuevo.
              </Note>
            </div>
          ) : null}
        </Section>
      )}
    </Screen>
  )
}
