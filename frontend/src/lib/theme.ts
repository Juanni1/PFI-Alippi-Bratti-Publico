// Modo claro y oscuro. La clase `dark` va en <html>, que es lo que espera el
// `@custom-variant dark (&:is(.dark *))` de index.css.
//
// El estado no vive en un `useState` por componente: la barra lateral y la
// banda angosta dibujan el control a la vez, y con estado por instancia el ícono
// de una quedaba al revés después de tocar la otra. La fuente de verdad es la
// clase del documento y los suscriptores se enteran por `useSyncExternalStore`.
//
// Quien nunca eligió sigue al sistema, y lo sigue en vivo: cambiar el tema del
// escritorio con la pestaña abierta se ve. La primera elección manual corta esa
// sujeción, porque a partir de ahí hay una preferencia declarada que respetar.

import { useSyncExternalStore } from "react"

export type Theme = "claro" | "oscuro"

const STORAGE_THEME = "pfi.tema"
const listeners = new Set<() => void>()

/** Lo guardado, si es una de las dos formas que este módulo escribe. */
function stored(): Theme | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_THEME)
    return raw === "claro" || raw === "oscuro" ? raw : null
  } catch {
    // Ventana privada o almacenamiento bloqueado: se cae al sistema.
    return null
  }
}

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "oscuro" : "claro"
}

// El arranque lo resolvió el script de index.html antes del primer pintado, así
// que acá se lee la clase en lugar de recalcularla: si los dos criterios
// difirieran, el que el usuario ya vio es el del documento.
let current: Theme = document.documentElement.classList.contains("dark")
  ? "oscuro"
  : "claro"

function apply(theme: Theme) {
  current = theme
  document.documentElement.classList.toggle("dark", theme === "oscuro")
  listeners.forEach((listener) => listener())
}

if (typeof window !== "undefined" && window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (stored()) return
    apply(systemTheme())
  })
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useSyncExternalStore(subscribe, () => current)

  return {
    theme,
    toggle: () => {
      const next: Theme = current === "oscuro" ? "claro" : "oscuro"
      try {
        window.localStorage.setItem(STORAGE_THEME, next)
      } catch {
        // Sin poder guardar, el cambio vale para esta pestaña y nada más.
      }
      apply(next)
    },
  }
}
