// MK-02 — Constancia de registro.
//
// El número identifica y el enlace abre. La distinción la fija adr-006: un
// identificador que por sí solo abre un recurso es una capacidad, y este circula
// por diseño porque la vista pública de auditoría lo toma como entrada. RF-47
// reemplazó el número pelado por un token emitido para ese reclamo.
//
// El comprobante va en sección secundaria para el reclamante. Si registra un
// operador, la constancia conserva la capacidad de seguimiento para entregarla
// al reclamante y ofrece un enlace al detalle interno; no expone allí la
// identidad de la sesión.

import { Link, useLocation, useParams } from "react-router-dom"
import type { RegistrationReceipt } from "@/types"
import { useSession } from "@/lib/session"
import {
  CopyableField,
  Note,
  Screen,
  Section,
  formatDateTime,
  formatMoney,
  formatPhotoCount,
} from "@/components/screen"

export function MK02RegistrationReceipt() {
  const { numero } = useParams()
  const location = useLocation()
  const { profile } = useSession()
  const isOperator = profile === "operador"

  // La constancia llega como respuesta del registro y solo se muestra en la
  // navegación inmediata; no hay un recurso que vuelva a servirla.
  const data = location.state as RegistrationReceipt | null

  if (!data) {
    return (
      <Screen
        context={`Aseguradora${numero ? ` · Reclamo ${numero}` : ""}`}
        session={isOperator ? "Sesión de operador" : "Póliza AB-3391"}
        title={isOperator ? "Constancia del registro" : "Recibimos tu reclamo"}
      >
        <Section>
          <Note>
            {isOperator
              ? "La constancia completa se muestra al terminar el registro. El detalle interno conserva el relato y quién lo cargó."
              : "La constancia se muestra una sola vez, al terminar la carga. Para volver al reclamo usá el enlace que guardaste."}
          </Note>
          {numero ? (
            <div className="mt-3">
              <Link
                to={isOperator ? `/resolucion/${numero}` : `/estado/${numero}`}
                className="text-sm underline underline-offset-4"
              >
                {isOperator ? "Abrir detalle interno ›" : "Ver estado del reclamo ›"}
              </Link>
            </div>
          ) : null}
        </Section>
      </Screen>
    )
  }

  const trackingUrl = `/estado/${data.number}#acceso=${data.accessToken}`

  return (
    <Screen
      context={`Aseguradora · Reclamo ${data.number}`}
      session={isOperator ? "Sesión de operador" : "Póliza AB-3391"}
      title={isOperator ? "Reclamo registrado" : "Recibimos tu reclamo"}
      subtitle={isOperator ? "Registro en nombre del reclamante" : "Paso 2 de 2"}
    >
      <Section>
        <p className="text-xs text-muted-foreground">
          {isOperator ? "Número del reclamo" : "Tu número de seguimiento"}
        </p>
        <p className="text-3xl font-semibold tracking-tight">{data.number}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Recibido el {formatDateTime(data.registeredAt)}
        </p>
        <div className="mt-4 border-t pt-4">
          <p className="text-sm font-medium">{data.status.label}</p>
          <Note>{data.status.cause}</Note>
        </div>
      </Section>

      {isOperator ? (
        <Section title="Acceso para el reclamante">
          <Note>
            La identidad de la sesión interna no se incluye en esta constancia. Entregá al
            reclamante el enlace de seguimiento para que pueda consultar el reclamo.
          </Note>
          <dl className="mt-3">
            <CopyableField
              label="Enlace de seguimiento para entregar al reclamante"
              value={trackingUrl}
            />
          </dl>
          <div className="mt-3">
            <Link
              to={`/resolucion/${data.number}`}
              className="text-sm font-medium underline underline-offset-4"
            >
              Abrir detalle interno ›
            </Link>
          </div>
        </Section>
      ) : (
        <Section>
          <Link
            to={trackingUrl}
            className="text-sm font-medium underline underline-offset-4"
          >
            Ver estado del reclamo ›
          </Link>
          <div className="mt-2">
            <Note>
              Guardá este enlace. Es tu acceso directo para ver cómo avanza el caso.
            </Note>
          </div>
        </Section>
      )}

      <Section
        title={isOperator ? "Relato declarado por el asegurado" : "Resumen de lo enviado"}
      >
        <p className="text-sm">
          {formatPhotoCount(data.submitted.photoCount)} · monto de reparación declarado{" "}
          {formatMoney(data.submitted.declaredAmount)}
        </p>
        <div className="mt-3 border-t pt-3">
          <p className="text-xs text-muted-foreground">
            {isOperator
              ? "Relato del asegurado"
              : "Descripción del daño"}
          </p>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed">
            {data.submitted.damageDescription}
          </p>
          {isOperator ? (
            <p className="mt-2 text-xs text-muted-foreground">
              Transcripción del relato recibido. La identidad de quien registró se consulta
              en el detalle interno.
            </p>
          ) : null}
        </div>
      </Section>

      {/* 6 a 10 — Constancia del registro, detrás de una regla. */}
      <Section
        title="Comprobante técnico"
        hint="Sirve para auditar el registro en la red. No lo necesitás para seguir el trámite."
      >
        <dl className="space-y-3">
          <CopyableField
            label="Identificador de fotos en IPFS"
            value={data.receipt.evidenceId}
          />
          <CopyableField
            label={
              isOperator
                ? "Token de acceso para el asegurado"
                : "Token de acceso"
            }
            value={data.accessToken}
          />
          {data.receipt.transactionHashes.map((hash) => (
            <CopyableField key={hash} label="Transacción en la red" value={hash} />
          ))}
        </dl>
        <div className="mt-4">
          <Note>{data.onChainScope}</Note>
        </div>
        <div className="mt-3">
          <Link
            to={`/traza/${data.number}`}
            className="text-sm underline underline-offset-4"
          >
            Ver traza pública ›
          </Link>
        </div>
      </Section>
    </Screen>
  )
}
