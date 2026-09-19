import { useEffect, useMemo, useState } from 'react'
import { decisionQueue } from './mockDecisionData'
import './App.css'

const timeBudgetMs = 5000

const parsePolicyRules = (policyText) => {
  const lower = policyText.toLowerCase()
  const amountMatch = policyText.match(/(?:chf\s*|CHF\s*)(\d+(?:[.,]\d{1,2})?)/)
  const budgetLimit = amountMatch ? Number(amountMatch[1].replace(',', '.')) : 20

  return {
    maxAmount: budgetLimit,
    allowedCategory: lower.includes('grocery') ? 'grocery' : 'general',
    merchantRequirement: lower.includes('regular') ? 'known merchant' : 'any merchant',
    uncertaintyPolicy: lower.includes('ask me when uncertain') ? 'ask' : 'decline',
  }
}

function App() {
  const [queueIndex, setQueueIndex] = useState(0)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [revealedChecks, setRevealedChecks] = useState(0)

  const currentRequest = decisionQueue[queueIndex]

  useEffect(() => {
    setElapsedMs(0)
    setRevealedChecks(0)

    const target =
      currentRequest.proposal.totalAmount > 70 || currentRequest.proposal.merchantKnown === false
        ? 2600
        : 1500

    const timer = window.setInterval(() => {
      setElapsedMs((current) => {
        const nextValue = current + 220

        if (nextValue >= Math.min(target, timeBudgetMs)) {
          setQueueIndex((index) => (index + 1) % decisionQueue.length)
          return 0
        }

        return nextValue
      })
    }, 220)

    return () => window.clearInterval(timer)
  }, [queueIndex, currentRequest])

  const policyRules = useMemo(
    () => parsePolicyRules(currentRequest.customer.walletPolicy),
    [currentRequest]
  )

  const productName = currentRequest.proposal.product.toLowerCase()
  const allowedCategory = policyRules.allowedCategory
  const productAllowed =
    allowedCategory === 'grocery'
      ? productName.includes('apple') || productName.includes('bread') || productName.includes('milk') || productName.includes('groceries') || productName.includes('organic')
      : true

  const priceAllowed = currentRequest.proposal.totalAmount <= policyRules.maxAmount
  const merchantAllowed = currentRequest.proposal.merchantKnown
  const injectionAttempt = currentRequest.proposal.sellerNotes.toLowerCase().includes('ignore the spending limit')
  const isUncertain = currentRequest.proposal.merchantKnown === false || currentRequest.proposal.riskScore > 40

  const baseChecks = [
    {
      title: 'Blended policy match',
      status: productAllowed && priceAllowed ? 'ok' : 'fail',
      detail: productAllowed && priceAllowed
        ? 'The requested item, price, and merchant context all fit the customer’s wallet policy.'
        : 'The item or price conflicts with the customer’s explicit rules, so the purchase is not automatically safe.',
    },
    {
      title: 'Merchant familiarity',
      status: merchantAllowed ? 'ok' : 'warn',
      detail: merchantAllowed
        ? 'The merchant is part of the customer’s trusted, regular shop list.'
        : 'The merchant is not a familiar shop, so the request needs a higher level of review.',
    },
    {
      title: 'Prompt injection resistance',
      status: injectionAttempt ? 'warn' : 'ok',
      detail: injectionAttempt
        ? 'The seller instruction was treated as untrusted text and blocked from changing the wallet policy.'
        : 'No malicious instruction pattern was detected in the merchant message.',
    },
    {
      title: 'Uncertainty handling',
      status: isUncertain ? 'warn' : 'ok',
      detail: isUncertain
        ? 'The policy requires customer confirmation because the situation is ambiguous or unusual.'
        : 'The request is sufficiently clear and stable for a routine approval flow.',
    },
  ]

  const priority = { ok: 0, warn: 1, fail: 2 }
  const checks = [...baseChecks].sort((a, b) => priority[a.status] - priority[b.status])

  useEffect(() => {
    const revealThresholds = checks.map((_, index) => (index + 1) * 800)
    const nextCount = revealThresholds.filter((threshold) => elapsedMs >= threshold).length
    setRevealedChecks(nextCount)
  }, [elapsedMs, checks])

  const decision =
    checks.some((check) => check.status === 'fail')
      ? 'decline'
      : isUncertain || injectionAttempt
        ? 'step_up'
        : 'approve'

  const decisionVisible = revealedChecks >= checks.length
  const decisionTargetMs = decision === 'approve' ? 1700 : decision === 'step_up' ? 2600 : 2200
  const effectiveDeadlineMs = Math.min(decisionTargetMs, timeBudgetMs)
  const percentUsed = Math.min((elapsedMs / timeBudgetMs) * 100, 100)
  const remainingMs = Math.max(timeBudgetMs - elapsedMs, 0)
  const withinSla = effectiveDeadlineMs <= 8000
  const liveStatus = decisionVisible ? decision.toUpperCase() : 'ANALYZING'

  const flowStages = [
    { label: 'Agent request', x: 8 },
    { label: 'Wallet review', x: 28 },
    { label: 'Analysis', x: 52 },
  ]

  const outcomeMap = {
    approve: { label: 'Approved', y: 8, className: 'approve' },
    step_up: { label: 'Step-up', y: 15, className: 'step_up' },
    decline: { label: 'Declined', y: 22, className: 'decline' },
  }

  const decisionOutcome = outcomeMap[decision] || outcomeMap.step_up
  const flowDotX = decisionVisible ? 82 : 12 + revealedChecks * 12
  const flowDotY = decisionVisible ? decisionOutcome.y : 15

  const agentReply =
    decision === 'approve'
      ? 'This purchase matches the customer’s confirmed policy and can proceed without extra approval.'
      : decision === 'decline'
        ? 'This purchase violates the customer’s wallet policy and must not be completed.'
        : 'This purchase is outside the customer’s approved pattern and should be paused for human confirmation before any spending occurs.'

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Wallet control layer</p>
          <h1>Agent on a Leash</h1>
        </div>
      </header>

      <section className="flow-overview panel">
        <div className="panel-header">
          <h2>Decision path</h2>
          <span className="panel-tag">Overview</span>
        </div>

        <div className="flow-visual" aria-label="Decision tree">
          <div className="flow-tree">
            <svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
              <path d="M 10 15 H 52" className="flow-line backbone" />
              <path
                d={decisionVisible ? `M 52 15 L 82 ${decisionOutcome.y}` : `M 10 15 L ${flowDotX} ${flowDotY}`}
                className="flow-line active"
              />
              <path d="M 52 15 L 82 8" className="flow-branch branch-approve" />
              <path d="M 52 15 L 82 15" className="flow-branch branch-step" />
              <path d="M 52 15 L 82 22" className="flow-branch branch-decline" />
            </svg>

            {flowStages.map((stage) => (
              <div
                key={stage.label}
                className="flow-stage-node"
                style={{ left: `${stage.x}%`, top: '15px' }}
              >
                <span className="flow-node-core" />
              </div>
            ))}

            {Object.entries(outcomeMap).map(([key, outcome]) => (
              <div
                key={key}
                className={`flow-outcome ${outcome.className} ${decision === key ? 'active' : ''}`}
                style={{ left: '82%', top: `${outcome.y}px` }}
              >
                {outcome.label}
              </div>
            ))}

            <div className="flow-moving-dot" style={{ left: `${flowDotX}%`, top: `${flowDotY}px` }} />
          </div>
        </div>
      </section>

      <main className="dashboard-grid">
        <section className="panel request-panel">
          <div className="panel-header">
            <h2>Incoming request</h2>
            <span className="panel-tag">Agent input</span>
          </div>

          <div className="request-summary">
            <div className="info-row">
              <span className="label">Request ID</span>
              <strong>{currentRequest.requestId}</strong>
            </div>
            <div className="info-row">
              <span className="label">Agent</span>
              <strong>{currentRequest.agent.name}</strong>
            </div>
            <div className="info-row">
              <span className="label">Customer rule</span>
              <strong>{currentRequest.customer.walletPolicy}</strong>
            </div>
            <div className="info-row split">
              <div>
                <span className="label">Product</span>
                <strong>{currentRequest.proposal.product}</strong>
              </div>
              <div>
                <span className="label">Amount</span>
                <strong>CHF {currentRequest.proposal.totalAmount}</strong>
              </div>
            </div>
            <div className="info-row split">
              <div>
                <span className="label">Merchant</span>
                <strong>{currentRequest.proposal.merchant}</strong>
              </div>
              <div>
                <span className="label">Seller note</span>
                <strong>{currentRequest.proposal.sellerNotes}</strong>
              </div>
            </div>
          </div>
        </section>

        <aside className="panel side-panel">
          <div className="timer-box">
            <div className="timer-header">
              <span>Decision latency</span>
              <span className={`sla-badge ${withinSla ? 'ok' : 'danger'}`}>
                {withinSla ? 'Within SLA' : 'Missed SLA'}
              </span>
            </div>

            <div className="timer-ring" style={{ '--progress': `${percentUsed}%` }}>
              <div className="timer-inner">
                <strong>{(elapsedMs / 1000).toFixed(1)}s</strong>
                <span>{(remainingMs / 1000).toFixed(1)}s left</span>
              </div>
            </div>

            <div className="timer-scale">
              <span>0s</span>
              <span>5s</span>
            </div>
          </div>

          <div className={`decision-box ${decisionVisible ? `decision-${decision}` : 'decision-analyzing'}`}>
            <span className="decision-label">Status</span>
            <strong>{liveStatus}</strong>
          </div>
        </aside>

        <section className="panel checks-panel">
          <div className="panel-header">
            <h2>Blended checks</h2>
            <span className="panel-tag">Policy engine</span>
          </div>

          <div className="check-list">
            {checks.map((check, index) => {
              const isVisible = index < revealedChecks

              return (
                <div
                  key={check.title}
                  className={`check-card ${check.status} ${isVisible ? 'visible' : 'hidden'}`}
                >
                  <div className="check-topline">
                    <h3>{check.title}</h3>
                    <span className={`status-dot ${check.status}`} aria-label={check.status} />
                  </div>
                  {isVisible && <p>{check.detail}</p>}
                </div>
              )
            })}
          </div>
        </section>

        <section className="panel response-panel">
          <div className="panel-header">
            <h2>Response back to agent</h2>
            <span className="panel-tag accent">Natural language</span>
          </div>

          <div className="response-box">
            <p>{decisionVisible ? agentReply : 'Waiting for the policy engine to finish the review...'}</p>
          </div>
        </section>
      </main>
    </div>
  )
}

export default App
