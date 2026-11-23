// Cáscara de la aplicación: encabezado de cada pantalla más una barra lateral
// por perfil (ADR-015).
//
// Las láminas dibujan el CONTENIDO de cada pantalla y no esta cáscara. Lo que la
// barra ofrece por perfil sale de la matriz de §2: el asegurado no ve la bandeja
// de revisión ni el monitor de la cola; el operador alcanza el formulario de
// carga, pero el revisor y el auditor no. La traza pública la alcanzan los cuatro
// porque RF-44 la sirve sin acreditar titularidad.
//
// Los destinos se nombran como los nombra el producto. El identificador del
// mockup es trazabilidad contra 70-mockups/ y vive en los nombres de archivo y
// en los comentarios; en la pantalla sería vocabulario interno filtrado a la
// interfaz, que es lo mismo que RNF-35 prohíbe para los estados.
//
// El desplegable de perfil ocupa el lugar del inicio de sesión que adr-006 dejó
// fuera de alcance. Elegir el perfil acá no otorga nada: el recorte por campo lo
// hace el servidor sobre el token (RF-46 y RF-48), de modo que pasarse a revisor
// sin su token devuelve 403 en lugar de datos. La leyenda dice lo que el lector
// necesita para operar y no el estado de la deuda del proyecto.
//
// La carga de un reclamo sigue siendo una acción al lado del título de «Mis
// reclamos» para el asegurado; el operador la alcanza desde su navegación.

import { useState } from "react"
import type { ReactNode } from "react"
import { NavLink, useLocation } from "react-router-dom"
import isotipoBlanco from "@/assets/veredic-isotipo-blanco.svg"
import isotipoColor from "@/assets/veredic-isotipo-color.svg"
import logoBlanco from "@/assets/veredic-logo-blanco.png"
import logoColor from "@/assets/veredic-logo-color.png"
import { ThemeToggle } from "@/components/theme-toggle"
import { PROFILES, PROFILE_LABEL, useSession } from "@/lib/session"
import type { Profile } from "@/lib/session"
import { cn } from "@/lib/utils"

type Destination = {
  /** Patrón de la ruta, que es también la llave del control de acceso. */
  pattern: string
  label: string
  hint: string
  href: string
}

const TRACE: Destination = {
  pattern: "/traza/:numero",
  label: "Traza pública",
  hint: "auditoría abierta sin iniciar sesión",
  href: "/traza",
}

const DESTINATIONS: Record<Profile, Destination[]> = {
  // Para el asegurado, la carga es la acción de «Mis reclamos» y vive junto a
  // su título, sin duplicar entradas. El operador tiene una entrada propia de
  // navegación para registrar y conserva el monitor como inicio.
  asegurado: [
    {
      pattern: "/mis-reclamos",
      label: "Mis reclamos",
      hint: "reclamos guardados en este dispositivo",
      href: "/mis-reclamos",
    },
    TRACE,
  ],
  revisor: [
    {
      pattern: "/revision",
      label: "Bandeja de revisión",
      hint: "reclamos derivados a revisión manual",
      href: "/revision",
    },
    TRACE,
  ],
  operador: [
    {
      pattern: "/monitor",
      label: "Monitor de la cola",
      hint: "estado de pruebas y liquidaciones",
      href: "/monitor",
    },
    {
      pattern: "/",
      label: "Registrar reclamo",
      hint: "cargar fotos, monto y relato del daño",
      href: "/",
    },
    TRACE,
  ],
  auditor: [TRACE],
}

// Los destinos de un reclamo abierto. No son entradas fijas de la barra: salen
// cuando hay un reclamo en la ruta, que es lo que los vuelve alcanzables.
const CLAIM_DESTINATIONS: Record<Profile, { pattern: string; label: string }[]> = {
  asegurado: [
    { pattern: "/estado", label: "Estado" },
    { pattern: "/explicacion", label: "Cómo se analizó" },
    { pattern: "/traza", label: "Traza pública" },
  ],
  revisor: [
    { pattern: "/resolucion", label: "Resolución" },
    { pattern: "/traza", label: "Traza pública" },
  ],
  operador: [
    { pattern: "/resolucion", label: "Liquidación" },
    { pattern: "/traza", label: "Traza pública" },
  ],
  auditor: [],
}

/** Las rutas que cada perfil alcanza, para el control de acceso. */
export const ALLOWED_PATTERNS: Record<Profile, string[]> = {
  asegurado: [
    "/",
    "/mis-reclamos",
    "/constancia/:numero",
    "/estado/:numero",
    "/explicacion/:numero",
    "/traza/:numero",
  ],
  revisor: ["/revision", "/resolucion/:numero", "/traza/:numero"],
  operador: ["/", "/constancia/:numero", "/monitor", "/resolucion/:numero", "/traza/:numero"],
  auditor: ["/traza/:numero"],
}

/** Adónde va un perfil cuando la ruta pedida no es suya. */
export function homeOf(profile: Profile): string {
  return DESTINATIONS[profile][0].href
}

function ProfilePicker() {
  const { profile, setProfile, staffTokens, setStaffToken } = useSession()
  const needsToken = profile === "revisor" || profile === "operador"

  return (
    <div className="border-b px-4 py-4">
      <label className="block text-xs font-medium text-muted-foreground" htmlFor="perfil">
        Perfil
      </label>
      <select
        id="perfil"
        className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm"
        value={profile}
        onChange={(event) => setProfile(event.target.value as Profile)}
      >
        {PROFILES.map((value) => (
          <option key={value} value={value}>
            {PROFILE_LABEL[value]}
          </option>
        ))}
      </select>
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        Elegi el perfil para navegar por la aplicación
      </p>
      {needsToken ? (
        <label className="mt-3 block text-xs">
          <span className="text-muted-foreground">Token de la sesión interna</span>
          <input
            className="mt-1 w-full rounded-md border px-2 py-1.5 font-mono text-xs"
            value={staffTokens[profile]}
            onChange={(event) => setStaffToken(profile, event.target.value)}
          />
        </label>
      ) : null}
    </div>
  )
}

// Las rutas que llevan un reclamo en el camino. La barra vive FUERA de
// `<Routes>`, así que `useParams` le devuelve siempre vacío: el número se lee
// del camino, que es lo que sí tiene a mano.
const CLAIM_ROUTE = /^\/(estado|explicacion|resolucion|constancia|traza)\/([^/]+)/

/** El reclamo que la ruta tiene abierto, con sus destinos. */
function OpenClaim() {
  const { profile, savedClaims } = useSession()
  const { pathname, hash } = useLocation()
  const numero = CLAIM_ROUTE.exec(pathname)?.[2]
  if (!numero) return null

  const destinos = CLAIM_DESTINATIONS[profile]
  if (destinos.length === 0) return null

  const guardado = savedClaims.find((claim) => claim.number === numero)
  const fragmento =
    profile === "asegurado" && guardado?.accessToken
      ? `#acceso=${guardado.accessToken}`
      : hash

  return (
    <div className="border-t px-2 py-3">
      <p className="px-2 text-[11px] font-medium text-muted-foreground">
        Reclamo {numero}
      </p>
      {destinos.map((destino) => (
        <NavLink
          key={destino.pattern}
          to={
            destino.pattern === "/traza"
              ? `/traza/${numero}`
              : `${destino.pattern}/${numero}${fragmento}`
          }
          className={({ isActive }) =>
            cn(
              "block rounded-md px-2 py-1.5 text-sm hover:bg-accent",
              isActive && "bg-accent font-medium",
            )
          }
        >
          {destino.label}
        </NavLink>
      ))}
    </div>
  )
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { profile } = useSession()

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex items-start justify-between gap-2 border-b px-4 py-4">
        <div className="flex items-start gap-2.5">
          <img
            src={isotipoColor}
            alt=""
            aria-hidden="true"
            className="mt-0.5 h-6 w-6 shrink-0 dark:hidden"
          />
          <img
            src={isotipoBlanco}
            alt=""
            aria-hidden="true"
            className="mt-0.5 hidden h-6 w-6 shrink-0 dark:block"
          />
          <div>
            <p className="font-brand text-lg leading-none font-extrabold tracking-tight">
              veredic
            </p>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Gestión de siniestros
            </p>
          </div>
        </div>
        {/* En pantalla ancha este es el único control visible; en angosta la
            barra está plegada y el de la banda superior lo reemplaza. Los dos
            comparten estado, que es lo que resuelve `useSyncExternalStore`. */}
        <ThemeToggle className="hidden lg:block" />
      </div>
      <ProfilePicker />
      <nav className="px-2 py-3">
        {DESTINATIONS[profile].map((destination) => (
          <NavLink
            key={destination.pattern}
            to={destination.href}
            end={destination.href === "/"}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "block rounded-md px-2 py-2 text-sm hover:bg-accent",
                isActive && "bg-accent font-medium",
              )
            }
          >
            {destination.label}
            <span className="block text-[11px] text-muted-foreground">
              {destination.hint}
            </span>
          </NavLink>
        ))}
      </nav>
      <OpenClaim />
    </div>
  )
}

export function AppShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[17rem_1fr]">
      {/* En pantalla angosta la barra se pliega detrás de un control, porque
          ocuparía el ancho entero de una pantalla del reclamante. */}
      <div className="flex items-center justify-between border-b px-4 py-2.5 lg:hidden">
        <div className="flex items-center gap-2">
          <img
            src={logoColor}
            alt="Veredic - Gestión de siniestros"
            className="h-5 w-auto dark:hidden"
          />
          <img
            src={logoBlanco}
            alt="Veredic - Gestión de siniestros"
            className="hidden h-5 w-auto dark:block"
          />
        </div>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <button
            type="button"
            className="rounded-md border px-3 py-1.5 text-xs"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "Cerrar" : "Menú"}
          </button>
        </div>
      </div>
      <aside
        className={cn(
          "border-b bg-muted/20 lg:sticky lg:top-0 lg:h-screen lg:border-r lg:border-b-0",
          open ? "block" : "hidden lg:block",
        )}
      >
        <Sidebar onNavigate={() => setOpen(false)} />
      </aside>
      <main className="min-w-0">{children}</main>
    </div>
  )
}
