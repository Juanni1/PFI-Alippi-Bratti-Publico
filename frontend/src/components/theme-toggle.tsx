// El control del modo oscuro. Es un botón y no un desplegable de tres opciones:
// mientras nadie eligió, el tema lo pone el sistema, y la primera pulsación es
// la que declara la preferencia. Un tercer estado «automático» explícito le
// pediría al lector que administrara algo que ya está administrado.

import { Moon, Sun } from "lucide-react"
import { useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, toggle } = useTheme()
  const oscuro = theme === "oscuro"
  const etiqueta = oscuro ? "Cambiar a modo claro" : "Cambiar a modo oscuro"

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={etiqueta}
      aria-pressed={oscuro}
      title={etiqueta}
      className={cn(
        "rounded-md border p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground",
        className,
      )}
    >
      {oscuro ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </button>
  )
}
