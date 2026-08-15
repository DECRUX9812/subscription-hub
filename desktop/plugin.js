/**
 * Subscription Hub — unified subscriptions widget (replaces cpa-quota +
 * cliproxy-widget).
 *
 * One pane + statusbar chip showing EVERYTHING about your subscriptions:
 *   - Pet mascot whose mood reflects consumption (happy → worried → panicked)
 *   - Antigravity quota buckets (animated bars + reset countdowns)
 *   - OpenCode Go rolling/weekly/monthly usage
 *   - Provider cards with one-click connect (OAuth / API-key / import)
 *   - Proxy health (up/down, model count, latency)
 *   - History sparkline
 *
 * Folder name == plugin id == 'subscription-hub'. Backend:
 * ~/.hermes/plugins/subscription-hub/dashboard/plugin_api.py (ctx.rest).
 */

import {
  Badge,
  Button,
  cn,
  haptic,
  host,
  ScrollArea,
  Skeleton,
  Tip,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@hermes/plugin-sdk'

const ID = 'subscription-hub'
const POLL_MS = 30000

// ctx.rest captured at register-time — plain closures keep component
// signatures clean (proven pixel-office pattern).
let ctxRest = async () => { throw new Error('not registered') }

/* ---------- helpers ---------- */

function pct(f) {
  if (f === null || f === undefined) return null
  return Math.round(f * 100)
}

function clamp01(v) {
  if (v === null || v === undefined) return null
  return Math.max(0, Math.min(1, v))
}

/** Tone for a remaining fraction 0..1: green → amber → red. */
function tone(f) {
  if (f === null || f === undefined) return { a: 'var(--ui-stroke-secondary)', text: 'var(--ui-text-tertiary)', hex: '#8b949e' }
  if (f >= 0.5) return { a: '#3ddc84', text: '#3ddc84', hex: '#3ddc84' }
  if (f >= 0.25) return { a: '#f5b301', text: '#f5b301', hex: '#f5b301' }
  if (f >= 0.1) return { a: '#ff8a3d', text: '#ff8a3d', hex: '#ff8a3d' }
  return { a: '#ff5252', text: '#ff5252', hex: '#ff5252' }
}

function fmtCountdown(secs) {
  if (secs === null || secs === undefined) return ''
  const s = Math.max(0, Math.floor(secs))
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${s}s`
}

function liveReset(resetIn, nowMs, serverTs) {
  if (resetIn === null || resetIn === undefined) return null
  const base = serverTs ? serverTs * 1000 : Date.now()
  return resetIn - (nowMs - base) / 1000
}

function bucketsOf(d) {
  return (d && d.buckets) || []
}

function lowestOf(buckets) {
  if (!buckets.length) return null
  return buckets.reduce((a, b) => {
    if (b.remaining === null || b.remaining === undefined) return a
    if (a === null || a.remaining === null || a.remaining === undefined) return b
    return b.remaining < a.remaining ? b : a
  }, null)
}

function overallFrac(d) {
  const low = lowestOf(bucketsOf(d))
  return low ? clamp01(low.remaining) : null
}

/* ---------- pet mascot ---------- */

// A tiny pixel critter whose mood tracks consumption. SVG pixel art, 16x16.
function Pet({ mood, size = 96 }) {
  const px = size / 16
  const palette = {
    happy: { body: '#3ddc84', eye: '#0f2b1d', glow: '#3ddc84' },
    good: { body: '#4fc3f7', eye: '#0b2540', glow: '#4fc3f7' },
    worried: { body: '#f5b301', eye: '#3a2800', glow: '#f5b301' },
    panicked: { body: '#ff5252', eye: '#2b0000', glow: '#ff5252' },
    sleeping: { body: '#7a8ba3', eye: '#223044', glow: '#7a8ba3' },
  }[mood] || { body: '#4fc3f7', eye: '#0b2540', glow: '#4fc3f7' }

  // 16x16 sprite: rows as strings, H=body, E=eye, M=mouth, .=bg, S=shine
  const rows = [
    '....HHHHHH....',
    '..HHHHHHHHHH..',
    '.HHHHHHHHHHHH.',
    '.HHSSHHHHSSHH.',
    'HHHHHHHHHHHHHH',
    'HHHEHHHHHHEHHH',
    'HHHHHHHHHHHHHH',
    'HHHHHHHHHHHHHH',
    'HHHMMMMMMMHHHH',
    'HHHMMMMMMMMHHH',
    '.HHHHHHHHHHHH.',
    '.HHHHHHHHHHHH.',
    '..HHHHHHHHHH..',
    '...HHHHHHHH...',
    '....HH..HH....',
    '....HH..HH....',
  ]
  // mood tweaks: eyes get big when panicked, closed when sleeping
  if (mood === 'panicked') {
    rows[5] = 'HHHEEHHHHHEEHH'
    rows[6] = 'HHHEEHHHHHEEHH'
    rows[8] = 'HHHMMMMMMMHHHH'
    rows[9] = 'HHHMMMMMMMHHHH'
  }
  if (mood === 'sleeping') {
    rows[5] = 'HHH__HHHHH__HH'
    rows[6] = 'HHH__HHHHH__HH'
    rows[8] = 'HHHZZZZZZZHHHH'
  }
  if (mood === 'happy') {
    rows[8] = 'HHHMMMMMMMHHHH'
    rows[9] = 'HHHHMMMMMHHHHH'
  }
  if (mood === 'worried') {
    rows[8] = 'HHH_MMMMM_HHHH'
    rows[9] = 'HHH_MMMMM_HHHH'
  }

  const anim =
    mood === 'sleeping'
      ? 'hub-pet-sleep 3s ease-in-out infinite'
      : mood === 'panicked'
        ? 'hub-pet-panic 0.35s ease-in-out infinite'
        : mood === 'worried'
          ? 'hub-pet-worry 2s ease-in-out infinite'
          : 'hub-pet-bob 2.4s ease-in-out infinite'

  return jsx('div', {
    className: 'relative inline-block',
    style: { width: size, height: size, animation: anim, filter: `drop-shadow(0 0 12px ${palette.glow}55)` },
    children: jsx('svg', {
      width: size,
      height: size,
      viewBox: '0 0 16 16',
      shapeRendering: 'crispEdges',
      children: rows.map((row, y) =>
        row.split('').map((ch, x) => {
          if (ch === '.') return null
          const fill = ch === 'E' ? palette.eye : ch === 'S' ? '#ffffff88' : ch === '_' ? '#ffffff22' : ch === 'Z' ? '#ffffff88' : palette.body
          return jsx('rect', { key: `${x}-${y}`, x, y, width: 1, height: 1, fill })
        })
      ),
    }),
  })
}

function moodOf(frac, loading, error) {
  if (loading) return 'sleeping'
  if (error || frac === null || frac === undefined) return 'sleeping'
  if (frac >= 0.5) return 'happy'
  if (frac >= 0.25) return 'good'
  if (frac >= 0.1) return 'worried'
  return 'panicked'
}

function moodLabel(mood) {
  return {
    happy: 'All good!',
    good: 'Doing fine',
    worried: 'Getting low…',
    panicked: 'Almost gone!',
    sleeping: 'Waiting for data…',
  }[mood] || ''
}

/* ---------- sparkline ---------- */

function Sparkline({ points, width = 200, height = 32 }) {
  if (!points || points.length < 2) {
    return jsx('div', {
      className: 'flex items-center justify-center text-[10px] text-(--ui-text-quaternary)',
      style: { width, height },
      children: 'no history yet',
    })
  }
  const values = points.map(p => p.remaining).filter(v => v !== null && v !== undefined)
  if (!values.length) return null
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const stepX = width / (values.length - 1)
  const pts = values
    .map((v, i) => `${(i * stepX).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 6)).toFixed(1)}`)
    .join(' ')
  const color = tone(values[values.length - 1]).hex
  return jsx('svg', {
    width, height,
    viewBox: `0 0 ${width} ${height}`,
    className: 'overflow-visible',
    children: [
      jsx('polyline', {
        points: pts,
        fill: 'none',
        stroke: color,
        strokeWidth: 1.5,
        strokeLinejoin: 'round',
        strokeLinecap: 'round',
        opacity: 0.9,
      }),
      jsx('circle', {
        cx: width - 1,
        cy: height - 2 - ((values[values.length - 1] - min) / span) * (height - 6),
        r: 2,
        fill: color,
      }),
    ],
  })
}

/* ---------- animated bar ---------- */

function AnimatedBar({ frac, height = 10 }) {
  const t = tone(frac)
  const p = frac === null || frac === undefined ? 0 : clamp01(frac) * 100
  return jsx('div', {
    className: 'hub-bar-track',
    style: { height, borderRadius: 999, overflow: 'hidden', background: 'var(--chrome-action-hover)' },
    children: jsx('div', {
      className: 'hub-bar-fill',
      style: {
        width: `${p}%`,
        height: '100%',
        borderRadius: 999,
        background: `linear-gradient(90deg, ${t.a}, ${t.hex}88)`,
        boxShadow: `0 0 10px ${t.hex}66`,
        transition: 'width 700ms cubic-bezier(0.22, 1, 0.36, 1)',
      },
    }),
  })
}

/* ---------- chip ---------- */

function HubChip() {
  const qc = useQueryClient()
  const { data, isError } = useQuery({
    queryKey: [ID, 'quota'],
    queryFn: async () => ctxRest('/quota', { method: 'GET', timeoutMs: 10000 }),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
  })
  const frac = overallFrac(data)
  const t = tone(frac)
  const label = isError
    ? '◇ sub?'
    : frac === null || frac === undefined
      ? '◇ subs'
      : `◇ ${pct(frac)}%`

  return jsx(Tip, {
    label: 'Subscription Hub — click to refresh',
    children: jsx('button', {
      type: 'button',
      className: cn(
        'inline-flex h-full items-center gap-1.5 px-2 font-mono text-[0.6875rem] transition-colors',
        'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      onClick: () => {
        haptic('tap')
        qc.invalidateQueries({ queryKey: [ID, 'quota'] })
        host.navigate?.('/subscription-hub')
      },
      children: [
        jsx('span', {
          className: 'hub-chip-dot',
          style: {
            width: 8,
            height: 8,
            borderRadius: 999,
            background: t.hex,
            boxShadow: `0 0 8px ${t.hex}`,
            display: 'inline-block',
          },
        }),
        jsx('span', { children: label }),
      ],
    }),
  })
}

/* ---------- pane ---------- */

function BucketCard({ b, nowMs, serverTs, threshold }) {
  const frac = clamp01(b.remaining)
  const live = liveReset(b.reset_in_seconds, nowMs, serverTs)
  const t = tone(frac)
  const low = frac !== null && frac < threshold
  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded-lg border border-(--ui-stroke-secondary) p-2.5',
    style: { borderLeft: `3px solid ${t.a}`, background: 'var(--ui-bg-subtle)' },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5 text-xs',
        children: [
          jsx('span', { className: 'truncate font-medium text-(--ui-text-primary)', children: b.display_name }),
          jsx('span', { className: 'shrink-0 text-[10px] text-(--ui-text-quaternary)', children: `${b.size} model${b.size === 1 ? '' : 's'}` }),
          jsx('span', { className: 'ml-auto shrink-0 font-mono text-[11px] font-semibold', style: { color: t.text }, children: frac === null ? 'n/a' : `${pct(frac)}%` }),
          live !== null && frac !== null
            ? jsx('span', { className: 'shrink-0 font-mono text-[10px] text-(--ui-text-tertiary)', children: `⏱ ${fmtCountdown(live)}` })
            : null,
        ],
      }),
      jsx(AnimatedBar, { frac }),
      low ? jsx('div', { className: 'text-[10px] text-(--ui-accent)', children: '⚠ low — top up soon' }) : null,
    ],
  })
}

function OcRow({ label, d, nowMs }) {
  if (!d) return null
  const usedNum = d.percent != null ? Math.min(100, Math.max(0, d.percent)) : d.remaining_fraction != null ? Math.round((1 - d.remaining_fraction) * 100) : null
  const frac = usedNum == null ? null : (100 - usedNum) / 100
  const reset = secsUntil(d.resetsAt, nowMs)
  const t = tone(frac)
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-lg border border-(--ui-stroke-secondary) p-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5 text-[11px]',
        children: [
          jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: label }),
          jsx('span', { className: 'ml-auto font-mono', style: { color: t.text }, children: usedNum == null ? '—' : `${usedNum}% used` }),
          jsx('span', { className: 'font-mono text-[10px] text-(--ui-text-tertiary)', children: reset != null ? `resets ${fmtCountdown(reset)}` : '' }),
        ],
      }),
      jsx(AnimatedBar, { frac, height: 6 }),
    ],
  })
}

function secsUntil(iso, nowMs) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return (d.getTime() - nowMs) / 1000
}

function ProviderCard({ p, onConnect }) {
  const kindIcon = p.kind === 'oauth' ? '🔑' : p.kind === 'api-key' ? '🗝️' : p.kind === 'import' ? '📄' : '🔌'
  return jsxs('div', {
    className: 'flex items-center gap-2 rounded-lg border border-(--ui-stroke-secondary) p-2',
    children: [
      jsx('span', { className: 'text-base', children: kindIcon }),
      jsxs('div', {
        className: 'flex min-w-0 flex-col',
        children: [
          jsx('span', { className: 'truncate text-xs font-medium text-(--ui-text-primary)', children: p.label }),
          jsx('span', {
            className: 'truncate text-[10px]',
            style: { color: p.connected ? '#3ddc84' : 'var(--ui-text-quaternary)' },
            children: p.connected ? `✓ ${p.detail || 'connected'}` : p.detail || 'not connected',
          }),
        ],
      }),
      jsx('div', { className: 'ml-auto shrink-0' }),
      p.connected
        ? jsx(Badge, { tone: 'ok', children: 'on' })
        : jsx(Button, {
            size: 'xs',
            variant: 'outline',
            onClick: () => {
              haptic('tap')
              onConnect(p)
            },
            children: 'Connect',
          }),
    ],
  })
}

function ConnectFlow({ provider, onDone }) {
  const [state, setState] = useState({ status: 'starting', url: null, output: '', error: null })
  useEffect(() => {
    let cancelled = false
    let timer = null
    async function run() {
      try {
        const start = await ctxRest('/connect', {
          method: 'POST',
          body: { provider: provider.id },
          timeoutMs: 15000,
        })
        if (cancelled) return
        if (!start.ok) {
          setState(s => ({ ...s, status: 'error', error: start.error || 'connect failed' }))
          return
        }
        setState(s => ({ ...s, status: 'waiting', url: start.auth_url }))
        const poll = async () => {
          if (cancelled) return
          try {
            const st = await ctxRest('/connect/status', { method: 'GET', timeoutMs: 8000 })
            if (cancelled) return
            setState(s => ({ ...s, url: st.auth_url || s.url, output: st.output || s.output, status: st.running ? 'waiting' : 'done' }))
            if (st.new_file || (st.completed && !st.error)) {
              onDone(st.new_file)
              return
            }
            if (st.completed && st.error) {
              setState(s => ({ ...s, status: 'error', error: st.error }))
              return
            }
            timer = setTimeout(poll, 2000)
          } catch (e) {
            timer = setTimeout(poll, 3000)
          }
        }
        timer = setTimeout(poll, 1500)
      } catch (e) {
        if (!cancelled) setState(s => ({ ...s, status: 'error', error: String(e) }))
      }
    }
    run()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [provider.id])

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-lg border border-(--ui-accent) p-2.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-xs',
        children: [
          jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: `Connect ${provider.label}` }),
          jsx('span', { className: 'ml-auto text-[10px] text-(--ui-text-tertiary)', children: state.status }),
        ],
      }),
      state.url
        ? jsx('a', {
            href: state.url,
            target: '_blank',
            rel: 'noreferrer',
            className: 'truncate rounded bg-(--chrome-action-hover) px-2 py-1 font-mono text-[11px] text-(--ui-accent) hover:underline',
            onClick: () => haptic('tap'),
            children: state.url,
          })
        : null,
      state.output ? jsx('pre', { className: 'max-h-24 overflow-auto text-[10px] leading-snug text-(--ui-text-tertiary)', children: state.output }) : null,
      state.error ? jsx('div', { className: 'text-[11px] text-(--ui-accent)', children: state.error }) : null,
      jsx(Button, {
        size: 'xs',
        variant: 'ghost',
        onClick: () => {
          haptic('tap')
          onDone(null)
        },
        children: 'Dismiss',
      }),
    ],
  })
}

function HubPane() {
  const qc = useQueryClient()
  const [connectProv, setConnectProv] = useState(null)
  const [openHistory, setOpenHistory] = useState(false)

  const quota = useQuery({
    queryKey: [ID, 'quota'],
    queryFn: async () => ctxRest('/quota', { method: 'GET', timeoutMs: 10000 }),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
  })
  const providers = useQuery({
    queryKey: [ID, 'providers'],
    queryFn: async () => ctxRest('/providers', { method: 'GET', timeoutMs: 8000 }),
    refetchInterval: 60000,
    refetchIntervalInBackground: false,
  })
  const status = useQuery({
    queryKey: [ID, 'status'],
    queryFn: async () => ctxRest('/status', { method: 'GET', timeoutMs: 8000 }),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
  })
  const history = useQuery({
    queryKey: [ID, 'history'],
    queryFn: async () => ctxRest('/history?days=1&max_points=48', { method: 'GET', timeoutMs: 8000 }),
    enabled: openHistory,
    refetchInterval: 120000,
  })

  const d = quota.data
  const frac = overallFrac(d)
  const mood = moodOf(frac, quota.isLoading, quota.isError)
  const buckets = bucketsOf(d)
  const opencode = (d && d.opencode_usage) || {}
  const provs = (providers.data && providers.data.providers) || []
  const st = status.data

  // pick a model for the sparkline — lowest remaining or first
  const histModel = useMemo(() => {
    const models = (history.data && history.data.models) || {}
    const keys = Object.keys(models)
    if (!keys.length) return null
    const low = lowestOf(buckets)
    return (low && low.models && low.models[0]) || keys[0]
  }, [history.data, buckets])

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm',
    children: [
      /* header with pet */
      jsxs('div', {
        className: 'flex items-center gap-3 rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-subtle) p-3',
        children: [
          jsx(Pet, { mood, size: 72 }),
          jsxs('div', {
            className: 'flex min-w-0 flex-col gap-0.5',
            children: [
              jsx('div', { className: 'flex items-center gap-2' }),
              jsx('span', { className: 'text-base font-semibold text-(--ui-text-primary)', children: 'Subscription Hub' }),
              jsx('span', {
                className: 'text-[11px]',
                style: { color: frac === null ? 'var(--ui-text-tertiary)' : tone(frac).text },
                children: moodLabel(mood) + (frac === null ? '' : ` — ${pct(frac)}% of lowest window left`),
              }),
              d && d.account
                ? jsx('span', { className: 'truncate text-[10px] text-(--ui-text-quaternary)', children: `account: ${d.account}` })
                : null,
            ],
          }),
          jsx('div', { className: 'ml-auto shrink-0' }),
          quota.isError || d?.error
            ? jsx(Badge, { tone: 'danger', children: 'error' })
            : jsx(Badge, { tone: 'ok', children: 'live' }),
        ],
      }),

      /* proxy status strip */
      jsxs('div', {
        className: 'grid grid-cols-3 gap-2 text-xs',
        children: [
          jsxs('div', {
            className: 'flex flex-col gap-0.5 rounded-md border border-(--ui-stroke-secondary) p-2',
            children: [
              jsx('span', { className: 'text-[10px] text-(--ui-text-quaternary)', children: 'proxy' }),
              jsx('span', {
                className: 'font-medium',
                style: { color: st ? (st.proxy_up ? '#3ddc84' : '#ff5252') : 'var(--ui-text-tertiary)' },
                children: st ? (st.proxy_up ? 'up' : 'down') : '…',
              }),
            ],
          }),
          jsxs('div', {
            className: 'flex flex-col gap-0.5 rounded-md border border-(--ui-stroke-secondary) p-2',
            children: [
              jsx('span', { className: 'text-[10px] text-(--ui-text-quaternary)', children: 'models' }),
              jsx('span', { className: 'font-medium', children: st ? st.model_count : '…' }),
            ],
          }),
          jsxs('div', {
            className: 'flex flex-col gap-0.5 rounded-md border border-(--ui-stroke-secondary) p-2',
            children: [
              jsx('span', { className: 'text-[10px] text-(--ui-text-quaternary)', children: 'latency' }),
              jsx('span', { className: 'font-medium', children: st ? `${st.latency_ms ?? '—'} ms` : '…' }),
            ],
          }),
        ],
      }),

      /* quota buckets */
      jsxs('div', {
        className: 'flex flex-col gap-2',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between',
            children: [
              jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Quota windows' }),
              jsx('span', {
                className: 'cursor-pointer font-mono text-[10px] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
                onClick: () => {
                  haptic('tap')
                  setOpenHistory(o => !o)
                },
                children: openHistory ? '▾ hide history' : '▸ history',
              }),
            ],
          }),
          quota.isLoading
            ? jsx(Skeleton, { className: 'h-16 w-full' })
            : quota.isError
              ? jsx('div', { className: 'text-xs text-(--ui-accent)', children: `quota error: ${quota.error?.message || quota.error || 'unknown'}` })
              : buckets.length
                ? buckets.map(b => jsx(BucketCard, { b, nowMs: Date.now(), serverTs: d.ts, threshold: (d.config && d.config.low_threshold) || 0.1, key: b.id }))
                : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: d?.error || 'no quota data' }),
          openHistory && histModel
            ? jsxs('div', {
                className: 'flex flex-col gap-1',
                children: [
                  jsx('span', { className: 'text-[10px] text-(--ui-text-quaternary)', children: `${histModel} · last 24h` }),
                  jsx(Sparkline, { points: history.data?.models?.[histModel] || [] }),
                ],
              })
            : null,
        ],
      }),

      /* OpenCode Go */
      (opencode.rolling || opencode.weekly || opencode.monthly)
        ? jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'OpenCode Go' }),
              jsx(OcRow, { label: 'rolling', d: opencode.rolling, nowMs: Date.now() }),
              jsx(OcRow, { label: 'weekly', d: opencode.weekly, nowMs: Date.now() }),
              jsx(OcRow, { label: 'monthly', d: opencode.monthly, nowMs: Date.now() }),
            ],
          })
        : null,

      /* providers + connect */
      jsxs('div', {
        className: 'flex flex-col gap-2',
        children: [
          jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Providers' }),
          provs.length
            ? provs.map(p => jsx(ProviderCard, { p, onConnect: setConnectProv, key: p.id }))
            : jsx(Skeleton, { className: 'h-12 w-full' }),
          connectProv ? jsx(ConnectFlow, { provider: connectProv, onDone: () => {
            setConnectProv(null)
            qc.invalidateQueries({ queryKey: [ID, 'quota'] })
            qc.invalidateQueries({ queryKey: [ID, 'providers'] })
          } }) : null,
        ],
      }),
    ],
  })
}

/* ---------- page ---------- */

function HubPage() {
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-4',
    children: [
      jsx('div', { className: 'text-base font-medium', children: 'Subscription Hub' }),
      jsx('div', { className: 'min-h-0 flex-1' }),
      jsx(HubPane, {}),
    ],
  })
}

/* ---------- register ---------- */

if (typeof document !== 'undefined' && !document.getElementById('subhub-css')) {
  const style = document.createElement('style')
  style.id = 'subhub-css'
  style.textContent = `
    @keyframes hub-pet-bob { 0%,100% { transform: translateY(0) } 50% { transform: translateY(-4px) } }
    @keyframes hub-pet-sleep { 0%,100% { transform: translateY(0) scaleY(1) } 50% { transform: translateY(2px) scaleY(0.96) } }
    @keyframes hub-pet-worry { 0%,100% { transform: rotate(0deg) } 25% { transform: rotate(-4deg) } 75% { transform: rotate(4deg) } }
    @keyframes hub-pet-panic { 0%,100% { transform: translateX(0) } 25% { transform: translateX(-2px) } 75% { transform: translateX(2px) } }
    @keyframes hub-bar-shimmer { 0% { background-position: -100px 0 } 100% { background-position: 200px 0 } }
    @keyframes hub-chip-pulse { 0%,100% { opacity: 1 } 50% { opacity: 0.55 } }
    .hub-bar-fill { position: relative; overflow: hidden; }
    .hub-bar-fill::after {
      content: ''; position: absolute; inset: 0;
      background: linear-gradient(90deg, transparent, #ffffff44, transparent);
      background-size: 120px 100%;
      animation: hub-bar-shimmer 2.4s linear infinite;
    }
    .hub-chip-dot { animation: hub-chip-pulse 2s ease-in-out infinite; }
  `
  document.head.appendChild(style)
}

export default {
  id: ID,
  name: 'Subscription Hub',
  register(ctx) {
    ctxRest = (path, opts) => ctx.rest(path, opts)

    ctx.register({
      id: 'subhub-chip',
      area: 'statusBar.right',
      order: 110,
      render: () => jsx(HubChip, {}),
    })

    ctx.register({
      id: 'subhub-pane',
      area: 'panes',
      title: 'Subscriptions',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(HubPane, {}),
    })

    ctx.register({
      id: 'subhub-page',
      area: 'pages',
      path: '/subscription-hub',
      title: 'Subscription Hub',
      render: () => jsx(HubPage, {}),
    })
  },
}
