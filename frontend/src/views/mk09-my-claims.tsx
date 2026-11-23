// MK-09 — Mis reclamos.
//
// La pantalla que el catálogo no tenía. El hueco lo abrió el prototipo
// desplegado y no el análisis previo: con las ocho pantallas dibujadas, el
// asegurado llegaba a su reclamo por el enlace que le habíamos dado y nunca
// tenía dos a la vez, así que la lista no hacía falta.
//
// NO HAY RECURSO QUE LISTE LOS RECLAMOS DE UNA PERSONA, y no se inventa uno.
// RF-47 emite una capacidad por reclamo y ninguna identidad; un recurso que
// devolviera «los reclamos de quien pregunta» necesitaría saber quién pregunta,
// que es exactamente lo que adr-006 dejó fuera de alcance. La lista es la de los
// enlaces que este navegador guardó, y la pantalla lo declara en vez de
// aparentar que el servidor la conoce.

import { useQueries } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import type { ClaimantClaim } from "@/types"
import { ApiError, apiGet } from "@/lib/api"
import { useSession } from "@/lib/session"
import type { SavedClaim } from "@/lib/session"
import {
  Empty,
  Note,
  Screen,
  Section,
  StatusBadge,
  describeError,
  formatDateTime,
  formatMoney,
  parseInstant,
} from "@/components/screen"

/** El enlace al seguimiento lleva la capacidad adentro, como el de la constancia. */
function trackingPath(claim: SavedClaim): string {
  return claim.accessToken
    ? `/estado/${claim.number}#acceso=${claim.accessToken}`
    : `/estado/${claim.number}`
}

function Row({
  claim,
  data,
  error,
  onForget,
}: {
  claim: SavedClaim
  data?: ClaimantClaim
  error: unknown
  onForget: () => void
}) {
  // Un reclamo guardado cuyo enlace ya no abre no se esconde: se muestra con lo
  // que le pasó. Esconderlo dejaría al lector sin entender por qué desapareció
  // algo que él mismo había guardado.
  const roto = error != null
  const vencido = error instanceof ApiError && (error.status === 401 || error.status === 404)

  return (
    // Grilla y no `flex-wrap`: con una etiqueta larga la columna derecha se
    // envolvía debajo del texto y el número quedaba flotando en el medio de la
    // fila. Las dos pistas se reparten el ancho y la derecha no se achica.
    <li className="grid gap-x-6 gap-y-1 py-4 sm:grid-cols-[1fr_auto]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-medium">
            {roto ? (
              <span className="font-mono text-sm">{claim.number}</span>
            ) : (
              <Link to={trackingPath(claim)} className="underline underline-offset-4">
                {data?.status.label ?? claim.number}
              </Link>
            )}
          </p>
          {!roto && data?.status.label ? (
            <StatusBadge label={data.status.label} />
          ) : null}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
          {roto ? describeError(error) : data?.status.cause}
        </p>
      </div>
      <div className="text-xs text-muted-foreground sm:text-right">
        {data ? (
          <>
            <p className="font-mono">{claim.number}</p>
            <p>{formatMoney(data.claim.declaredAmount)}</p>
            {data.timeline.at(-1)?.occurredAt ? (
              <p>{formatDateTime(data.timeline.at(-1)!.occurredAt)}</p>
            ) : null}
          </>
        ) : null}
        {vencido ? (
          <button
            type="button"
            className="mt-1 underline underline-offset-4"
            onClick={onForget}
          >
            quitar de la lista
          </button>
        ) : null}
      </div>
    </li>
  )
}

export function MK09MyClaims() {
  const { savedClaims, removeClaim } = useSession()

  // Un pedido por reclamo, cada uno con SU token. No hay forma de pedirlos
  // juntos: cada capacidad abre uno solo, que es la propiedad que las hace
  // capacidades y no un identificador de usuario.
  const consultas = useQueries({
    queries: savedClaims.map((claim) => ({
      queryKey: ["reclamo", claim.number, claim.accessToken],
      queryFn: () =>
        apiGet<ClaimantClaim>(`/reclamos/${claim.number}`, claim.accessToken),
      enabled: Boolean(claim.accessToken),
      retry: false,
    })),
  })

  const pendientes = consultas.some((consulta) => consulta.isPending)

  // El más reciente arriba, por fecha de registro y no por orden de guardado:
  // el primer asiento del historial es la creación del reclamo, y el servidor
  // lo manda ordenado. Un enlace que se guarda hoy puede ser de un reclamo de
  // la semana pasada, y la lista lo ubica donde corresponde.
  //
  // Los pedidos se lanzan en el orden en que están guardados y el orden se
  // decide al dibujar: reordenar las consultas cambiaría sus claves y las
  // volvería a pedir. Mientras un reclamo no cargó, o cargó con error, no hay
  // fecha con la que compararlo y cae al final por orden de guardado inverso.
  const filas = savedClaims
    .map((claim, indice) => {
      const registrado = consultas[indice]?.data?.timeline[0]?.occurredAt
      return { claim, indice, registrado: registrado ? parseInstant(registrado).getTime() : null }
    })
    .sort((a, b) => {
      if (a.registrado !== null && b.registrado !== null) return b.registrado - a.registrado
      if (a.registrado !== null) return -1
      if (b.registrado !== null) return 1
      return b.indice - a.indice
    })

  return (
    <Screen
      context="Aseguradora"
      session="Póliza AB-3391"
      title="Mis reclamos"
      subtitle={
        savedClaims.length > 0
          ? `${savedClaims.length} reclamos en este dispositivo`
          : undefined
      }
      action={
        // La carga es la acción de esta pantalla y no un destino de la barra:
        // el asegurado abre su lista y desde ahí carga, en vez de elegir entre
        // dos entradas que la barra le ofrecía al mismo nivel.
        <Link
          to="/"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
        >
          Nuevo reclamo
        </Link>
      }
    >
      <Section>
        {savedClaims.length === 0 ? (
          <Empty>
            Todavía no tenés reclamos guardados. Cargá uno nuevo o abrí
            el enlace que guardaste al registrarte.
          </Empty>
        ) : (
          <ul className="divide-y">
            {filas.map(({ claim, indice }) => (
              <Row
                key={claim.number}
                claim={claim}
                data={consultas[indice]?.data}
                error={consultas[indice]?.error}
                onForget={() => removeClaim(claim.number)}
              />
            ))}
          </ul>
        )}
        {pendientes ? (
          <div className="mt-3">
            <Note>Actualizando estado de los reclamos…</Note>
          </div>
        ) : null}
      </Section>
    </Screen>
  )
}
