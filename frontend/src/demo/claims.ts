// Los reclamos de demostración del prototipo desplegado.
//
// El asegurado no tiene lista propia en la API y no la va a tener: RF-47 le
// emite una capacidad por reclamo y ninguna identidad, de modo que «mis
// reclamos» es, por diseño, el conjunto de enlaces que su navegador guardó. Un
// visitante que llega por primera vez no guardó ninguno, y sin estos la pantalla
// abre vacía y el prototipo no se puede recorrer.
//
// PROCEDENCIA. Los casos son reales y no fabricados: los cargó
// `PFI-Backend/scripts/seed-demo-claims.py` por POST /reclamos con parches del
// conjunto de prueba de CarDD, y los procesó el sistema entero, extractor ONNX,
// cabeza, prueba y transiciones. Lo que no son es siniestros de una persona, y
// la pantalla que los lista lo dice.
//
// Los tokens están acá en claro a propósito. Son capacidades sobre estos cuatro
// reclamos de demostración y sobre ninguna otra cosa; publicarlos es lo que
// permite que cualquiera abra la demo, que es para lo que existen. Un reclamo de
// una persona real nunca entra a este archivo.
//
// Se regeneran corriendo la siembra y copiando `number` y `accessToken` de su
// salida. Estos cuatro se cargaron el 2026-08-24 contra el api de producción, y
// los estados que muestran son los que el sistema les dio: dos derivados y dos
// cerrados, uno por la liquidación del operador y otro por la resolución del
// revisor, las dos ejecutadas por sus rutas.

import type { SavedClaim } from "@/lib/session"

export const DEMO_CLAIMS: SavedClaim[] = [
  // Derivado por monto: $1.850.000 supera el límite de RF-34.
  { number: "SN-4151", accessToken: "odZumR1XaAVZ4CqL2cI9Jp3NsqaphTATHg3V4zCvR6Y" },
  // Aprobable liquidado por el operador (CU-06).
  { number: "SN-4152", accessToken: "hx7J11XkivsRHUmBUOH2ZlaJw7J9qlfvQE1Gb82U-ug" },
  // Derivado por confianza y resuelto en favor por el revisor (CU-05).
  { number: "SN-4153", accessToken: "UT9KMhytyUT1t1pOHFyMM_C1NxOj5y9P-XpHsO8hHno" },
  // Análisis favorable, esperando todavía la liquidación de RF-37.
  { number: "SN-4154", accessToken: "cIEoDkvH-Dq1xqhtTjk2qMyoI3BTaSzNFult7wLb-yM" },
]

/** Para que la lista pueda declarar de dónde salieron sin repetir el texto. */
export const DEMO_NOTICE =
  "Los reclamos de esta lista son casos de demostración: se cargaron con " +
  "imágenes del conjunto CarDD y el sistema los procesó como a cualquier otro. " +
  "No son siniestros de una persona."
