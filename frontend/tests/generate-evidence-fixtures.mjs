import { deflateSync } from 'node:zlib'
import { mkdir, writeFile } from 'node:fs/promises'

// PNG RGB sin metadatos: ilustraciones geométricas, nunca fotografías reales.
const directory = new URL('../public/evidence-fixtures/', import.meta.url)
await mkdir(directory, { recursive: true })
function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type), data])
  let crc = 0xffffffff
  for (const byte of body) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0)
  }
  const header = Buffer.alloc(4)
  header.writeUInt32BE(data.length)
  const checksum = Buffer.alloc(4)
  checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0)
  return Buffer.concat([header, body, checksum])
}
for (const [name, width, height, accent] of [
  ['vehicle-front.png', 1200, 800, [40, 110, 180]],
  ['vehicle-rear.png', 800, 1200, [170, 85, 40]],
  ['vehicle-detail.png', 1200, 900, [50, 130, 90]],
]) {
  const pixels = Buffer.alloc((width * 3 + 1) * height)
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const u = x / width, v = y / height
      let color = [225, 231, 238]
      if (v > 0.8) color = [95, 105, 115]
      if (u > 0.18 && u < 0.82 && v > 0.38 && v < 0.73) color = accent
      if (u > 0.3 && u < 0.7 && v > 0.25 && v < 0.4) color = [55, 70, 85]
      if (((u - 0.3) ** 2 + (v - 0.74) ** 2 < 0.004) ||
          ((u - 0.7) ** 2 + (v - 0.74) ** 2 < 0.004)) color = [25, 30, 35]
      if (u > 0.5 && u < 0.7 && Math.abs(v - (0.8 - u * 0.3)) < 0.006) color = [250, 220, 90]
      const offset = y * (width * 3 + 1) + 1 + x * 3
      pixels.set(color, offset)
    }
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header[8] = 8
  header[9] = 2
  await writeFile(new URL(name, directory), Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0)),
  ]))
  console.log(`PNG sintético: ${name} (${width}×${height})`)
}
