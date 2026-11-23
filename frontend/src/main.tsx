import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { BrowserRouter } from "react-router-dom"
import App from "./App"
import { SessionProvider } from "./lib/session"
import "./index.css"

/**
 * MSW intercepta la API para poder trabajar sin backend.
 *
 * La decisión entra por variable y no por modo de compilación: atarla a DEV
 * hacía imposible correr el servidor de desarrollo contra el backend real, que
 * es justamente lo que hace falta para verificar que la SPA y la API se
 * entienden. Con VITE_USE_MOCKS se fuerza en cualquiera de los dos sentidos;
 * sin la variable, los mocks se usan solo en desarrollo y solo si no hay una
 * API a la que apuntar.
 */
async function enableMocks() {
  const forzado = import.meta.env.VITE_USE_MOCKS
  const usarMocks =
    forzado !== undefined
      ? forzado === "true"
      : import.meta.env.DEV && !import.meta.env.VITE_API_URL
  if (!usarMocks) return
  const { worker } = await import("./mocks/browser")
  await worker.start({ onUnhandledRequest: "bypass" })
}

const queryClient = new QueryClient()

enableMocks().then(() => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <SessionProvider>
            <App />
          </SessionProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  )
})
