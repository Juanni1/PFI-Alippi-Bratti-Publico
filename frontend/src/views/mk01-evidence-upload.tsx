// MK-01 — Carga de evidencia.
//
// El reclamante aporta evidencia y relato. El operador puede registrar en su
// nombre y debe transcribir ese relato sin agregar diagnósticos; el servidor
// conserva la identidad de la sesión interna por separado.
//
// El rechazo opera por archivo y no por envío, de modo que corregir el que falla
// no obliga a recargar los que ya pasaron. Los límites se muestran antes de
// elegir archivos y se validan con la misma normalización que aplica la API.

import { useMemo, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import type { Configuration, PublicConfiguration, RegistrationReceipt } from "@/types"
import {
  DAMAGE_DESCRIPTION_MAX_CODE_POINTS,
  countCodePoints,
  normalizeDamageDescription,
  validateNormalizedDamageDescription,
} from "@/lib/damage-description"
import { apiGet, apiPost } from "@/lib/api"
import { useSession, useStaffToken } from "@/lib/session"
import {
  Note,
  Screen,
  Section,
  Unmeasured,
  describeError,
  formatMoney,
} from "@/components/screen"

type Candidate = {
  file: File
  rejection: { reason: string; message: string } | null
}

// Un megabyte son 1024 KiB y no 1000 KB, que es como lo cuenta el backend al
// fijar el límite. Con la división decimal la pantalla rechazaba archivos de
// entre 10 MiB y 10,49 MB que la API habría aceptado, y el reclamante veía un
// rechazo que el sistema no sostenía.
const MB = 1024 * 1024

function validate(
  file: File,
  config?: Pick<Configuration, "acceptedFormats" | "maxFileSizeMb">,
): Candidate["rejection"] {
  const formats = config?.acceptedFormats ?? ["image/jpeg", "image/png"]
  if (!formats.includes(file.type as (typeof formats)[number])) {
    const extension = file.name.split(".").pop()?.toUpperCase() ?? "El archivo"
    return {
      reason: "formato_no_admitido",
      message: `${extension} no admitido. Subí fotos en JPEG o PNG.`,
    }
  }
  // El máximo de tamaño es configuración y llega por GET /configuracion, no
  // como constante de la pantalla (RNF-29). Mientras viajó nulo la pantalla no
  // rechazaba por peso, porque inventar el umbral para poder validar sería
  // exactamente lo que la regla prohíbe; quedó fijado en 10 MB el 2026-08-23.
  const limit = config?.maxFileSizeMb
  if (limit?.value != null && limit.measured) {
    const megabytes = file.size / MB
    if (megabytes > limit.value) {
      return {
        reason: "excede_tamano_maximo",
        message: `Pesa ${megabytes.toFixed(1)} MB y el tope es ${limit.value} MB.`,
      }
    }
  }
  return null
}

export function MK01EvidenceUpload() {
  const navigate = useNavigate()
  const { addClaim, profile } = useSession()
  const isOperator = profile === "operador"
  const sessionToken = useStaffToken()
  const operatorToken = isOperator ? sessionToken?.trim() || null : null
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])
  const [amount, setAmount] = useState("")
  const [damageDescription, setDamageDescription] = useState("")
  const [damageDescriptionTouched, setDamageDescriptionTouched] = useState(false)
  // Antes del registro solo se piden los límites públicos del formulario.
  // El operador conserva la configuración protegida y su sesión interna.
  const { data: config } = useQuery<Configuration | PublicConfiguration>({
    queryKey: ["configuracion", isOperator ? operatorToken : "publica"],
    queryFn: () => isOperator
      ? apiGet<Configuration>("/configuracion", operatorToken)
      : apiGet<PublicConfiguration>("/configuracion/publica"),
    enabled: !isOperator || Boolean(operatorToken),
    retry: false,
  })

  const maxFiles = config?.maxFiles.value ?? 5
  const candidates = useMemo(
    () =>
      selectedFiles.map((file) => ({
        file,
        rejection: validate(file, config),
      })),
    [selectedFiles, config],
  )
  const valid = useMemo(() => candidates.filter((c) => !c.rejection), [candidates])
  const rejected = candidates.filter((c) => c.rejection)
  const normalizedDamageDescription = normalizeDamageDescription(damageDescription)
  const damageDescriptionLength = countCodePoints(normalizedDamageDescription)
  const damageDescriptionError = validateNormalizedDamageDescription(
    normalizedDamageDescription,
    damageDescriptionLength,
  )
  const showDamageDescriptionError =
    damageDescriptionTouched && damageDescriptionError !== null

  const submit = useMutation({
    mutationFn: async () => {
      if (isOperator && !operatorToken) {
        throw new Error("La sesión de operador necesita un token para registrar el reclamo.")
      }
      if (damageDescriptionError) throw new Error(damageDescriptionError)
      const form = new FormData()
      valid.forEach((candidate) => form.append("photos", candidate.file))
      form.append("declaredAmount", amount)
      form.append("damageDescription", normalizedDamageDescription)
      return apiPost<RegistrationReceipt>("/reclamos", form, operatorToken)
    },
    // El número y el token de capacidad quedan guardados acá y en ningún otro
    // lado: la constancia se muestra una sola vez y el contrato no tiene un
    // recurso que la vuelva a servir. La lista conserva solo esa capacidad.
    onSuccess: (receipt) => {
      addClaim({ number: receipt.number, accessToken: receipt.accessToken })
      navigate(`/constancia/${receipt.number}`, { state: receipt })
    },
  })

  function addFiles(files: FileList | null) {
    if (!files) return
    const next = Array.from(files)
    setSelectedFiles((current) => [...current, ...next].slice(0, maxFiles + rejected.length))
  }

  // RNF-36: el envío queda inhabilitado y la instrucción para destrabarlo va al
  // lado del control, en vez de aparecer como error después del intento.
  const canSubmit =
    valid.length >= 1 &&
    valid.length <= maxFiles &&
    amount.trim() !== "" &&
    damageDescriptionError === null &&
    (!isOperator || Boolean(operatorToken))

  return (
    <Screen
      context="Aseguradora"
      session={
        isOperator
          ? operatorToken
            ? "Sesión de operador"
            : "Sesión sin token"
          : "Póliza AB-3391"
      }
      title={isOperator ? "Registrar reclamo para un asegurado" : "Cargá las fotos del daño"}
      subtitle="Paso 1 de 2"
    >
      {isOperator ? (
        <Section>
          <Note>
            Cargás en nombre del asegurado. Transcribí su relato textual, sin diagnósticos propios.
          </Note>
        </Section>
      ) : null}
      <Section>
        {/* 2 — Zona de carga. */}
        <label className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-10 text-center">
          <span className="text-sm font-medium">Arrastrá hasta {maxFiles} fotos del daño</span>
          <span className="rounded-md border px-3 py-1.5 text-sm">Elegir fotos</span>
          <input
            type="file"
            multiple
            accept="image/jpeg,image/png"
            className="hidden"
            onChange={(event) => addFiles(event.target.files)}
          />
        </label>

        {/* 3 — Límites declarados antes de elegir. El máximo por archivo sigue
            sin fijarse y se dibuja con su marca, igual que en la lámina. */}
        <div className="mt-3">
          <Note>
            JPEG o PNG · entre 1 y {maxFiles} fotos · hasta{" "}
            {config ? <Unmeasured value={config.maxFileSizeMb} /> : "…"} MB cada una
          </Note>
        </div>

        {/* 4, 9 y 10 — Lista de archivos con su causa concreta por fila. */}
        {candidates.length > 0 ? (
          <ul className="mt-4 divide-y text-sm">
            {candidates.map((candidate, index) => (
              <li
                key={`${candidate.file.name}-${index}`}
                className="flex items-start justify-between gap-4 py-2"
              >
                <div>
                  <p className={candidate.rejection ? "text-muted-foreground" : ""}>
                    {candidate.file.name}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {candidate.rejection
                      ? candidate.rejection.message
                      : `${(candidate.file.size / MB).toFixed(1)} MB`}
                  </p>
                </div>
                <button
                  type="button"
                  className="text-xs underline underline-offset-4"
                  onClick={() =>
                    setSelectedFiles((current) => current.filter((_, i) => i !== index))
                  }
                >
                  quitar
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        {/* 5 y 11 — El contador distingue archivos válidos de archivos cargados:
            el mínimo de uno y el máximo de cinco se cuentan sobre los válidos. */}
        {candidates.length > 0 ? (
          <p className="mt-3 text-xs text-muted-foreground">
            {valid.length} de {maxFiles} fotos{rejected.length > 0 ? " válidas" : ""}
          </p>
        ) : null}
      </Section>

      {/* 6 — Monto estimado, con la ayuda que enuncia quién lo declara y qué
          dispara. Lo declara el reclamante porque el modelo clasifica tipo de
          daño y no estima costo de reparación. */}
      <Section>
        <label className="block text-sm">
          <span className="font-medium">Monto estimado de reparación</span>
          <input
            className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
            inputMode="numeric"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
          />
        </label>
        <div className="mt-2">
          <Note>
            {isOperator
              ? "Transcribí lo que declaró el asegurado. Si supera el límite configurado "
              : "Ingresá un valor estimado. Si supera el límite configurado "}
            {config ? (
              <>
                (<Unmeasured value={config.amountLimit} format={formatMoney} />)
              </>
            ) : null}
            {isOperator
              ? ", pasa a revisión de un analista."
              : ", un analista revisa el caso antes de liquidar."}
          </Note>
        </div>
      </Section>
      {/* El relato del reclamante se conserva fuera de la cadena y no es un diagnóstico. */}
      <Section>
        <label htmlFor="damage-description" className="block text-sm">
          <span className="font-medium">
            {isOperator
              ? "Relato declarado por el asegurado (obligatorio)"
              : "Descripción del daño (obligatoria)"}
          </span>
        </label>
        <textarea
          id="damage-description"
          required
          rows={4}
          value={damageDescription}
          onChange={(event) => {
            setDamageDescription(event.target.value)
            setDamageDescriptionTouched(true)
          }}
          onBlur={() => setDamageDescriptionTouched(true)}
          aria-invalid={showDamageDescriptionError ? true : undefined}
          aria-describedby={
            showDamageDescriptionError
              ? "damage-description-guidance damage-description-count damage-description-error"
              : "damage-description-guidance damage-description-count"
          }
          className={`mt-1 w-full resize-y rounded-md border px-3 py-2 text-sm ${
            showDamageDescriptionError ? "border-destructive" : ""
          }`}
        />
        <p
          id="damage-description-guidance"
          className="mt-2 text-xs leading-relaxed text-muted-foreground"
        >
          {isOperator
            ? "Transcribí el relato textual del asegurado. No agregues diagnósticos ni datos personales."
            : "Contá qué parte del auto se dañó y qué pasó. No incluyas nombres, patentes ni datos personales."}
        </p>
        <p id="damage-description-count" className="mt-1 text-xs text-muted-foreground">
          {damageDescriptionLength} / {DAMAGE_DESCRIPTION_MAX_CODE_POINTS} caracteres
        </p>
        {showDamageDescriptionError ? (
          <p id="damage-description-error" role="alert" className="mt-1 text-sm text-destructive">
            {damageDescriptionError}
          </p>
        ) : null}
      </Section>

      {/* 7 — Aviso de publicación (RNF-23). */}
      <Section>
        <Note>
          No incluyas personas, patentes ni documentos en las fotos. Las imágenes se
          publican en una red abierta para que el resultado sea auditable.
        </Note>
      </Section>

      {/* 8 y 12 — Acciones. */}
      <Section>
        <div className="flex flex-wrap items-center gap-4">
          <button
            type="button"
            disabled={!canSubmit || submit.isPending}
            onClick={() => submit.mutate()}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
          >
            {submit.isPending ? "Enviando…" : isOperator ? "Registrar reclamo" : "Enviar reclamo"}
          </button>
          <button
            type="button"
            className="text-sm underline underline-offset-4"
            onClick={() => navigate(isOperator ? "/monitor" : "/mis-reclamos")}
          >
            Cancelar
          </button>
        </div>
        {isOperator && !operatorToken ? (
          <div className="mt-2">
            <Note>
              La sesión de operador no tiene token. Completá el token de la sesión interna en
              la navegación para habilitar el registro.
            </Note>
          </div>
        ) : !canSubmit ? (
          <div className="mt-2">
            <Note>
              Para continuar cargá al menos una foto válida, el monto estimado y la descripción.
            </Note>
          </div>
        ) : null}
        {submit.error ? (
          <div role="alert" className="mt-3">
            <p className="text-sm text-destructive">
              {describeError(submit.error, {
                401: isOperator
                  ? "Sesión de operador vencida o no válida."
                  : "No se pudo validar el acceso al registro.",
                403: "Solo el operador puede registrar en nombre del asegurado.",
                422: "Revisá los archivos marcados y los campos obligatorios antes de reintentar.",
                502: "El servidor no pudo procesar el reclamo. Probá de nuevo.",
              })}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Las fotos, el monto y el relato se conservan en esta pantalla.
            </p>
          </div>
        ) : null}
      </Section>
    </Screen>
  )
}
