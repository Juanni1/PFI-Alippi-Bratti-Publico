// Cliente de la API. La dirección entra por variable de build y no hay ninguna
// literal en el código (RNF-29); sin la variable, el desarrollo corre contra los
// mocks de MSW sobre el mismo origen.

const BASE_URL = import.meta.env.VITE_API_URL ?? ""

// La lectura de los tokens vive en `session.tsx`. El del reclamante es una
// capacidad emitida para un reclamo (RF-47) y viaja en el FRAGMENTO de la
// dirección: así no llega al servidor que sirve la SPA ni entra en la línea de
// pedido de la API, de modo que la capacidad no queda asentada en registros de
// acceso ajenos (adr-013). El del personal interno sale del perfil elegido,
// mientras la autenticación siga fuera de alcance (adr-006).

/**
 * Fallo de la API con su código. El `status` es lo que deja que cada pantalla
 * distinga un token que no abre ese reclamo de un reclamo que no existe: sin él
 * las vistas escribían un solo texto para las cuatro causas posibles y el
 * mensaje no ayudaba a nadie a corregir nada. El `0` queda reservado para el
 * pedido que no llegó a destino.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
  ) {
    super(title)
  }
}

const SIN_RED = "No se pudo alcanzar la API"

/**
 * Una respuesta correcta que no trae JSON no es un caso teórico: el alojamiento
 * estático responde el index.html de la SPA a cualquier ruta que no reconoce, de
 * modo que un pedido mal dirigido volvía con 200 y reventaba al parsearlo fuera
 * de todo manejo de error.
 */
async function parse<T>(response: Response): Promise<T> {
  try {
    return (await response.json()) as T
  } catch {
    throw new ApiError(response.status, "La API respondió algo que no es JSON")
  }
}

async function request(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(`${BASE_URL}${path}`, init)
  } catch {
    throw new ApiError(0, SIN_RED)
  }
}

export async function apiGet<T>(path: string, token?: string | null): Promise<T> {
  const response = await request(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { title?: string }
    throw new ApiError(response.status, body.title ?? "No se pudo obtener el recurso")
  }
  return parse<T>(response)
}

// Sólo rutas relativas canónicas: no URL absoluta, query, fragmento ni escapes.
export function isEvidencePhotoPath(url: string): boolean {
  return url === url.trim() && /^\/reclamos\/[A-Za-z0-9_-]+\/evidencia\/[1-9]\d*$/.test(url)
}

export async function apiGetEvidencePhoto(
  path: string,
  token: string,
  signal: AbortSignal,
): Promise<Blob> {
  if (!isEvidencePhotoPath(path) || !token) {
    throw new ApiError(0, "Ruta o sesión de fotografía no válida")
  }
  const response = await request(path, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
    credentials: "omit",
    referrerPolicy: "no-referrer",
    redirect: "error",
    cache: "no-store",
    signal,
  })
  if (!response.ok) throw new ApiError(response.status, "No se pudo recuperar la fotografía.")
  return response.blob()
}

export async function apiPost<T>(
  path: string,
  body: unknown,
  token?: string | null,
): Promise<T> {
  const isFormData = body instanceof FormData
  const response = await request(path, {
    method: "POST",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
    },
    body: isFormData ? body : JSON.stringify(body),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new ApiError(
      response.status,
      (payload as { title?: string }).title ?? "No se pudo completar la operación",
    )
  }
  return parse<T>(response)
}
