import { createRequire } from 'node:module'
const require = createRequire('/app/package.json')
const { defineAgent } = require('@livekit/agents')

export default defineAgent({
  entry: async ctx => {
    const token = ctx.runtimeContext?.privateProbeToken
    if (typeof token !== 'string') throw new Error('Private owned-probe token missing')
    await ctx.connectWithToken(token)
    // No model, tools, audio publication, or production conversation is invoked.
    await new Promise(resolve => ctx.room.once('disconnected', resolve))
  },
})
