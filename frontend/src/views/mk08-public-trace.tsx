// MK-08 — Traza pública.
//
// Única vista sin acreditación de titularidad (RF-44). La lista de campos que
// expone es cerrada: no muestra monto declarado, imagen, clase predicha,
// confianza, identidad de quien resolvió, texto de la justificación ni
// descripción del daño. Sí muestra el vector de características, que es la
// única fila de la matriz de visibilidad que no se le puede negar a nadie: viaja
// en la calldata de la transacción de verificación y ya es público para
// cualquiera que sepa leerla.

import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useParams } from "react-router-dom"
import type { PublicTrace, Transition } from "@/types"
import { apiGet } from "@/lib/api"
import { CAUSE_LABEL, ENTRY_LABEL } from "@/lib/vocabulary"
import {
  CopyableValue,
  Empty,
  Loading,
  Note,
  Screen,
  Section,
  describeError,
  formatDateTime,
} from "@/components/screen"

function TransitionRow({ transition }: { transition: Transition }) {
  // La tercera pista lleva un hash de sesenta y seis caracteres. Sin
  // `minmax(0,…)` la pista toma como mínimo el ancho de su contenido, y sin
  // `min-w-0` con `break-all` el hash es una sola palabra que no corta: la
  // columna se desbordaba por fuera de la caja en vez de envolver.
  return (
    <li className="grid gap-2 border-t py-4 text-sm sm:grid-cols-[11rem_1fr_minmax(0,14rem)]">
      <div className="text-xs text-muted-foreground">
        <div>{formatDateTime(transition.occurredAt, true)}</div>
        {transition.blockNumber ? (
          <div>bloque {transition.blockNumber.toLocaleString("es-AR")}</div>
        ) : null}
      </div>
      <div>
        <div className="font-medium">
          {transition.from ?? "(inicio)"} <span className="text-muted-foreground">→</span>{" "}
          {transition.to}
        </div>
        <div className="text-xs text-muted-foreground">
          {transition.cause ? CAUSE_LABEL[transition.cause] : ENTRY_LABEL}
        </div>
      </div>
      <div className="min-w-0 text-xs">
        {transition.transactionHash ? (
          <CopyableValue value={transition.transactionHash} />
        ) : (
          <span className="text-muted-foreground">sin hash de transacción</span>
        )}
        <div className="mt-1 text-muted-foreground">
          {transition.proofHash ? (
            <>
              <span className="block">hash de la prueba</span>
              <CopyableValue value={transition.proofHash} />
            </>
          ) : transition.from === null ? (
            "sin hash de prueba: el reclamo se registra antes de la inferencia"
          ) : (
            "sin hash de prueba: la transición no la requiere"
          )}
        </div>
      </div>
    </li>
  )
}

function TraceDetail({ trace }: { trace: PublicTrace }) {
  const circuito = trace.artifacts.proof.circuitParameters
  const replicacion = trace.artifacts.replication
  const entradasReplicacion: Array<[string, string | null | undefined]> = [
    ["hash del extractor", replicacion.extractorOnnxHash],
    ["ONNX del extractor", replicacion.extractorOnnxUrl],
    ["preprocesamiento", replicacion.preprocessingSpecUrl],
    ["motor de inferencia", replicacion.runtimeVersion],
  ]
  const replicacionPublicada = entradasReplicacion.filter(
    (entrada): entrada is [string, string] => Boolean(entrada[1]),
  )
  // El generador simulado se autodeclara con esta marca. Que exista no alcanza:
  // si la pantalla no la lee, la traza simulada se ve igual que la acreditada.
  const simulada = circuito?.simulated === true

  return (
    <>
      {/* 2 — Estado vigente. El que se muestra es el que define el contrato: la
          base es réplica y no fuente (adr-004). */}
      <Section
        title="Estado vigente"
        hint="definido en el contrato inteligente"
      >
        <p className="text-xl font-semibold">{trace.statusId}</p>
        <Note>
          {trace.statusIsTerminal
            ? "terminal: sin transiciones de salida"
            : "intermedio: la máquina admite salida"}
        </Note>
      </Section>

      {/* 3 y 4 — Secuencia de transiciones y procedencia de la reconstrucción. */}
      <Section
        title="Secuencia de transiciones"
        hint="los cinco campos que el contrato emite en cada evento"
      >
        <ul>
          {trace.transitions.map((transition) => (
            <TransitionRow
              key={`${transition.occurredAt}-${transition.to}`}
              transition={transition}
            />
          ))}
        </ul>
        <div className="mt-4">
          <Note>
            Reconstruida leyendo los eventos del contrato desde el bloque de creación. No
            interviene la base de la aseguradora.
          </Note>
        </div>
      </Section>

      {/* 5 — Evidencia y sus dos comprobaciones. */}
      <Section title="Evidencia" hint="identificador público sin exponer el archivo">
        <CopyableValue className="text-sm" value={trace.evidence.evidenceId} />
        <div className="mt-3">
          {trace.evidence.retrievable ? (
            <Note>
              Recuperar por el identificador y comprobar que el contenido recuperado
              corresponde al identificador registrado. Reejecutar sobre ese archivo el
              extractor publicado: el vector resultante debe coincidir byte a byte con la
              entrada pública de la prueba.
            </Note>
          ) : (
            <Note>
              El identificador sigue registrado y no hay contenido detrás. La traza sigue
              siendo válida, pero no puede comprobarse la relación entre la imagen y el
              vector numérico.
            </Note>
          )}
        </div>
      </Section>

      {/* 6 — Los dos actos de comprobación, en dos columnas. Verificar la prueba
          acredita que ese vector produjo esos logits; rehacer el vector acredita
          que ese vector proviene de esa fotografía. Ninguno de los dos por
          separado alcanza el píxel. */}
      <Section
        title="Comprobar por cuenta propia"
        hint="dos comprobaciones independientes sin intervención de la aseguradora"
      >
        {simulada ? (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 p-3">
            <p className="text-sm font-medium">Esta prueba no es criptográfica.</p>
            <p className="mt-1 text-xs text-muted-foreground">
              La generó el sustituto de desarrollo, que produce un artefacto con la forma
              correcta y sin criptografía, y lo declara en los parámetros del circuito.
              Nada de lo que sigue se puede comprobar sobre esta traza: no hay prueba que
              verificar. El resto de la traza (transiciones, evidencia y vector numérico)
              sí coincide con lo registrado por el sistema.
            </p>
          </div>
        ) : null}
        <div className="grid gap-6 sm:grid-cols-2">
          <div>
            <h3 className="text-sm font-medium">Verificar la prueba</h3>
            <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
              <li>la prueba y sus entradas públicas</li>
              <li>los parámetros del circuito y su hash</li>
              <li>la clave de verificación</li>
            </ul>
            {circuito ? (
              <dl className="mt-3 space-y-0.5 text-xs">
                {Object.entries(circuito)
                  .filter(([clave]) => clave !== "simulated")
                  .map(([clave, valor]) => (
                    <div key={clave} className="flex min-w-0 gap-2">
                      <dt className="shrink-0 text-muted-foreground">{clave}</dt>
                      <dd className="min-w-0">
                        <CopyableValue value={String(valor)} />
                      </dd>
                    </div>
                  ))}
              </dl>
            ) : null}
          </div>
          <div>
            <h3 className="text-sm font-medium">Rehacer el vector desde la evidencia</h3>
            <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
              <li>
                el ONNX del extractor y su hash
                {replicacion.extractorOnnxUrl ? null : (
                  <span> (sin publicar todavía)</span>
                )}
              </li>
              <li>el preprocesamiento que produce el tensor</li>
              <li>la versión fijada del motor de inferencia</li>
            </ul>
            {replicacionPublicada.length > 0 ? (
              <dl className="mt-3 space-y-0.5 text-xs">
                {replicacionPublicada.map(([clave, valor]) => (
                  <div key={clave} className="flex min-w-0 gap-2">
                    <dt className="shrink-0 text-muted-foreground">{clave}</dt>
                    <dd className="min-w-0">
                      <CopyableValue
                        value={valor}
                        href={
                          valor.startsWith("https://") || valor.startsWith("http://")
                            ? valor
                            : undefined
                        }
                      />
                    </dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
        </div>
        <div className="mt-4">
          <Note>
            La prueba cubre el tramo que va del vector a la clasificación, y una generada
            con otros parámetros no verifica contra esta clave. El tramo anterior, de la
            imagen al vector, no se prueba: se replica, y por eso el extractor se publica
            entero. Los parámetros del modelo son públicos. El vector numérico también es
            público y no permite reconstruir la imagen.
          </Note>
        </div>
        <details className="mt-4">
          <summary className="cursor-pointer text-xs text-muted-foreground">
            Vector de características publicado ({trace.featureVector.length} valores)
          </summary>
          <div className="mt-2 max-h-40 overflow-y-auto text-[10px] leading-relaxed text-muted-foreground">
            <CopyableValue value={trace.featureVector.join(", ")} />
          </div>
        </details>
      </Section>

      {/* 7 y 8 — Lo que la vista no expone, y el alcance real de esa omisión. */}
      <Section title="Datos protegidos no expuestos">
        <Note>
          Sin {trace.notExposed.join(", ")}. De la justificación se publica el hash y no el
          texto. La red no contiene datos personales.
        </Note>
        <div className="mt-3">
          <Note>{trace.imageDisclosureNotice}</Note>
        </div>
      </Section>
    </>
  )
}

export function MK08PublicTrace() {
  const { numero } = useParams()
  const [query, setQuery] = useState(numero ?? "")
  const [submitted, setSubmitted] = useState(numero ?? "")

  const { data, error, isFetching } = useQuery({
    queryKey: ["traza", submitted],
    queryFn: () => apiGet<PublicTrace>(`/publico/reclamos/${submitted}/traza`),
    enabled: submitted.length > 0,
    retry: false,
  })

  return (
    <Screen
      context="Traza pública · Auditoría de reclamos"
      session="Consulta pública · sin sesión"
      title={submitted ? `Traza del reclamo ${submitted}` : "Traza pública"}
      subtitle={data?.provenance}
    >
      {/* 1 — Consulta por identificador. La vista se abre con el número y sin
          acreditar titularidad: es requerimiento y no descuido. */}
      <Section>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(query.trim())
          }}
        >
          <label className="flex-1 text-sm">
            <span className="mb-1 block font-medium">Identificador del reclamo</span>
            <input
              className="w-full rounded-md border px-3 py-2 font-mono text-sm"
              placeholder="SN-4103"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <button
            type="submit"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            Consultar
          </button>
        </form>
        <div className="mt-3">
          <Note>
            Sin acreditación de titularidad: cualquiera consulta cualquier reclamo.
          </Note>
        </div>
      </Section>

      {isFetching ? <Loading>Leyendo los eventos del contrato…</Loading> : null}
      {error ? (
        <Section>
          <Note>{describeError(error)}</Note>
        </Section>
      ) : null}
      {!submitted && !isFetching ? (
        <Section>
          <Empty>
            Escribí el identificador de un reclamo para reconstruir su traza desde los
            eventos del contrato.
          </Empty>
        </Section>
      ) : null}
      {data ? <TraceDetail trace={data} /> : null}
    </Screen>
  )
}
