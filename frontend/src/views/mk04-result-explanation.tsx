// MK-04 — Explicación del resultado.
//
// RF-42 entrega lo que sí se puede entregar, que es la traza legible del
// criterio aplicado; RF-43 impide que el sistema se atribuya una capacidad que
// no tiene. Una prueba de conocimiento cero acredita que un cómputo se ejecutó
// de forma correcta y guarda silencio sobre por qué el modelo llegó a esa
// salida, y con la arquitectura adoptada cubre además solo la mitad final del
// procesamiento: una pantalla que hablara de la prueba sin nombrar ese recorte
// le atribuiría al contrato una comprobación que el contrato no hizo.

import { useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import type { ClaimExplanation } from "@/types"
import { apiGet } from "@/lib/api"
import { useAccessToken } from "@/lib/session"
import { CLASS_LABEL } from "@/lib/vocabulary"
import {
  Loading,
  Note,
  Screen,
  Section,
  StatusBadge,
  Unmeasured,
  VerificationSeal,
  describeError,
  formatDateTime,
  formatMoney,
  formatPercent,
} from "@/components/screen"

// Los pasos numerados son CUATRO, que es lo que promete la bajada: evidencia,
// modelo, regla y resultado. El bloque de la prueba no es un paso del recorrido
// sino lo que declara su alcance, y por eso va sin número y anidado en el paso
// del modelo, como en la lámina.
function Step({
  index,
  title,
  children,
}: {
  index: number
  title: string
  children: React.ReactNode
}) {
  return (
    <Section>
      <div className="flex gap-3">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs">
          {index}
        </span>
        <div className="flex-1">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="mt-2 space-y-2">{children}</div>
        </div>
      </div>
    </Section>
  )
}

export function MK04ResultExplanation() {
  const { numero = "" } = useParams()
  const token = useAccessToken(numero)

  const { data, error, isPending } = useQuery({
    queryKey: ["explicacion", numero],
    queryFn: () => apiGet<ClaimExplanation>(`/reclamos/${numero}/explicacion`, token),
    enabled: numero.length > 0,
    retry: false,
  })

  if (!token || !numero) {
    return (
      <Screen context="Aseguradora" session="" title="Cómo se analizó tu reclamo">
        <Note>
          Para ver la explicación hace falta el enlace que te dimos al registrar el
          reclamo: lleva el acceso adentro.
        </Note>
      </Screen>
    )
  }
  if (error) {
    return (
      <Screen context="Aseguradora" session="" title="Cómo se analizó tu reclamo">
        {/* Excepción 2a de CU-02: sin clasificación no hay explicación que dar.
            El 409 es ese caso y los demás códigos no son ese caso; darles a todos
            el mismo texto le decía al reclamante que su reclamo estaba en cola
            cuando lo que había fallado era el acceso. */}
        <Note>
          {describeError(error, {
            409: "Todavía no analizamos tu reclamo. Cuando el análisis termine vas a poder ver acá la clase de daño, la confianza y la regla que se aplicó.",
          })}
        </Note>
      </Screen>
    )
  }
  if (isPending || !data) {
    return (
      <Screen context="Aseguradora" session="" title="Cómo se analizó tu reclamo">
        <Loading>Buscando el análisis de tu reclamo…</Loading>
      </Screen>
    )
  }

  const { evidence, model, proof, rule, outcome } = data

  return (
    <Screen
      context={`Aseguradora · Reclamo ${numero}`}
      session="Póliza AB-3391"
      title="Cómo se analizó tu reclamo"
      subtitle="Los 4 pasos de la evaluación automática."
    >
      {/* 2 — Eslabón de evidencia. El aviso distingue la publicación de fotos
          en IPFS del resumen numérico que utiliza el análisis. */}
      <Step
        index={1}
        title={
          evidence.fileCount === 1
            ? "1. Recepción de la foto"
            : `1. Recepción de las ${evidence.fileCount} fotos`
        }
      >
        <Note>{formatDateTime(evidence.submittedAt)}</Note>
        <Note>{evidence.publishedFeatureVector.notice}</Note>
      </Step>

      {/* 3 y 8 — Eslabón del modelo. En la variante de verificación fallida la
          clase se muestra rotulada como sin acreditar (RNF-17). */}
      <Step index={2} title="2. Análisis del modelo">
        <p className="text-sm">
          Daño detectado: <strong>{CLASS_LABEL[model.predictedClass]}</strong>
        </p>
        <p className="text-sm">
          Nivel de confianza: <strong>{Math.round(model.confidence * 100)}%</strong>
        </p>
        {model.accredited === false ? (
          <Note>
            Sin acreditar: la comprobación criptográfica no se completó.
          </Note>
        ) : null}

        {/* 4 y 9 — Qué acredita la prueba, cómo se comprueba el tramo que no
            cubre y qué no acredita. La garantía es escalonada y hay que
            declararlo: una pantalla que hablara de la prueba sin nombrar el
            recorte le atribuiría al contrato una comprobación que no hizo. */}
        <div className="mt-4 rounded-md border bg-muted/40 p-4">
          <p className="text-sm font-medium">Qué acredita la comprobación criptográfica</p>
          <Note>{proof.provenSegment}</Note>
          <p className="pt-3 text-sm font-medium">
            Cómo se comprueba que ese resumen salió de tus fotos
          </p>
          <Note>{proof.replicatedSegment}</Note>
          <p className="pt-3 text-sm font-medium">Qué no acredita</p>
          <Note>{proof.disclaimer}</Note>
          {proof.verificationFailed ? (
            <Note>
              Falló la comprobación interna y el intento quedó registrado para revisión manual, sin descartar el caso.
            </Note>
          ) : null}
        </div>
      </Step>

      {/* 5, 15 y 16 — Eslabón de la regla: confianza contra el mínimo y monto
          contra el límite. Los dos umbrales viajan sin medir y se dibujan como
          tales; elegirlos antes de medir sería un número plausible. */}
      <Step index={3} title="3. Aplicación de reglas">
        {rule.confidenceAgainstThreshold ? (
          <Note>
            {rule.confidenceAgainstThreshold.satisfied === false ||
            rule.confidenceAgainstThreshold.satisfied === null
              ? "La confianza quedó por debajo del mínimo para resolver solo"
              : "La confianza superó el mínimo para resolver solo"}{" "}
            (mínimo:{" "}
            <Unmeasured
              value={rule.confidenceAgainstThreshold.threshold}
              format={formatPercent}
            />
            ).
          </Note>
        ) : null}
        {rule.amountAgainstLimit ? (
          <Note>
            El monto que declaraste, {formatMoney(rule.amountAgainstLimit.observed)}, quedó{" "}
            {rule.amountAgainstLimit.satisfied ? "dentro" : "por encima"} del límite
            automático (límite:{" "}
            <Unmeasured value={rule.amountAgainstLimit.threshold} format={formatMoney} />
            ).
          </Note>
        ) : null}
        {rule.autoSettlementApplied ? (
          <Note>
            Superó el umbral de certeza para liquidación inmediata sin intervención humana.
          </Note>
        ) : null}
      </Step>

      {/* 6 y 17 — Eslabón del resultado, con etiqueta y causa del diccionario. */}
      <Step index={4} title="4. Resultado">
        <div className="flex flex-wrap items-center gap-2.5">
          <p className="text-lg font-semibold">{outcome.label}</p>
          <StatusBadge label={outcome.label} />
        </div>
        <Note>{outcome.cause}</Note>
        {model.accredited !== false && !proof.verificationFailed ? (
          <div className="pt-1">
            <VerificationSeal />
          </div>
        ) : null}
      </Step>

      {/* 7 — Pie. */}
      <Section>
        <div className="flex flex-wrap gap-6 text-sm">
          <Link
            to={`/estado/${numero}#acceso=${token}`}
            className="underline underline-offset-4"
          >
            Línea de tiempo del reclamo ›
          </Link>
          <Link to={`/traza/${numero}`} className="underline underline-offset-4">
            Comprobante y traza pública ›
          </Link>
        </div>
      </Section>
    </Screen>
  )
}
