// Sesión del prototipo: qué perfil opera y sobre qué reclamo.
//
// La autenticación quedó fuera de alcance (adr-006), así que la sesión no se
// establece iniciándola sino eligiéndola. Lo que este módulo guarda es esa
// elección; el recorte por perfil lo sigue haciendo el servidor sobre el token
// (RF-46 y RF-48) y ninguna pantalla decide por su cuenta qué campos le
// corresponden a quién.
//
// El token del reclamante es una capacidad emitida para un reclamo (RF-47) y no
// una credencial de persona: se obtiene registrando en MK-01 y viaja en el
// fragmento de la dirección (adr-013). Por eso el fragmento manda siempre sobre
// lo que haya guardado el navegador: quien abre el enlace que le llegó tiene que
// ver ese reclamo y no el último que se cargó en esa computadora.

import { createContext, useCallback, useContext, useMemo, useState } from "react"
import type { ReactNode } from "react"
import { useLocation } from "react-router-dom"
import { DEMO_CLAIMS } from "@/demo/claims"

export const PROFILES = ["asegurado", "revisor", "operador", "auditor"] as const

export type Profile = (typeof PROFILES)[number]

export const PROFILE_LABEL: Record<Profile, string> = {
  asegurado: "Asegurado",
  revisor: "Revisor",
  operador: "Operador",
  auditor: "Auditoría pública",
}

/** Un reclamo que este navegador puede abrir, con la capacidad que lo abre. */
export type SavedClaim = {
  number: string
  /** Solo el asegurado tiene token de capacidad; el personal interno opera con su sesión. */
  accessToken: string | null
}

// Las dos sesiones internas que el backend trae configuradas
// (`SESIONES_POR_DEFECTO` en app/exposicion/security.py) y que los mocks de MSW
// resuelven por prefijo. Son valores iniciales editables y no constantes de
// configuración: la dirección de la API sigue siendo la única cosa que entra por
// variable de build (RNF-29).
const DEFAULT_STAFF_TOKENS: Record<Profile, string> = {
  asegurado: "",
  revisor: "revisor-rev-04",
  operador: "operador-op-02",
  auditor: "",
}

const STORAGE_PROFILE = "pfi.perfil"
const STORAGE_TOKENS = "pfi.tokens"
const STORAGE_CLAIMS = "pfi.reclamos"
const STORAGE_DEMO = "pfi.demo-sembrada"

function read<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

function write(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Modo privado o almacenamiento bloqueado: la sesión vale para esta pestaña.
  }
}

type SessionValue = {
  profile: Profile
  setProfile: (profile: Profile) => void
  staffTokens: Record<Profile, string>
  setStaffToken: (profile: Profile, token: string) => void
  /** Los reclamos que este navegador guardó, en orden de llegada. */
  savedClaims: SavedClaim[]
  addClaim: (claim: SavedClaim) => void
  removeClaim: (number: string) => void
}

const SessionContext = createContext<SessionValue | null>(null)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [profile, setProfileState] = useState<Profile>(() => {
    const stored = read<Profile | null>(STORAGE_PROFILE, null)
    return stored && PROFILES.includes(stored) ? stored : "asegurado"
  })
  const [staffTokens, setStaffTokens] = useState<Record<Profile, string>>(() => ({
    ...DEFAULT_STAFF_TOKENS,
    ...read<Partial<Record<Profile, string>>>(STORAGE_TOKENS, {}),
  }))
  const [savedClaims, setSavedClaims] = useState<SavedClaim[]>(() => {
    const guardados = read<SavedClaim[]>(STORAGE_CLAIMS, [])
    if (guardados.length > 0) return guardados
    // Los casos de demostración se depositan una sola vez y detrás de su marca:
    // quien ya cargó los suyos y después los borró no tiene por qué encontrarlos
    // de vuelta en la próxima visita.
    if (read<boolean>(STORAGE_DEMO, false)) return []
    write(STORAGE_DEMO, true)
    write(STORAGE_CLAIMS, DEMO_CLAIMS)
    return DEMO_CLAIMS
  })

  const setProfile = useCallback((next: Profile) => {
    setProfileState(next)
    write(STORAGE_PROFILE, next)
  }, [])

  const setStaffToken = useCallback((target: Profile, token: string) => {
    setStaffTokens((current) => {
      const next = { ...current, [target]: token }
      write(STORAGE_TOKENS, next)
      return next
    })
  }, [])

  const addClaim = useCallback((claim: SavedClaim) => {
    setSavedClaims((current) => {
      const sin = current.filter((guardado) => guardado.number !== claim.number)
      const next = [...sin, claim]
      write(STORAGE_CLAIMS, next)
      return next
    })
  }, [])

  const removeClaim = useCallback((number: string) => {
    setSavedClaims((current) => {
      const next = current.filter((guardado) => guardado.number !== number)
      write(STORAGE_CLAIMS, next)
      return next
    })
  }, [])

  const value = useMemo(
    () => ({
      profile,
      setProfile,
      staffTokens,
      setStaffToken,
      savedClaims,
      addClaim,
      removeClaim,
    }),
    [profile, setProfile, staffTokens, setStaffToken, savedClaims, addClaim, removeClaim],
  )

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext)
  if (!value) throw new Error("useSession fuera de SessionProvider")
  return value
}

/**
 * Token de capacidad del reclamante. El fragmento de la dirección tiene
 * prioridad sobre la sesión guardada, y se lee con `useLocation` para que un
 * cambio de dirección vuelva a evaluarlo: `window.location` no es reactivo y la
 * pantalla se quedaba con el token de la navegación anterior.
 */
export function useAccessToken(numero?: string): string | null {
  const { hash } = useLocation()
  const { savedClaims } = useSession()
  const fromFragment = new URLSearchParams(hash.replace(/^#/, "")).get("acceso")
  if (fromFragment) return fromFragment
  // Sin fragmento, el token es el que este navegador guardó PARA ESE reclamo.
  // Buscarlo por número y no tomar el último guardado evita mandar la capacidad
  // de un reclamo a pedir otro, que es un 401 con apariencia de reclamo ajeno.
  const guardado = savedClaims.find((claim) => claim.number === numero)
  return guardado?.accessToken ?? null
}

/**
 * Token de la sesión interna. Igual que arriba, la dirección manda sobre la
 * elección del sidebar: un enlace con `?sesion=` sirve para reproducir un caso
 * sin tocar la configuración del navegador.
 */
export function useStaffToken(): string | null {
  const { hash, search } = useLocation()
  const { profile, staffTokens } = useSession()
  const fromFragment = new URLSearchParams(hash.replace(/^#/, "")).get("sesion")
  const fromQuery = new URLSearchParams(search).get("sesion")
  return fromFragment ?? fromQuery ?? staffTokens[profile] ?? null
}
