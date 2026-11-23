import { useEffect, useId, useRef, useState } from "react"
import type { ReviewerClaim } from "@/types"
import { apiGetEvidencePhoto, isEvidencePhotoPath } from "@/lib/api"

type EvidenceFile = ReviewerClaim["evidence"]["files"][number]

function EvidencePhoto({ file, token, enlarged = false }: {
  file: EvidenceFile; token: string; enlarged?: boolean
}) {
  const [status, setStatus] = useState<"loading" | "loaded" | "error">("loading")
  const [objectUrl, setObjectUrl] = useState<string>()
  const authenticated = Boolean(file.url && isEvidencePhotoPath(file.url))

  useEffect(() => {
    if (!file.url || !authenticated) return
    const controller = new AbortController()
    let active = true
    let ownedUrl: string | undefined
    const timeout = window.setTimeout(() => {
      controller.abort()
      if (active) setStatus("error")
    }, 15000)
    void apiGetEvidencePhoto(file.url, token, controller.signal).then(blob => {
      if (!active || controller.signal.aborted) return
      ownedUrl = URL.createObjectURL(blob)
      setObjectUrl(ownedUrl)
    }).catch(() => {
      if (active && !controller.signal.aborted) setStatus("error")
    }).finally(() => window.clearTimeout(timeout))
    return () => {
      active = false
      controller.abort()
      window.clearTimeout(timeout)
      if (ownedUrl) URL.revokeObjectURL(ownedUrl)
    }
  }, [file.url, token, authenticated])

  if (!file.url) {
    return <p className="p-3 text-sm">URL no disponible para {file.filename}.</p>
  }

  return (
    <div aria-busy={status === "loading"}>
      {status === "loading" ? (
        <p role="status" className="p-3 text-sm">Cargando fotografía: {file.filename}…</p>
      ) : status === "error" ? (
        <p role="status" className="p-3 text-sm">
          No se pudo recuperar {file.filename}. Las otras fotografías siguen disponibles.
        </p>
      ) : null}
      {(!authenticated || objectUrl) ? <img
        src={authenticated ? objectUrl : file.url}
        alt={`Fotografía original del reclamo: ${file.filename}`}
        // Los recursos públicos siguen anónimos; los protegidos usan un blob local.
        crossOrigin="anonymous"
        referrerPolicy="no-referrer"
        onLoad={() => setStatus("loaded")}
        onError={() => setStatus("error")}
        hidden={status !== "loaded"}
        style={enlarged
          ? { width: "auto", height: "auto", maxWidth: "100%", maxHeight: "70vh", objectFit: "contain", margin: "auto" }
          : { width: "auto", height: "auto", maxWidth: "100%", maxHeight: "16rem", objectFit: "contain", margin: "auto" }}
      /> : null}
    </div>
  )
}

function EvidenceCard({ file, token }: { file: EvidenceFile; token: string }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const [isOpen, setIsOpen] = useState(false)

  return (
    <li className="min-w-0 rounded-md border p-3">
      <p className="mb-2 break-words text-sm font-medium">{file.filename}</p>
      <EvidencePhoto key={`${file.url}-${token}`} file={file} token={token} />
      {file.url ? (
        <>
          <button
            ref={trigger}
            type="button"
            aria-haspopup="dialog"
            className="mt-3 rounded-md border px-3 py-2 text-sm underline-offset-4 hover:underline focus-visible:outline-2"
            onClick={() => {
              setIsOpen(true)
              dialog.current?.showModal()
            }}
          >
            Ampliar {file.filename}
          </button>
          {/* El diálogo nativo encierra el foco y cierra con Escape. */}
          <dialog
            ref={dialog}
            aria-labelledby={titleId}
            className="fixed inset-0 m-auto max-h-[95vh] w-[95vw] max-w-6xl overflow-auto rounded-lg border bg-background p-5 text-foreground backdrop:bg-black/70"
            onClose={() => {
              setIsOpen(false)
              trigger.current?.focus()
            }}
          >
            <div className="mb-4 flex items-center justify-between gap-4">
              <h2 id={titleId} className="break-words font-medium">{file.filename}</h2>
              <button
                type="button"
                autoFocus
                onClick={() => dialog.current?.close()}
                className="shrink-0 rounded-md border px-3 py-2 text-sm focus-visible:outline-2"
              >
                Cerrar ampliación
              </button>
            </div>
            {isOpen ? <EvidencePhoto key={`${file.url}-${token}`} file={file} token={token} enlarged /> : null}
          </dialog>
        </>
      ) : null}
    </li>
  )
}

export function EvidenceGallery({ files, token }: { files: EvidenceFile[]; token: string }) {
  return (
    <div className="mt-3">
      <p className="mb-3 text-sm text-muted-foreground">
        Fotografías originales disponibles para revisor y operador mediante la API.
        La publicación existente en IPFS sigue siendo pública. Mostrar todas las fotos
        no significa que todas hayan sido analizadas: la inferencia ONNX actual utiliza solo la primera.
      </p>
      {files.length === 0 ? (
        <p role="status" className="text-sm">No hay fotografías disponibles para este reclamo.</p>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {files.map((file, index) => (
            <EvidenceCard key={`${index}-${file.filename}-${file.url ?? ""}`} file={file} token={token} />
          ))}
        </ul>
      )}
    </div>
  )
}
