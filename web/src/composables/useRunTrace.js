import { agentApi } from '@/apis'
import { applyTraceEvent, applyTraceSnapshot, createTraceState } from '@/utils/traceProjection'

const TRACE_CATCHUP_PAGE_LIMIT = 500
const TRACE_CATCHUP_MAX_PAGES_PER_PASS = 100

/** Durable trace consumer. USER events need not have dense ledger sequences. */
export function useRunTrace({ getThreadState }) {
  const ensureTraceState = (threadId) => {
    const ts = getThreadState(threadId)
    if (!ts) return null
    if (!ts.trace) ts.trace = createTraceState()
    return ts.trace
  }

  const resetRunTrace = (threadId, runId = null) => {
    const trace = ensureTraceState(threadId)
    if (trace) Object.assign(trace, createTraceState(), { runId })
  }

  const queuePending = (trace, event) => {
    const eventId = event?.event_id
    if (eventId && trace.pendingEvents.some((item) => item?.event_id === eventId)) return
    trace.pendingEvents.push(event)
    trace.pendingEvents.sort(
      (left, right) => Number(left?.sequence || 0) - Number(right?.sequence || 0)
    )
  }

  const loadRunTraceSnapshot = async (threadId, runId) => {
    const trace = ensureTraceState(threadId)
    if (!trace || !runId || trace.runId !== runId) return false
    const token = (trace._snapshotToken || 0) + 1
    trace._snapshotToken = token
    try {
      const snapshot = await agentApi.getAgentRunTrace(runId)
      const latest = ensureTraceState(threadId)
      if (!latest || latest.runId !== runId || latest._snapshotToken !== token) return false
      return applyTraceSnapshot(latest, snapshot)
    } catch (error) {
      console.warn('Failed to load run trace snapshot:', runId, error)
      return false
    }
  }

  const catchUpTraceEvents = async (threadId, runId) => {
    const trace = ensureTraceState(threadId)
    if (!trace || !runId || trace.runId !== runId) return
    if (trace._catchupPromise) return trace._catchupPromise

    let shouldContinue = false
    trace._catchupPromise = (async () => {
      let after = Number(trace.scannedThroughSequence) || 0
      for (let page = 0; page < TRACE_CATCHUP_MAX_PAGES_PER_PASS; page += 1) {
        const previous = after
        const res = await agentApi.getAgentRunTraceEvents(runId, {
          afterSequence: after,
          limit: TRACE_CATCHUP_PAGE_LIMIT
        })
        const latest = ensureTraceState(threadId)
        if (!latest || latest.runId !== runId) return
        const events = Array.isArray(res?.events) ? res.events : []
        events
          .slice()
          .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0))
          .forEach((event) => applyTraceEvent(latest, event))
        after = Number(res?.scanned_through_sequence ?? res?.next_after_sequence) || previous
        latest.scannedThroughSequence = Math.max(latest.scannedThroughSequence, after)
        if (!res?.has_more || after <= previous) break
      }
      const latest = ensureTraceState(threadId)
      if (!latest || latest.runId !== runId) return
      const pending = latest.pendingEvents
      latest.pendingEvents = []
      pending.forEach((event) => {
        if (Number(event?.sequence || 0) <= latest.scannedThroughSequence)
          applyTraceEvent(latest, event)
        else queuePending(latest, event)
      })
      shouldContinue = latest.pendingEvents.some(
        (event) => Number(event?.sequence || 0) > latest.scannedThroughSequence
      )
    })()
      .catch((error) => console.warn('Failed to catch up run trace events:', runId, error))
      .finally(() => {
        const latest = ensureTraceState(threadId)
        if (!latest || latest.runId !== runId) return
        latest._catchupPromise = null
        if (shouldContinue) setTimeout(() => void catchUpTraceEvents(threadId, runId), 250)
      })
    return trace._catchupPromise
  }

  const handleTraceEvent = ({ threadId, runId, traceEvent }) => {
    if (!threadId || !runId || !traceEvent || traceEvent.visibility === 'ADMIN') return
    const trace = ensureTraceState(threadId)
    if (!trace) return
    if (trace.runId !== runId) {
      Object.assign(trace, createTraceState(), { runId, loading: true })
      queuePending(trace, traceEvent)
      void loadRunTraceSnapshot(threadId, runId).finally(() => {
        const latest = ensureTraceState(threadId)
        if (!latest || latest.runId !== runId) return
        latest.loading = false
        void catchUpTraceEvents(threadId, runId)
      })
      return
    }
    if (Number(traceEvent.sequence || 0) <= trace.lastAppliedSequence) return
    queuePending(trace, traceEvent)
    if (!trace.loading) void catchUpTraceEvents(threadId, runId)
  }

  const refreshRunTraceSnapshot = (threadId, runId) => {
    const trace = ensureTraceState(threadId)
    if (!trace || !runId || trace.runId !== runId) return
    void loadRunTraceSnapshot(threadId, runId).finally(
      () => void catchUpTraceEvents(threadId, runId)
    )
  }

  return {
    handleTraceEvent,
    loadRunTraceSnapshot,
    refreshRunTraceSnapshot,
    resetRunTrace,
    catchUpTraceEvents
  }
}
