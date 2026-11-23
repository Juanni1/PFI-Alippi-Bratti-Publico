import React from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { setupWorker } from 'msw/browser'
import { http, HttpResponse, delay } from 'msw'
import { SessionProvider } from '../src/lib/session'
import { MK06EscalatedResolution } from '../src/views/mk06-escalated-resolution'
import { EvidenceGallery } from '../src/components/evidence-gallery'
import { REVIEWER_CLAIM_4127 } from '../src/mocks/fixtures/claims'
import { handlers } from '../src/mocks/handlers'
import '../src/index.css'

declare global {
  interface Window {
    testResult?: string
    browserKey: (payload: string) => void
    keyDone: (id: number, failed?: boolean) => void
  }
}

const assert = (condition: unknown, message: string) => {
  if (!condition) throw new Error(message)
}
const wait = async (predicate: () => unknown, message: string) => {
  const deadline = Date.now() + 6000
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message)
    await new Promise(resolve => setTimeout(resolve, 30))
  }
}
// onLoad actualiza hidden mediante React después de disponer de dimensiones naturales.
const isRenderedImage = (img: HTMLImageElement | null | undefined) => {
  if (!img || img.hidden || img.naturalWidth <= 0 || img.naturalHeight <= 0) return false
  const bounds = img.getBoundingClientRect()
  return bounds.width > 0 && bounds.height > 0
}
const text = () => document.body.textContent ?? ''
const button = (name: string) => [...document.querySelectorAll('button')]
  .find(el => el.textContent?.trim() === name)!
const keys = new Map<number, { resolve: () => void; reject: (error: Error) => void }>()
window.keyDone = (id, failed) => {
  const pending = keys.get(id)
  keys.delete(id)
  if (failed) pending?.reject(new Error('Falló la inyección de teclado CDP'))
  else pending?.resolve()
}
const key = (key: string) => new Promise<void>((resolve, reject) => {
  const id = keys.size + 1
  keys.set(id, { resolve, reject })
  window.browserKey(JSON.stringify({ id, key }))
})
const worker = setupWorker(...handlers)
const root = createRoot(document.getElementById('root')!)
let generation = 0
const render = async (claim = REVIEWER_CLAIM_4127, profile = 'revisor') => {
  worker.use(http.get('*/reclamos/SN-4127', () => {
    if (profile === 'operador') {
      const { availableResolutions, ...rest } = claim
      void availableResolutions
      return HttpResponse.json({ ...rest, settleable: claim.statusId === 'Aprobable' })
    }
    return HttpResponse.json(claim)
  }))
  flushSync(() => root.render(
    <MemoryRouter key={++generation} initialEntries={[
      `/resolucion/SN-4127#sesion=${profile}-prueba`,
    ]}>
      <SessionProvider><QueryClientProvider client={new QueryClient()}>
        <Routes><Route path="/resolucion/:numero" element={<MK06EscalatedResolution />} /></Routes>
      </QueryClientProvider></SessionProvider>
    </MemoryRouter>,
  ))
  await wait(() => text().includes('Justificación') || text().includes('Registrar liquidación'), 'MK-06 no abrió')
}
const withFiles = (files: typeof REVIEWER_CLAIM_4127.evidence.files) => ({
  ...REVIEWER_CLAIM_4127, evidence: { ...REVIEWER_CLAIM_4127.evidence, files },
})

try {
  await worker.start({ quiet: true, onUnhandledRequest: 'bypass' })
  // Se usan los archivos del fixture predeterminado, no imágenes personales.
  await render()
  await wait(() => document.querySelectorAll('img').length === 3,
    'Se esperaban tres fotografías renderizadas en MK-06')
  await wait(() => [...document.images].every(isRenderedImage),
    'Las fotos locales no se recuperaron o no se mostraron')
  assert(text().includes(REVIEWER_CLAIM_4127.evidence.evidenceId), 'Se perdió el identificador')
  for (const file of REVIEWER_CLAIM_4127.evidence.files) {
    const img = [...document.images].find(img => img.getAttribute('src') === file.url)
    assert(img?.alt.includes(file.filename), 'Falta URL individual o alt del archivo')
    assert(img?.referrerPolicy === 'no-referrer', 'La imagen puede filtrar la URL de sesión')
    assert(img?.crossOrigin === 'anonymous', 'La imagen no excluye credenciales cross-origin')
    const bounds = img!.getBoundingClientRect()
    assert(Math.abs(bounds.width / bounds.height - img!.naturalWidth / img!.naturalHeight) < 0.02,
      'La miniatura deforma la imagen')
  }
  const opener = button('Ampliar frente-izq.jpg')
  assert(opener, 'Falta control de ampliación con nombre accesible')
  opener.focus()
  assert(document.activeElement === opener, 'El disparador no recibió foco antes de Enter')
  await key('Enter')
  await wait(() => document.querySelector('dialog[open]'), 'Enter no abrió la ampliación')
  const enlarged = document.querySelector('dialog img') as HTMLImageElement
  await wait(() => isRenderedImage(enlarged), 'No cargó la ampliación')
  assert(enlarged.getBoundingClientRect().width > document.images[0].getBoundingClientRect().width,
    'La ampliación no aumenta el tamaño')
  const focusMetadata = () => ({
    tag: document.activeElement?.tagName ?? null,
    body: document.activeElement === document.body,
    close: document.activeElement === button('Cerrar ampliación'),
    inModal: document.querySelector('dialog[open]')?.contains(document.activeElement) ?? false,
    modal: document.querySelector('dialog[open]')?.matches(':modal') ?? false,
    documentFocused: document.hasFocus(),
  })
  const isFocusedModalControl = (state: ReturnType<typeof focusMetadata>) =>
    state.documentFocused && state.modal && state.inModal && state.close
  const beforeTab = focusMetadata()
  assert(isFocusedModalControl(beforeTab),
    `El control del modal no recibió foco antes de Tab: ${JSON.stringify({ beforeTab })}`)
  // Tres ciclos acotados; salir al navegador exige volver con el Tab inmediato.
  for (let cycle = 0; cycle < 3; cycle++) {
    const beforeTab = focusMetadata()
    await key('Tab')
    const afterTab = focusMetadata()
    const browserChrome = afterTab.body && !afterTab.documentFocused && afterTab.modal
    assert(isFocusedModalControl(afterTab) || browserChrome,
      `El foco escapó del modal: ${JSON.stringify({ cycle, beforeTab, afterTab })}`)
    if (browserChrome) {
      await key('Tab')
      const afterReturnTab = focusMetadata()
      assert(isFocusedModalControl(afterReturnTab),
        `Tab no devolvió el foco al control del modal: ${JSON.stringify({ cycle, beforeTab, afterTab, afterReturnTab })}`)
    }
  }
  await key('Escape')
  await wait(() => !document.querySelector('dialog[open]'), 'Escape no cerró el modal')
  assert(document.activeElement === opener, 'El foco no volvió al disparador')
  assert(button('Aprobar reclamo').disabled, 'La justificación dejó de ser obligatoria')

  const files = REVIEWER_CLAIM_4127.evidence.files
  let imageRequests = 0
  worker.use(http.get('*/evidence-fixtures/fallo.png', ({ request }) => {
    imageRequests++
    assert(!request.headers.has('authorization'), 'Se envió autorización a la foto')
    assert(!request.headers.has('referer'), 'Se envió referer a la foto')
    return new HttpResponse(null, { status: 503 })
  }))
  worker.use(http.get('*/evidence-fixtures/lenta.png', async () => {
    await delay(1000)
    return new HttpResponse(null, { status: 503 })
  }))
  await render(withFiles([
    files[0], { ...files[1], url: '/evidence-fixtures/fallo.png' },
    { ...files[2], url: undefined }, { ...files[0], filename: 'lenta.png', url: '/evidence-fixtures/lenta.png' },
  ]))
  await wait(() => text().includes('Cargando fotografía: lenta.png'), 'No se mostró la carga individual')
  await wait(() => text().includes('No se pudo recuperar paragolpes.jpg'), 'Falta error individual')
  assert(imageRequests > 0 && text().includes('URL no disponible para detalle-faro.png'), 'Falta estado de URL ausente')
  await wait(() => isRenderedImage(document.images[0]), 'La foto sana no terminó de mostrarse')
  assert(document.images[0].naturalWidth > 0, 'Una foto fallida ocultó la otra')
  const textarea = document.querySelector('textarea')!
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
  setter.call(textarea, 'Revisión sintética')
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
  await wait(() => !button('Aprobar reclamo').disabled, 'La galería bloqueó la resolución con justificación')
  assert(!button('Rechazar reclamo').disabled, 'Se bloqueó el rechazo')

  await render(withFiles([]))
  assert(text().includes('No hay fotografías disponibles para este reclamo.'), 'Falta estado vacío')
  await render({ ...withFiles([]), statusId: 'InconsistenciaEvidencia', availableResolutions: ['desfavorable'] })
  const restricted = document.querySelector('textarea')!
  setter.call(restricted, 'Revisión sintética')
  restricted.dispatchEvent(new Event('input', { bubbles: true }))
  await wait(() => !button('Rechazar reclamo').disabled, 'Se perdió la salida desfavorable')
  assert(button('Aprobar reclamo').disabled, 'Se habilitó una transición prohibida')
  await render(withFiles([]), 'operador')
  assert(!button('Aprobar reclamo') && !button('Rechazar reclamo'), 'El operador obtuvo acciones de revisor')
  assert(button('Registrar liquidación').disabled, 'El operador puede liquidar un estado no aprobable')
  await render({ ...withFiles([]), statusId: 'Aprobable' }, 'operador')
  assert(!button('Registrar liquidación').disabled, 'Se perdió la liquidación en Aprobable')
  // C4: originales sintéticos, sin gateway real ni servicios de producción.
  const photoBytes = await fetch(files[0].url!).then(response => response.arrayBuffer())
  const photoRequests: Request[] = []
  const binaryResponse = () => new HttpResponse(photoBytes.slice(0), {
    headers: { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' },
  })
  const publicUrls = [
    '/evidence-fixtures/publica.png',
    // Una ruta parecida a la API no convierte un origen externo en confiable.
    'https://gateway.example.invalid/reclamos/SN-4127/evidencia/71',
    `${location.origin}/reclamos/SN-4127/evidencia/72`,
  ]
  for (const url of publicUrls) {
    worker.use(http.get(new URL(url, location.origin).href, ({ request }) => {
      photoRequests.push(request)
      return binaryResponse()
    }))
    await render(withFiles([{ ...files[0], url }]))
    await wait(() => photoRequests.some(request => request.url === new URL(url, location.origin).href),
      'No se solicitó la fotografía pública sintética')
    await wait(() => document.images.length === 1 && isRenderedImage(document.images[0]),
      'Se perdió la compatibilidad con fotografías públicas')
    const request = photoRequests.find(request => request.url === new URL(url, location.origin).href)!
    assert(request.method === 'GET', 'La fotografía pública no usó GET')
    assert(!request.headers.has('authorization'), 'Se filtró el bearer a una URL pública o absoluta')
    assert(!request.headers.has('referer'), 'Se filtró el fragmento de sesión a una URL pública')
  }

  const binaryCalls: { url: string; credentials: RequestCredentials; referrerPolicy: ReferrerPolicy; signal: boolean }[] = []
  const originalFetch = window.fetch
  window.fetch = (input, init) => {
    const request = new Request(input, init)
    if (/\/reclamos\/SN-4127\/evidencia\/(81|82|83)$/.test(new URL(request.url).pathname)) {
      binaryCalls.push({ url: request.url, credentials: request.credentials,
        referrerPolicy: request.referrerPolicy, signal: Boolean(init?.signal) })
    }
    return originalFetch(input, init)
  }
  try {
    for (const profile of ['revisor', 'operador']) {
      photoRequests.length = 0
      binaryCalls.length = 0
      worker.use(http.get('*/reclamos/SN-4127/evidencia/:id', async ({ request, params }) => {
        photoRequests.push(request)
        if (request.headers.get('authorization') !== `Bearer ${profile}-prueba`) {
          return HttpResponse.json({ title: 'Token ausente, vencido o emitido para otro reclamo' }, { status: 401 })
        }
        if (params.id === '82') {
          return HttpResponse.json({ title: 'No se pudo recuperar la fotografía.' }, { status: 502 })
        }
        if (params.id === '83') await delay(1000)
        return binaryResponse()
      }))
      await render(withFiles([
        { ...files[0], url: '/reclamos/SN-4127/evidencia/81' },
        { ...files[1], url: '/reclamos/SN-4127/evidencia/82' },
        { ...files[2], url: '/reclamos/SN-4127/evidencia/83' },
      ]), profile)
      await wait(() => photoRequests.length >= 3, 'No se solicitaron las tres rutas de evidencia')
      for (const id of [81, 82, 83]) {
        const expected = new URL(`/reclamos/SN-4127/evidencia/${id}`, location.origin).href
        const request = photoRequests.find(request => request.url === expected)
        assert(request?.method === 'GET', 'La fotografía autenticada no usó GET/ruta/base exactos')
        assert(request?.headers.get('authorization') === `Bearer ${profile}-prueba`,
          'La ruta relativa de evidencia no recibió el bearer de la sesión interna')
        assert(!request?.headers.has('referer'), 'La fotografía autenticada filtró referer')
        const call = binaryCalls.find(call => call.url === expected)
        assert(call?.credentials === 'omit', 'La recuperación binaria no omitió credenciales del navegador')
        assert(call?.referrerPolicy === 'no-referrer', 'La recuperación binaria no fijó no-referrer')
        assert(call?.signal, 'La recuperación binaria no recibió AbortSignal')
      }
      await wait(() => text().includes('Cargando fotografía: detalle-faro.png'), 'Falta carga binaria individual')
      await wait(() => text().includes('No se pudo recuperar paragolpes.jpg. Las otras fotografías siguen disponibles.'),
        'El error 502 del backend no mostró el estado individual existente')
      await wait(() => [...document.images].filter(isRenderedImage).length === 2,
        'El original binario no se dibujó o el error ocultó fotografías sanas')
      assert([...document.images].filter(isRenderedImage)
        .every(img => img.src.startsWith('blob:')), 'El original autenticado no se sirvió como blob local')
      if (profile === 'revisor') {
        assert(button('Aprobar reclamo').disabled, 'La carga binaria eliminó la justificación obligatoria')
      } else {
        assert(!button('Aprobar reclamo') && !button('Rechazar reclamo'), 'La carga binaria cambió los permisos')
      }
    }
  } finally {
    window.fetch = originalFetch
  }
  // Un transporte controlado que ignora abort tampoco debe publicar un blob obsoleto.
  // Los GET autenticados y sus cabeceras ya se comprueban arriba mediante MSW.
  const created: string[] = []
  const revoked: string[] = []
  const createObjectURL = URL.createObjectURL
  const revokeObjectURL = URL.revokeObjectURL
  const pendingPhotos: { signal: AbortSignal; token: string | null; finish: () => void }[] = []
  let consumed = 0
  URL.createObjectURL = blob => {
    const url = createObjectURL(blob)
    created.push(url)
    return url
  }
  URL.revokeObjectURL = url => { revoked.push(url); revokeObjectURL(url) }
  window.fetch = (input, init) => {
    const request = new Request(input, init)
    if (!/\/reclamos\/SN-4127\/evidencia\/(91|92)$/.test(new URL(request.url).pathname)) {
      return originalFetch(input, init)
    }
    return new Promise<Response>(resolve => {
      pendingPhotos.push({ signal: init!.signal!, token: request.headers.get('authorization'), finish: () => {
        const response = new Response(photoBytes.slice(0), { headers: { 'Content-Type': 'image/png' } })
        const blob = response.blob.bind(response)
        response.blob = async () => { const value = await blob(); consumed++; return value }
        resolve(response)
      } })
    })
  }
  const renderGallery = (id: number, token: string) => flushSync(() => root.render(
    <EvidenceGallery files={[{ ...files[0], url: `/reclamos/SN-4127/evidencia/${id}` }]} token={token} />,
  ))
  try {
    renderGallery(91, 'revisor-anterior')
    await wait(() => pendingPhotos.length === 1, 'No comenzó la petición anterior')
    renderGallery(91, 'revisor-actual')
    await wait(() => pendingPhotos.length === 2, 'El cambio de token no inició otra petición')
    assert(pendingPhotos[0].signal.aborted, 'El cambio de token no abortó la petición anterior')
    assert(pendingPhotos[1].token === 'Bearer revisor-actual', 'Se reutilizó el token anterior')
    pendingPhotos[1].finish()
    await wait(() => isRenderedImage(document.images[0]),
      'No se dibujó el blob de la sesión actual')
    const thumbnailUrl = document.images[0].src
    pendingPhotos[0].finish()
    await wait(() => consumed === 2, 'No terminó la respuesta obsoleta controlada')
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    assert(created.length === 1 && document.images[0].src === thumbnailUrl,
      'La respuesta obsoleta creó o reemplazó el blob actual')

    button('Ampliar frente-izq.jpg').click()
    await wait(() => pendingPhotos.length === 3, 'La ampliación no inició su recuperación independiente')
    pendingPhotos[2].finish()
    await wait(() => isRenderedImage(document.querySelector<HTMLImageElement>('dialog img')),
      'No se dibujó el blob del modal')
    const modalUrl = (document.querySelector('dialog img') as HTMLImageElement).src
    assert(modalUrl !== thumbnailUrl, 'Modal y miniatura no tienen propiedad independiente del blob')
    button('Cerrar ampliación').click()
    await wait(() => revoked.includes(modalUrl), 'Cerrar el modal no revocó su blob')
    assert(!revoked.includes(thumbnailUrl) && document.images[0].src === thumbnailUrl,
      'El cierre del modal revocó la miniatura viva')

    renderGallery(91, 'revisor-renovado')
    await wait(() => pendingPhotos.length === 4, 'No se recuperó la foto al renovar el token')
    assert(revoked.includes(thumbnailUrl), 'El cambio de token no revocó el blob anterior')
    pendingPhotos[3].finish()
    await wait(() => isRenderedImage(document.images[0]), 'No cargó la foto con token renovado')
    const renewedUrl = document.images[0].src
    renderGallery(92, 'revisor-renovado')
    await wait(() => pendingPhotos.length === 5, 'No comenzó la foto de reemplazo')
    assert(revoked.includes(renewedUrl), 'El reemplazo no revocó el blob anterior')
    pendingPhotos[4].finish()
    await wait(() => isRenderedImage(document.images[0]), 'No cargó la foto de reemplazo')
    flushSync(() => root.render(<></>))
    assert(created.every(url => revoked.filter(value => value === url).length === 1),
      'Los blobs no se revocaron exactamente una vez al reemplazar/cambiar token/desmontar')
    renderGallery(91, 'revisor-final')
    await wait(() => pendingPhotos.length === 6, 'No comenzó la petición final')
    flushSync(() => root.render(<></>))
    assert(pendingPhotos[5].signal.aborted, 'El desmontaje no abortó la petición pendiente')
    pendingPhotos[5].finish()
    await wait(() => consumed === 6, 'No terminó la respuesta posterior al desmontaje')
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    assert(created.length === 4 && document.images.length === 0,
      'La respuesta posterior al desmontaje creó un blob o actualizó la vista')
  } finally {
    flushSync(() => root.render(<></>))
    window.fetch = originalFetch
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL
  }
  window.testResult = 'PASS: MK-06/MSW — fotos públicas y binarias autenticadas, límite del bearer, abort/stale/revocación, carga/error, Enter/Tab/Escape/foco, justificación y permisos por estado/perfil.'
} catch (error) {
  window.testResult = `FAIL: ${error instanceof Error ? error.message : error}`
}
