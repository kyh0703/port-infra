import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { createHash, randomUUID } from 'node:crypto'
import { resolve } from 'node:path'

const arg = name => {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) throw new Error(`Missing ${name}`)
  return process.argv[index + 1]
}
const apiRoot = resolve(arg('--api-root')), webRoot = resolve(arg('--web-root'))
const output = resolve(arg('--output')), projectPath = resolve(arg('--project-key-file'))
const apiRequire = createRequire(resolve(apiRoot, 'package.json'))
const webRequire = createRequire(resolve(webRoot, 'package.json'))
const { AccessToken, RoomServiceClient } = apiRequire('livekit-server-sdk')
const sdkRequire = createRequire(apiRequire.resolve('livekit-server-sdk'))
const { SignalResponse } = sdkRequire('@livekit/protocol')
const { chromium } = webRequire('@playwright/test')
const project = JSON.parse(await readFile(projectPath, 'utf8'))
const key = project.Key ?? project.key, secret = project.Secret ?? project.secret
const url = project.URL ?? project.url
if (!/^wss:\/\//.test(url ?? '') || !key || !secret) throw new Error('Approved project missing')
const rooms = new RoomServiceClient(url.replace(/^ws/, 'http'), key, secret, { failover: false, requestTimeout: 10 })
const name = `ownyai-cloud-revocation-${randomUUID()}`
const identities = [`owned-caller-${randomUUID()}`, `owned-helper-${randomUUID()}`]
const bundle = await readFile(resolve(webRoot, 'node_modules/livekit-client/dist/livekit-client.umd.js'))
const server = createServer((request, response) => {
  response.setHeader('Cache-Control', 'no-store')
  if (request.url === '/sdk.js') { response.setHeader('Content-Type', 'text/javascript'); response.end(bundle) }
  else { response.setHeader('Content-Type', 'text/html'); response.end('<!doctype html><script src="/sdk.js"></script>') }
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const origin = `http://127.0.0.1:${server.address().port}`
let browser, created = false
const clockSamples = [], cases = []
const hash = value => createHash('sha256').update(value).digest('hex')
const result = { schemaVersion: 1, provider: 'livekit-cloud', projectId: project.ProjectId ?? project.projectId,
  observedAt: new Date().toISOString(), scenario: 'owned Cloud RTC refresh/absent revocation; no production sessions', cases, clockSamples }
try {
  const before = Date.now()
  const room = await rooms.createRoom({ name, emptyTimeout: 90, metadata: JSON.stringify({ purpose: 'owned-revocation-acceptance-probe' }) })
  created = true
  result.roomCreated = Boolean(room.sid)
  result.creationRoundTripMs = Date.now() - before
  browser = await chromium.launch({ headless: true })
  for (const identity of identities) {
    const page = await browser.newPage()
    page.on('websocket', socket => socket.on('framereceived', frame => {
      try {
        const data = typeof frame.payload === 'string'
          ? SignalResponse.fromJsonString(frame.payload) : SignalResponse.fromBinary(frame.payload)
        if (data.message.case === 'pongResp') {
          const pong = data.message.value
          clockSamples.push({ serverTimestampMs: Number(pong.timestamp), clientPingTimestampMs: Number(pong.lastPingTimestamp), receivedAtMs: Date.now() })
        }
      } catch { /* Only authoritative decoded pongs are usable. No raw frames retained. */ }
    }))
    await page.goto(origin)
    const token = new AccessToken(key, secret, { identity, ttl: 300 })
    token.addGrant({ roomJoin: true, room: name, canPublish: false, canPublishData: false, canSubscribe: true })
    const jwt = await token.toJwt()
    await page.evaluate(async ({ url, jwt }) => {
      window.room = new window.LivekitClient.Room()
      await window.room.connect(url, jwt, { autoSubscribe: false, maxRetries: 0, websocketTimeout: 10000, peerConnectionTimeout: 10000 })
    }, { url, jwt })
    const samplesBefore = clockSamples.length
    await page.evaluate(() => window.room.engine.client.sendPing())
    const pingDeadline = Date.now() + 5000
    while (clockSamples.length === samplesBefore && Date.now() < pingDeadline)
      await new Promise(resolve => setTimeout(resolve, 100))
    if (clockSamples.length === samplesBefore) throw new Error('Authoritative Cloud pong timestamp was not observed')
    const native = await rooms.getParticipant(name, identity)
    const initial = await page.evaluate(() => window.room.engine.token)
    await rooms.updateParticipant(name, identity, { metadata: `owned-refresh-${randomUUID()}` })
    await page.waitForFunction(previous => window.room.engine?.token && window.room.engine.token !== previous, initial, { timeout: 10000 })
    const refreshed = await page.evaluate(() => window.room.engine.token)
    const claims = JSON.parse(Buffer.from(refreshed.split('.')[1], 'base64url').toString())
    const entry = { identityHash: hash(identity), participantSidObserved: Boolean(native.sid),
      refreshedTokenObserved: refreshed !== initial, refreshedNbf: claims.nbf, refreshedTokenHash: hash(refreshed) }
    // Disconnect genuinely, then obtain two positive explicit-cutoff ACKs for an absent identity.
    await page.evaluate(() => window.room.disconnect())
    const absent = await rooms.listParticipants(name)
    if (absent.some(value => value.identity === identity)) throw new Error('Owned identity did not become absent')
    const firstStarted = Date.now()
    await rooms.removeParticipant(name, identity, { revokeTokenTs: BigInt(Math.floor(firstStarted / 1000)) })
    const firstAck = Date.now()
    const cutoff = BigInt(Math.floor(Math.max(firstAck, Number(claims.nbf) * 1000) / 1000) + 2)
    const waitMs = Number(cutoff) * 1000 + 500 - Date.now()
    if (waitMs > 5000 || waitMs < -1000) throw new Error('Provider/client clock is outside bounded probe window')
    if (waitMs > 0) await new Promise(resolve => setTimeout(resolve, waitMs))
    const secondStarted = Date.now()
    await rooms.removeParticipant(name, identity, { revokeTokenTs: cutoff })
    entry.firstAckMs = firstAck - firstStarted
    entry.secondAckMs = Date.now() - secondStarted
    entry.explicitUnixSecondsCutoff = cutoff.toString()
    const rejection = await page.evaluate(async ({ url, jwt }) => {
      const attempt = new window.LivekitClient.Room()
      try { await attempt.connect(url, jwt, { autoSubscribe: false, maxRetries: 0, websocketTimeout: 10000, peerConnectionTimeout: 10000 }); await attempt.disconnect(); return { rejected: false } }
      catch (error) { return { rejected: true, reason: error.reason, status: error.status, code: error.code, message: String(error.message).replace(/access_token=[^\s&]+/g, 'access_token=[redacted]') } }
    }, { url, jwt: refreshed })
    entry.cachedRefreshedToken = { rejected: rejection.rejected, reason: rejection.reason, status: rejection.status, code: rejection.code }
    if (!rejection.rejected || !/401|unauthor|permission|token|auth/i.test(rejection.message ?? '')) throw new Error('No positive token-specific denial was observed')
    cases.push(entry)
    await page.close()
  }
  result.absentIdentityRevoked = cases.every(value => value.cachedRefreshedToken.rejected)
  result.recentlyRefreshedTokenRejected = cases.every(value => value.refreshedTokenObserved && value.cachedRefreshedToken.rejected)
  result.allOwnedInheritedIdentitiesCovered = cases.length === identities.length
  result.status = 'observed'
} catch (error) {
  result.status = 'unproven'
  // Errors may carry signal URLs with JWTs. Preserve only a bounded classification.
  result.failure = String(error.message).replace(/https?:\/\/[^\s]+/g, '[URL]').replace(/eyJ[A-Za-z0-9_.-]+/g, '[JWT]').slice(0, 250)
  process.exitCode = 1
} finally {
  if (browser) await browser.close()
  if (created) { await rooms.deleteRoom(name); result.ownedRoomRemoved = true }
  await new Promise(resolve => server.close(resolve))
  result.finishedAt = new Date().toISOString()
  await writeFile(output, JSON.stringify(result, null, 2) + '\n', { mode: 0o600, flag: 'wx' })
  console.log(JSON.stringify({ status: result.status, coveredIdentities: cases.length, clockSamples: clockSamples.length, ownedRoomRemoved: result.ownedRoomRemoved, failure: result.failure }))
}
