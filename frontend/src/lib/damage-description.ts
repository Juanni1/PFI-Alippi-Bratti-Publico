export const DAMAGE_DESCRIPTION_MAX_CODE_POINTS = 2000

// Replica `str.strip()` del backend: conserva U+FEFF y quita los demás
// espacios Unicode que ese normalizador reconoce en los extremos.
const EDGE_WHITESPACE =
  /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu

export function normalizeDamageDescription(value: string): string {
  return value.replace(/\r\n?/g, "\n").replace(EDGE_WHITESPACE, "")
}

// Cuenta puntos de código sobre UTF-16 sin materializar un arreglo.
export function countCodePoints(value: string): number {
  let count = 0
  for (let index = 0; index < value.length; index += 1) {
    const leading = value.charCodeAt(index)
    if (
      leading >= 0xd800 &&
      leading <= 0xdbff &&
      index + 1 < value.length &&
      value.charCodeAt(index + 1) >= 0xdc00 &&
      value.charCodeAt(index + 1) <= 0xdfff
    ) {
      index += 1
    }
    count += 1
  }
  return count
}

// Recibe el texto ya normalizado y su longitud para compartir el cálculo entre
// el formulario y MSW.
export function validateNormalizedDamageDescription(
  normalized: string,
  codePointLength: number,
): string | null {
  if (!normalized) return "Describí el daño antes de enviar."
  if (codePointLength > DAMAGE_DESCRIPTION_MAX_CODE_POINTS) {
    return `La descripción del daño no puede superar los ${DAMAGE_DESCRIPTION_MAX_CODE_POINTS} caracteres.`
  }
  return null
}
