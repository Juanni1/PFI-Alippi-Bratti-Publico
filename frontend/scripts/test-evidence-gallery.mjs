import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { createServer } from 'vite'

// Runner local: sin dependencias de automatización ni perfiles personales.
const scratch = await mkdtemp(path.join(tmpdir(), 'pfi-gallery-'))
let browser
let server
let socket
// Sólo metadatos acotados: nunca mensajes, URLs, cabeceras ni objetos de página.
const pageErrors = []
let pageErrorCount = 0
const recordPageError = (kind, details = {}) => {
  pageErrorCount++
  if (pageErrors.length < 8) pageErrors.push({ kind, ...details })
}
const exceptionMetadata = details => ({
  line: details.lineNumber,
  column: details.columnNumber,
  exceptionType: ['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError']
    .includes(details.exception?.className) ? details.exception.className : 'otro',
})
const keyDefinitions = {
  Enter: { key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 },
  Tab: { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 },
  Escape: { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 },
}
try {
  server = await createServer({
    envDir: false,
    cacheDir: path.join(scratch, 'vite'),
    server: { host: '127.0.0.1', port: 0 },
    define: { 'import.meta.env.VITE_API_URL': '""' },
  })
  await server.listen()
  const origin = server.resolvedUrls.local[0]
  browser = spawn('/usr/bin/chromium', [
    '--headless', '--no-sandbox', '--disable-dev-shm-usage',
    '--remote-debugging-port=0', `--user-data-dir=${scratch}/browser`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] })
  const endpoint = await new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error(`Chromium no anunció CDP en 10 s: ${output}`)), 10000)
    browser.stderr.on('data', chunk => {
      output += chunk
      const match = output.match(/DevTools listening on (ws:\/\/\S+)/)
      if (match) { clearTimeout(timer); resolve(match[1]) }
    })
    browser.on('error', error => { clearTimeout(timer); reject(error) })
    browser.on('exit', code => { clearTimeout(timer); reject(new Error(`Chromium terminó: ${code}; ${output}`)) })
  })
  const port = new URL(endpoint).port
  const targets = await fetch(`http://127.0.0.1:${port}/json`).then(r => r.json())
  socket = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl)
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }))
  let nextId = 0
  const pending = new Map()
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
  socket.addEventListener('message', async event => {
    const message = JSON.parse(event.data)
    if (message.id) {
      const entry = pending.get(message.id)
      pending.delete(message.id)
      if (message.error) entry.reject(new Error(`CDP rechazó el comando (${message.error.code})`))
      else {
        if (message.result?.exceptionDetails) {
          recordPageError('evaluate', exceptionMetadata(message.result.exceptionDetails))
        }
        entry.resolve(message.result)
      }
    }
    if (message.method === 'Runtime.exceptionThrown') {
      recordPageError('exceptionThrown', exceptionMetadata(message.params.exceptionDetails))
    }
    if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
      recordPageError('console.error')
    }
    if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') {
      recordPageError('pageLog', { line: message.params.entry.lineNumber })
    }
    if (message.method === 'Runtime.bindingCalled') {
      const { key, id } = JSON.parse(message.params.payload)
      if (!Number.isSafeInteger(id)) return
      let failed = false
      try {
        const definition = Object.hasOwn(keyDefinitions, key) ? keyDefinitions[key] : undefined
        if (!definition) throw new Error('Tecla no admitida por el runner')
        await call('Input.dispatchKeyEvent', {
          ...definition,
          type: key === 'Enter' ? 'keyDown' : 'rawKeyDown',
          ...(key === 'Enter' ? { text: '\r', unmodifiedText: '\r' } : {}),
        })
        await call('Input.dispatchKeyEvent', { ...definition, type: 'keyUp' })
      } catch {
        failed = true
        recordPageError('keyInjection')
      }
      try {
        await call('Runtime.evaluate', { expression: `window.keyDone(${id}, ${failed})` })
      } catch {
        recordPageError('keyAcknowledgement')
      }
    }
  })
  await call('Runtime.enable')
  await call('Log.enable')
  await call('Runtime.addBinding', { name: 'browserKey' })
  await call('Page.navigate', { url: `${origin}tests/evidence-gallery.html` })
  const deadline = Date.now() + 45000
  let result
  while (Date.now() < deadline) {
    const response = await call('Runtime.evaluate', {
      expression: 'window.testResult', returnByValue: true,
    })
    result = response.result.value
    if (result) break
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  if (!result) throw new Error('Tiempo agotado: 45 s sin resultado de la prueba')
  console.log(result)
  if (!result.startsWith('PASS')) process.exitCode = 1
} catch (error) {
  console.error(error)
  process.exitCode = 1
} finally {
  console.error(`Diagnóstico de página: ${JSON.stringify({
    count: pageErrorCount, first: pageErrors, omitted: pageErrorCount - pageErrors.length,
  })}`)
  socket?.close()
  if (browser && browser.exitCode === null) {
    browser.kill('SIGTERM')
    await new Promise(resolve => browser.once('exit', resolve))
  }
  await server?.close()
  await rm(scratch, { recursive: true, force: true })
}
