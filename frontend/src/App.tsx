import type { ReactElement } from "react"
import { Navigate, Route, Routes } from "react-router-dom"
import {
  MK01EvidenceUpload,
  MK02RegistrationReceipt,
  MK03ClaimStatus,
  MK04ResultExplanation,
  MK05ReviewerInbox,
  MK06EscalatedResolution,
  MK07QueueMonitor,
  MK08PublicTrace,
  MK09MyClaims,
} from "./views"
import { ALLOWED_PATTERNS, AppShell, homeOf } from "@/components/app-shell"
import { useSession } from "@/lib/session"

// Una ruta por vista del prototipo (MK-01 a MK-09). Las URL van en español
// porque son visibles al usuario. El número de reclamo es parámetro de ruta y
// no llave de acceso: identificar y abrir se separaron con RF-47, y el token de
// capacidad viaja en el fragmento.
//
// La navegación es la barra lateral de `app-shell.tsx` (ADR-015). Lo que cada
// perfil alcanza surge de la matriz de visibilidad por actor (RF-46).
const ROUTES = [
  { path: "/", Component: MK01EvidenceUpload },
  { path: "/constancia/:numero", Component: MK02RegistrationReceipt },
  { path: "/estado/:numero", Component: MK03ClaimStatus },
  { path: "/explicacion/:numero", Component: MK04ResultExplanation },
  { path: "/revision", Component: MK05ReviewerInbox },
  { path: "/resolucion/:numero", Component: MK06EscalatedResolution },
  { path: "/monitor", Component: MK07QueueMonitor },
  { path: "/traza/:numero", Component: MK08PublicTrace },
  { path: "/mis-reclamos", Component: MK09MyClaims },
]

/**
 * Redirige la ruta que el perfil vigente no tiene en su barra. No dibuja un
 * rechazo propio: el recorte que cuenta es el del servidor sobre el token
 * (RF-46 y RF-48), y una pantalla que lo imitara del lado del cliente daría a
 * entender que la restricción vive acá.
 */
function ForProfile({
  pattern,
  children,
}: {
  pattern: string
  children: ReactElement
}) {
  const { profile } = useSession()
  if (!ALLOWED_PATTERNS[profile].includes(pattern)) {
    return <Navigate to={homeOf(profile)} replace />
  }
  return children
}

export default function App() {
  return (
    <AppShell>
      <Routes>
        {ROUTES.map(({ path, Component }) => (
          <Route
            key={path}
            path={path}
            element={
              <ForProfile pattern={path}>
                <Component />
              </ForProfile>
            }
          />
        ))}
        {/* Sin número: MK-08 pide el identificador en su propio campo y MK-02
            explica que la constancia se muestra una sola vez. */}
        <Route path="/traza" element={<MK08PublicTrace />} />
        <Route
          path="/constancia"
          element={
            <ForProfile pattern="/constancia/:numero">
              <MK02RegistrationReceipt />
            </ForProfile>
          }
        />
      </Routes>
    </AppShell>
  )
}
