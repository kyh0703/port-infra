import { writeFileSync } from 'node:fs'
import { createInterface } from 'node:readline'
import { createRequire } from 'node:module'
const require = createRequire('/app/package.json')
const { AgentServer, ServerOptions, WorkerPermissions, initializeLogger } = require('@livekit/agents')
const { AccessToken } = require('livekit-server-sdk')

const lines = createInterface({ input: process.stdin })
const input = JSON.parse(await new Promise(resolve => lines.once('line', resolve)))
lines.close()
initializeLogger({ pretty: false, level: 'warn' })
const server = new AgentServer(new ServerOptions({
  agent: '/probe/agent.mjs', agentName: input.agentName, production: true,
  wsURL: input.URL, apiKey: input.Key, apiSecret: input.Secret,
  host: '127.0.0.1', port: 8083, numIdleProcesses: 0,
  shutdownProcessTimeout: 5000, initializeProcessTimeout: 20000,
  permissions: new WorkerPermissions(false, false, false, false),
  requestFunc: async request => {
    const metadata = JSON.parse(request.job.metadata)
    if (metadata.probeNonce !== input.probeNonce || metadata.incarnation !== input.incarnation) { await request.reject(); return }
    await request.accept('', input.participantIdentity, '')
  },
  prepareJob: async info => {
    writeFileSync(`/evidence/native-job-${info.job.id}.sanitized.json`, JSON.stringify({
      jobId: info.job.id, dispatchId: info.job.dispatchId,
      roomName: info.job.room.name, roomSid: info.job.room.sid,
      participantIdentity: input.participantIdentity, incarnation: input.incarnation,
      probeNonce: input.probeNonce, observedAt: new Date().toISOString(),
    }), { mode: 0o600, flag: 'wx' })
    const access = new AccessToken(input.Key, input.Secret, { identity: input.participantIdentity, ttl: 120 })
    access.kind = 'agent'
    access.addGrant({ roomJoin: true, room: info.job.room.name, agent: true, canPublish: false, canPublishData: false, canSubscribe: false })
    return { ...info, runtimeContext: { privateProbeToken: await access.toJwt(), ownedIncarnation: input.incarnation } }
  },
}))
server.event.on('worker_registered', workerId => {
  writeFileSync('/evidence/native-registration.sanitized.json', JSON.stringify({ workerId, agentName: input.agentName, incarnation: input.incarnation, probeNonce: input.probeNonce, observedAt: new Date().toISOString() }), { mode: 0o600 })
})
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => { void server.close().then(() => process.exit(0), () => process.exit(1)) })
await server.run()
