// Convenciones comunes a las nueve pantallas (00-criterios.md §3).
//
// Las nueve comparten encabezado y barra lateral; la navegación la agregó
// adr-015. El asegurado inicia un reclamo junto al título de «Mis reclamos»;
// el operador alcanza el mismo formulario desde su navegación.
//
// Lo que cambia entre actores es el CONTENIDO de la banda, no su estructura: el
// reclamante ve su póliza y su reclamo, el personal interno ve la sesión con la
// que opera. La matriz de la sección 2 gobierna esa diferencia.

import { useState } from "react"
import type { ReactNode } from "react"
import isotipoBlanco from "@/assets/veredic-isotipo-blanco.svg"
import isotipoColor from "@/assets/veredic-isotipo-color.svg"
import type { UnmeasuredNumber, UnmeasuredString } from "@/types"
import { ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"

type ScreenProps = {
  context: string
  session: string
  title: string
  subtitle?: string
  /** La acción principal de la pantalla, a la derecha del título. */
  action?: ReactNode
  children: ReactNode
}

export function Screen({
  context,
  session,
  title,
  subtitle,
  action,
  children,
}: ScreenProps) {
  return (
    <div className="mx-auto max-w-3xl px-6 pb-16">
      <header className="flex items-baseline justify-between border-b py-3 text-sm">
        <span className="font-medium">{context}</span>
        <span className="text-muted-foreground">{session}</span>
      </header>
      {/* La acción va al lado del título y no en la barra lateral: es de esta
          pantalla y no un destino más. En pantalla angosta cae debajo. */}
      <div className="mt-6 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {subtitle ? (
            <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
          ) : null}
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
      <div className="mt-6 space-y-6">{children}</div>
    </div>
  )
}

export function Section({
  title,
  hint,
  children,
  className,
}: {
  title?: string
  hint?: string
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cn("rounded-lg border p-5", className)}>
      {title ? (
        <div className="mb-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
        </div>
      ) : null}
      {children}
    </section>
  )
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="text-xs leading-relaxed text-muted-foreground">{children}</p>
}

/**
 * Indicador semántico del estado del reclamo según el manual de marca de Veredic:
 * Constancia (#12A57A, texto #0B6B50) identifica las resoluciones verificadas;
 * Revisión (#A16207, texto #8A5306) señala los casos derivados a un liquidador.
 */
export function StatusBadge({ label }: { label: string }) {
  if (label === "Aprobado" || label.startsWith("Análisis favorable")) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-[#12A57A]/40 bg-[#12A57A]/10 px-2.5 py-0.5 text-xs font-medium text-[#0B6B50] dark:text-[#12A57A]">
        <span aria-hidden="true">✓</span> Verificado
      </span>
    )
  }
  if (label.startsWith("En revisión")) {
    return (
      <span className="inline-flex items-center rounded-full border border-[#A16207]/40 bg-[#A16207]/10 px-2.5 py-0.5 text-xs font-medium text-[#8A5306] dark:text-[#F59E0B]">
        Lo revisa una persona
      </span>
    )
  }
  return null
}

/**
 * Sello de verificación de Veredic para la pantalla del asegurado.
 */
export function VerificationSeal() {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold">
      <img
        src={isotipoColor}
        alt=""
        aria-hidden="true"
        className="h-3.5 w-3.5 shrink-0 dark:hidden"
      />
      <img
        src={isotipoBlanco}
        alt=""
        aria-hidden="true"
        className="hidden h-3.5 w-3.5 shrink-0 dark:block"
      />
      Decisión verificada con veredic
    </span>
  )
}

/**
 * Copia al portapapeles. `navigator.clipboard` solo existe en contexto seguro,
 * y la vista por IP del servidor de desarrollo no lo es; sin el respaldo, el
 * botón no hacía nada justamente en el escenario en que se prueban las
 * pantallas.
 */
async function copyToClipboard(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {
    // Sigue por el respaldo.
  }
  try {
    const area = document.createElement("textarea")
    area.value = value
    area.setAttribute("readonly", "")
    area.style.position = "fixed"
    area.style.opacity = "0"
    document.body.appendChild(area)
    area.select()
    const copiado = document.execCommand("copy")
    document.body.removeChild(area)
    return copiado
  } catch {
    return false
  }
}

/**
 * Un identificador largo con su botón de copiar. Los CID y los hashes son la
 * pieza con la que el reclamante y el auditor ejercen la comprobación por cuenta
 * propia que el hallazgo 3 de la encuesta ubica entre los factores de confianza,
 * y transcribir sesenta y seis caracteres a mano no es ejercerla.
 */
export function CopyableValue({
  value,
  href,
  className,
}: {
  value: string
  href?: string
  className?: string
}) {
  const [state, setState] = useState<"listo" | "copiado" | "falló">("listo")

  return (
    <span className={cn("flex min-w-0 items-start gap-2", className)}>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="min-w-0 font-mono break-all underline underline-offset-4 hover:text-foreground"
        >
          {value}
        </a>
      ) : (
        <span className="min-w-0 font-mono break-all">{value}</span>
      )}
      <button
        type="button"
        aria-label={`Copiar ${value}`}
        className="shrink-0 text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
        onClick={async () => {
          const ok = await copyToClipboard(value)
          setState(ok ? "copiado" : "falló")
          window.setTimeout(() => setState("listo"), 2000)
        }}
      >
        {state === "listo" ? "copiar" : state === "copiado" ? "copiado" : "no se pudo"}
      </button>
    </span>
  )
}

/** El mismo control con su rótulo, para las listas de constancia. */
export function CopyableField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-xs">
        <CopyableValue value={value} />
      </dd>
    </div>
  )
}

/**
 * Lo que se dibuja mientras el pedido viaja. Las vistas devolvían `null` en ese
 * intervalo, de modo que un pedido lento o colgado se veía igual que una
 * pantalla rota: en blanco y sin nada que leer.
 */
export function Loading({ children }: { children?: ReactNode }) {
  return (
    <Section>
      <Note>{children ?? "Consultando…"}</Note>
    </Section>
  )
}

/** Una lista sin elementos dice por qué está vacía y no se dibuja como nada. */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-2 text-sm text-muted-foreground">{children}</p>
}

/**
 * Traduce el fallo al motivo que lo produjo. Un solo texto para el 401, el 403,
 * el 404 y la caída de red le pedía al lector que adivinara cuál de las cuatro
 * cosas tenía que corregir.
 */
export function describeError(
  error: unknown,
  texts: Partial<Record<number, string>> = {},
): string {
  if (!(error instanceof ApiError)) {
    return texts[0] ?? "Ocurrió un error inesperado."
  }
  if (texts[error.status]) return texts[error.status] as string
  switch (error.status) {
    case 0:
      return "No pudimos conectar con el servidor. Probá de nuevo en un momento."
    case 401:
      return "El enlace o token no es válido para este reclamo."
    case 403:
      return "No tenés permiso para realizar esta operación."
    case 404:
      return "No encontramos un reclamo con ese número."
    case 409:
      return "El reclamo todavía no llegó a esta etapa."
    default:
      return error.title
  }
}

/**
 * Dibuja un valor que puede no estar medido. Cuando el contrato lo manda nulo,
 * la pantalla escribe la marca literal y no un número plausible: es la misma
 * regla con la que las láminas dejaron veintiuna marcas [VERIFICAR] sobre doce
 * de las veintidós.
 */
export function Unmeasured({
  value,
  format,
}: {
  value: UnmeasuredNumber | UnmeasuredString
  format?: (raw: never) => string
}) {
  if (value.value === null || !value.measured) {
    return (
      <span
        className="text-muted-foreground"
        title={
          value.requirement
            ? `Sin medir; lo cierra ${value.requirement}`
            : "Valor sin fijar"
        }
      >
        [VERIFICAR]
      </span>
    )
  }
  const raw = value.value as never
  return <span>{format ? format(raw) : String(value.value)}</span>
}

// «las 1 fotos» era lo que salía de concatenar el conteo con el plural fijo. El
// singular se resuelve acá y no en cada pantalla para que las tres que muestran
// el conteo lo escriban igual.
export function formatPhotoCount(count: number): string {
  return count === 1 ? "1 foto" : `${count} fotos`
}

export function formatMoney(amount: number): string {
  return `$${amount.toLocaleString("es-AR")}`
}

// Los umbrales de confianza se dibujan con coma decimal y dos cifras, que es
// como los escriben las láminas y adr-014. Sin esto `Unmeasured` los mostraba
// con `String()`, que da «0.9» donde la lámina dice «0,90»: el valor es el
// mismo y la pantalla dejaba de coincidir con su especificación.
// MK-04 escribe el umbral como porcentaje entero y no como fracción, porque la
// pantalla que lo usa es la del reclamante y ahí conviven con la confianza, que
// también va en porcentaje. Las otras láminas lo escriben como fracción.
export function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`
}

export function formatThreshold(value: number): string {
  return value.toLocaleString("es-AR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

// Reloj de veinticuatro horas, que es el que dibujan las veintidós láminas. El
// formato por omisión de es-AR usa doce y produce «02:31 p. m.» donde la lámina
// escribe «14:31».
const CLOCK = { hour12: false, hour: "2-digit", minute: "2-digit" } as const

// Las marcas de tiempo se dibujan en el huso de quien mira: `toLocaleString`
// resuelve el del navegador sin que haya que declararlo. El nombre se pide de
// forma explícita para poder caer en Buenos Aires cuando el entorno no lo
// expone, que es el huso en el que corre el prototipo y el que leen sus
// láminas.
const FALLBACK_TIME_ZONE = "America/Argentina/Buenos_Aires"

const TIME_ZONE: string = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || FALLBACK_TIME_ZONE
  } catch {
    return FALLBACK_TIME_ZONE
  }
})()

/**
 * El backend guarda y serializa sus marcas en UTC pero sin designador de huso
 * (`datetime.now(timezone.utc).replace(tzinfo=None)`), de modo que
 * `2026-08-09T17:31:00` es UTC aunque no lo diga. `new Date` lee esa forma como
 * hora local y en GMT-3 la pantalla adelantaba tres horas. Se le agrega la `Z`
 * que le falta; lo que ya trae huso, como los fixtures de MSW en `-03:00`, se
 * interpreta tal cual viene.
 */
export function parseInstant(iso: string): Date {
  const hasZone = /(?:Z|z|[+-]\d{2}:?\d{2})$/.test(iso)
  return new Date(hasZone ? iso : `${iso}Z`)
}

export function formatDateTime(iso: string, withSeconds = false): string {
  const day = parseInstant(iso).toLocaleDateString("es-AR", {
    timeZone: TIME_ZONE,
    day: "numeric",
    month: "short",
    year: "numeric",
  })
  return `${day.replace(" de ", " ").replace(" de ", " ")}, ${formatTime(iso, withSeconds)}`
}

export function formatTime(iso: string, withSeconds = false): string {
  return parseInstant(iso).toLocaleTimeString("es-AR", {
    timeZone: TIME_ZONE,
    ...CLOCK,
    ...(withSeconds ? { second: "2-digit" } : {}),
  })
}

/**
 * Una fecha sin hora (`2026-08-08`) se interpreta como UTC y, leída en un huso
 * negativo, retrocede un día: la fecha del siniestro se dibujaría como la
 * anterior. Se la trata como fecha local.
 */
export function formatDay(iso: string): string {
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(iso)
  const date = dateOnly ? new Date(`${iso}T00:00:00`) : parseInstant(iso)
  return date
    .toLocaleDateString("es-AR", {
      ...(dateOnly ? {} : { timeZone: TIME_ZONE }),
      day: "numeric",
      month: "short",
    })
    .replace(" de ", " ")
}
