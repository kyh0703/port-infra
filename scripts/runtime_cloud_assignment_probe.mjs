import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'

const root = resolve(process.argv[2]), apiRoot = resolve(process.argv[3]), webRoot = resolve(process.argv[4])
const apiRequire = createRequire(resolve(apiRoot, 'package.json')), webRequire = createRequire(resolve(webRoot, 'package.json'))
const { AccessToken, RoomServiceClient, AgentDispatchClient } = apiRequire('livekit-server-sdk')
const { chromium } = webRequire('@playwright/test')
const settings = JSON.parse(await readFile(resolve(root, 'native-probe-settings.private.json')))
const registered = JSON.parse(await readFile(resolve(root, 'native-probe-evidence/native-registration.sanitized.json')))
if (registered.incarnation !== settings.incarnation || registered.probeNonce !== settings.probeNonce)
  throw new Error('Owned registration differs from the exact probe incarnation')
const rooms = new RoomServiceClient(settings.URL.replace(/^ws/, 'http'), settings.Key, settings.Secret, { requestTimeout: 10, failover: false })
const dispatches = new AgentDispatchClient(settings.URL.replace(/^ws/, 'http'), settings.Key, settings.Secret, { requestTimeout: 10, failover: false })
const bundle = await readFile(resolve(webRoot, 'node_modules/livekit-client/dist/livekit-client.umd.js'))
const server = createServer((request, response) => {
  response.setHeader('Cache-Control', 'no-store')
  response.setHeader('Content-Type', request.url === '/sdk.js' ? 'text/javascript' : 'text/html')
  response.end(request.url === '/sdk.js' ? bundle : '<!doctype html><script src="/sdk.js"></script>')
})
server.listen(0, '127.0.0.1'); await once(server, 'listening')
const name = `ownyai-assignment-probe-${randomUUID()}`, caller = `owned-caller-${randomUUID()}`
const result = { provider: 'livekit-cloud', projectId: settings.ProjectId,
  observedAt: new Date().toISOString(), scenario: 'exact SDK native job readback and stale-incarnation rejection in an owned room', assertions: {} }
let browser, created = false, validDispatch, staleDispatch
try {
  const room = await rooms.createRoom({ name, emptyTimeout: 90, metadata: JSON.stringify({ purpose: 'owned-native-assignment-probe' }) }); created = true
  browser = await chromium.launch({ headless: true });const page = await browser.newPage()
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  const token = new AccessToken(settings.Key, settings.Secret, { identity: caller, ttl: 120 })
  token.addGrant({ roomJoin: true, room: name, canPublish: false, canPublishData: false, canSubscribe: true })
  await page.evaluate(async ({url,jwt}) => {
    window.room = new window.LivekitClient.Room()
    await window.room.connect(url,jwt,{autoSubscribe:false,maxRetries:0})
  },{url:settings.URL,jwt:await token.toJwt()})
  const metadata = { probeNonce: settings.probeNonce, incarnation: settings.incarnation }
  validDispatch = await dispatches.createDispatch(name, settings.agentName, { metadata: JSON.stringify(metadata) })
  const deadline = Date.now() + 25000
  let observed, job, agent
  while (Date.now() < deadline) {
    observed = await dispatches.getDispatch(validDispatch.id,name)
    job = observed?.state?.jobs.find(item => item.state?.status === 1 && item.state.workerId === registered.workerId && item.state.participantIdentity === settings.participantIdentity)
    if (job) {
      agent = (await rooms.listParticipants(name)).find(item => item.identity === settings.participantIdentity)
      if (agent?.sid) break
    }
    await new Promise(resolve => setTimeout(resolve,200))
  }
  if (!job || !observed) throw new Error('No current server-observed native assignment was available')
  const nativeRoom = (await rooms.listRooms([name]))
  const sdkJob = JSON.parse(await readFile(resolve(root, `native-probe-evidence/native-job-${job.id}.sanitized.json`)))
  const matches = binding => observed.id === binding.dispatchId && observed.room === binding.roomName
    && job.id === binding.jobId && job.dispatchId === binding.dispatchId
    && nativeRoom.length === 1 && nativeRoom[0].name === binding.roomName && nativeRoom[0].sid === binding.roomSid
    && (job.room === undefined || (job.room.sid === binding.roomSid && job.room.name === binding.roomName))
    && sdkJob.jobId === binding.jobId && sdkJob.dispatchId === binding.dispatchId
    && sdkJob.roomSid === binding.roomSid && sdkJob.roomName === binding.roomName
    && sdkJob.participantIdentity === binding.identity && sdkJob.incarnation === binding.incarnation && sdkJob.probeNonce === binding.probeNonce
    && job.state.workerId === binding.workerId && job.state.participantIdentity === binding.identity
    && job.state.status === 1 && job.state.endedAt === 0n
    && JSON.parse(observed.metadata).incarnation === binding.incarnation
    && JSON.parse(observed.metadata).probeNonce === binding.probeNonce
  const binding = { dispatchId: observed.id, roomName: name, roomSid: room.sid, jobId: job.id,
    workerId: registered.workerId, identity: settings.participantIdentity, ...metadata }
  result.nativeChecks = { dispatchIdMatches: observed.id === binding.dispatchId,
    dispatchRoomMatches: observed.room === binding.roomName, jobDispatchMatches: job.dispatchId === binding.dispatchId,
    jobRoomSidMatches: job.room?.sid === binding.roomSid, jobRoomSidPresent: Boolean(job.room?.sid),
    nativeRoomSidMatches: nativeRoom[0]?.sid === binding.roomSid, sdkJobRoomSidMatches: sdkJob.roomSid === binding.roomSid,
    jobStatus: job.state.status, endedAtType: typeof job.state.endedAt, endedAt: String(job.state.endedAt),
    agentSidPresent: Boolean(agent?.sid), agentKind: agent?.kind,
    metadataMatches: observed.metadata === JSON.stringify(metadata) }
  if (!matches(binding) || !agent?.sid || agent.kind !== 4) throw new Error('Native worker/job/room/agent identity was not bound')
  const staleBindingRejected = !matches({...binding,workerId:'AW_stale-incarnation',incarnation:randomUUID()})
  staleDispatch = await dispatches.createDispatch(name, settings.agentName, { metadata: JSON.stringify({probeNonce:randomUUID(),incarnation:randomUUID()}) })
  await new Promise(resolve => setTimeout(resolve,1500))
  const stale = await dispatches.getDispatch(staleDispatch.id,name)
  const staleActivated = stale?.state?.jobs.some(item => item.state?.status === 1 && item.state.workerId === registered.workerId)
  if (!staleBindingRejected || staleActivated) throw new Error('Stale incarnation was accepted')
  result.assertions = { serverObservedAssignment:true,staleAssignmentRejected:true,poolIncarnationBound:true }
  result.binding = { workerId:registered.workerId,incarnation:settings.incarnation,jobId:job.id,dispatchId:observed.id,roomSid:room.sid,agentParticipantSid:agent.sid }
  result.status = 'observed'
} catch(error) {
  result.status='unproven';result.failure=String(error.message).replace(/https?:\/\/\S+/g,'[URL]').replace(/eyJ[A-Za-z0-9_.-]+/g,'[JWT]').slice(0,250);process.exitCode=1
} finally {
  if(staleDispatch)await dispatches.deleteDispatch(staleDispatch.id,name)
  if(validDispatch)await dispatches.deleteDispatch(validDispatch.id,name)
  if(browser)await browser.close()
  if(created){await rooms.deleteRoom(name);result.ownedRoomRemoved=true}
  await new Promise(resolve=>server.close(resolve))
  result.finishedAt=new Date().toISOString()
  await writeFile(resolve(root,process.argv[5] ?? 'cloud-native-assignment-20261009.sanitized.json'),JSON.stringify(result,null,2)+'\n',{mode:0o600,flag:'wx'})
  console.log(JSON.stringify({status:result.status,assertions:result.assertions,ownedRoomRemoved:result.ownedRoomRemoved,failure:result.failure}))
}
