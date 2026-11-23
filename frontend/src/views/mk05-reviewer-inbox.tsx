// MK-05 — Bandeja del revisor.
//
// Dibujar la bandeja dejó a la vista que RF-36 dice bastante menos de lo que la
// pantalla necesita: no fija con qué criterio se reparte el trabajo entre varios
// revisores, ni el orden de la lista, ni qué ocurre cuando el asignado no está.
// El orden de llegada es elaboración propia y por eso se declara sobre la lista
// en lugar de quedar implícito.
//
// El panel de configuración exhibe los umbrales que RF-32 y RF-51 mandan
// versionar, y ninguna pantalla del catálogo los edita.

import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import type { ReviewInbox } from "@/types"
import { apiGet } from "@/lib/api"
import { useStaffToken } from "@/lib/session"
import { CLASS_LABEL } from "@/lib/vocabulary"
import {
  Empty,
  Loading,
  Note,
  Screen,
  Section,
  Unmeasured,
  describeError,
  formatDateTime,
  formatMoney,
  formatThreshold,
} from "@/components/screen"

export function MK05ReviewerInbox() {
  const token = useStaffToken()

  const { data, error, isPending } = useQuery({
    queryKey: ["bandeja", token],
    queryFn: () => apiGet<ReviewInbox>("/revision/bandeja", token),
    enabled: Boolean(token),
    retry: false,
  })

  if (error || !token) {
    return (
      <Screen context="Aseguradora · Revisión de reclamos" session="" title="Bandeja de revisión">
        <Note>
          {token
            ? describeError(error, {
                401: "El token de la sesión no lo reconoce el servidor.",
                403: "La sesión no tiene el perfil de revisor.",
              })
            : "La sesión no tiene token: se elige en la barra lateral."}
        </Note>
      </Screen>
    )
  }
  if (isPending || !data) {
    return (
      <Screen context="Aseguradora · Revisión de reclamos" session="" title="Bandeja de revisión">
        <Loading>Leyendo la bandeja…</Loading>
      </Screen>
    )
  }

  return (
    <Screen
      context="Aseguradora · Revisión de reclamos"
      session={`Sesión: ${token.replace("revisor:", "")}`}
      title="Bandeja de revisión"
      subtitle={`${data.assignedTotal} reclamos asignados · ${data.unopened} sin abrir`}
    >
      {/* 9 — Con el modo de RF-35 activo la clasificación no emite Aprobable y
          la liquidación automática queda sin efecto: no hay Aprobable que
          liquidar. Los dos modos no se combinan. */}
      {data.humanConfirmationBanner ? (
        <Section>
          <Note>{data.humanConfirmationBanner}</Note>
        </Section>
      ) : null}

      {/* 2 — Configuración vigente. Exhibe los valores de hoy sobre una bandeja
          que puede tener casos clasificados con los de la semana pasada: ningún
          requerimiento ata un reclamo a la versión con la que se lo evaluó, y ese
          hueco está anotado en 00-criterios.md §4. */}
      <Section title="Configuración vigente" hint={`versión ${data.configuration.version}`}>
        <p className="text-sm">Política: {data.configuration.policyModeLabel}</p>
        <div className="mt-2 space-y-1 text-xs text-muted-foreground">
          <p>
            Umbral de derivación{" "}
            <Unmeasured
              value={data.configuration.derivationThreshold}
              format={formatThreshold}
            />{" "}
            · umbral de liquidación automática{" "}
            <Unmeasured
              value={data.configuration.autoSettlementThreshold}
              format={formatThreshold}
            />
          </p>
          <p>
            Límite de monto{" "}
            <Unmeasured value={data.configuration.amountLimit} format={formatMoney} />
          </p>
        </div>
      </Section>

      {/* 1, 3 a 7 — La lista. El marcador de sin abrir absorbe la notificación
          de RF-36, que no tiene artefacto propio en la matriz inversa. */}
      <Section title="Reclamos asignados" hint={`orden de ${data.ordering}`}>
        {/* Una bandeja sin filas es un desenlace normal y no un error: el
            prototipo recién desplegado no tiene nada derivado todavía. Sin este
            texto la sección se dibujaba vacía y se leía como una pantalla rota. */}
        {data.rows.length === 0 ? (
          <Empty>
            No hay reclamos asignados a esta sesión. Los casos aparecen cuando el
            sistema automático los deriva a revisión.
          </Empty>
        ) : null}
        <ul className="divide-y">
          {data.rows.map((row) => (
            <li key={row.number} className="py-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium">
                  {row.unopened ? <span className="mr-2 text-xs">●</span> : null}
                  {row.number}
                </span>
                <span className="text-xs text-muted-foreground">{row.statusId}</span>
              </div>
              <p className="mt-1 text-sm">{row.cause.text}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {row.predictedClass ? CLASS_LABEL[row.predictedClass] : null}
                {row.confidence != null ? ` ${Math.round(row.confidence * 100)}%` : null}
                {row.runnerUpClass
                  ? ` · segunda opción: ${CLASS_LABEL[row.runnerUpClass]}`
                  : null}
                {row.cause.amountWasNotTheCause && row.cause.id !== "ConfianzaBajoUmbral"
                  ? " · la confianza no fue la causa de la derivación"
                  : null}
                {row.accredited === false
                  ? " · sin acreditar: la comprobación criptográfica no se completó"
                  : null}
              </p>
              <div className="mt-1 flex flex-wrap items-baseline justify-between gap-2 text-xs text-muted-foreground">
                <span>
                  {row.declaredAmount != null
                    ? `monto declarado ${formatMoney(row.declaredAmount)}`
                    : null}
                  {row.arrivedAt ? ` · asignado ${formatDateTime(row.arrivedAt)}` : null}
                </span>
                <Link
                  to={`/resolucion/${row.number}`}
                  className="underline underline-offset-4"
                >
                  revisar ›
                </Link>
              </div>
            </li>
          ))}
        </ul>
      </Section>

      {/* 8 — Pie: dónde viven la traza completa y el vector de logits. */}
      <Section>
        <Note>
          Abrir un reclamo lleva a su vista de resolución: traza completa de transiciones,
          hashes y vector de logits.
        </Note>
      </Section>
    </Screen>
  )
}
