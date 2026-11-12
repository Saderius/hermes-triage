/**
 * Hermes Desktop half of triage.
 * One file. Runtime plugins may only import @hermes/plugin-sdk and react.
 *
 * A row inside the composer card (Hermes' composer.top slot): the held pile and
 * suggestion chips on the left, the loaded-skill fan on the right. Everything
 * that floats (the search list, the skill card, the spread pile) is an SDK
 * Popover. The bar reads nothing of Hermes' own markup. The auto-attach threshold is a plugin setting
 * (Settings -> Plugins -> triage), not composer chrome.
 * Clicking a suggestion holds it on the left pile (the prompt text is never
 * touched); Enter sends the prompt, the agent loads every held skill with it,
 * and the pile flies onto the fan. Plugin-only: no Hermes internals.
 * Behavior spec: docs/designs/how-triage-works.md.
 */
import { Badge, Button, COMPOSER_AREAS, Popover, PopoverContent, PopoverTrigger, haptic, host, icons, useValue } from '@hermes/plugin-sdk'
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'

function jsx(type, props, key) {
  const next = props ? { ...props } : null
  if (key !== undefined && next) next.key = key
  return React.createElement(type, next)
}

const jsxs = jsx

const STYLE_ID = 'triage-style'

// Diagnostics: Hermes copies only renderer console.error lines into
// %LOCALAPPDATA%\hermes\logs\desktop.log, so stage markers go out at error
// level, once per distinct line. Off unless the plugin setting `debug: true`
// (GET /settings reports it), so shipped builds stay quiet.
const DIAG = { on: false, seen: new Set() }
function diag(stage, detail) {
  if (!DIAG.on) return
  const line = `[triage-diag] ${stage}${detail === undefined ? '' : ' ' + safeJson(detail)}`
  if (DIAG.seen.has(line) || DIAG.seen.size > 400) return
  DIAG.seen.add(line)
  console.error(line)
}
function safeJson(value) {
  try {
    return JSON.stringify(value).slice(0, 400)
  } catch {
    return String(value)
  }
}

export default {
  id: 'triage',
  name: 'Triage',
  defaultEnabled: true,
  register(ctx) {
    ensureStyles()
    ctx.onDispose(() => {
      const node = document.getElementById(STYLE_ID)
      if (node) node.remove()
    })
    const area = COMPOSER_AREAS && (COMPOSER_AREAS.top || COMPOSER_AREAS.actions)
    if (!area) {
      ctx.register({
        id: 'no-composer',
        area: 'statusBar.right',
        order: 80,
        render: () => jsx('span', {
          className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
          children: 'triage: composer chrome missing',
        }),
      })
      return
    }
    ctx.register({
      id: 'composer',
      area,
      order: 20,
      title: 'Triage',
      render: () => jsx(SkillJudgeBar, { ctx }),
    })
    // Learn the debug flag early so register/mount markers can be logged.
    Promise.resolve()
      .then(() => ctx.rest('/settings'))
      .then((settings) => {
        DIAG.on = Boolean(settings && settings.debug)
        diag('registered', { area })
      })
      .catch(() => {})
  },
}

// Two ids for one chat. The composer verbs and the slash RPC take the RUNTIME id
// (host.state.focusedSessionId). The Python hooks see the STORED id
// (`local-<sha>`), so every REST call (/approve, /loaded) must send that one,
// or the pick is queued under a key pre_llm_call never reads.
function SkillJudgeBar({ ctx }) {
  const state = host.state || {}
  const runtimeAtom = state.focusedSessionId || null
  const storedAtom = state.focusedStoredSessionId || null
  if (runtimeAtom && storedAtom) return jsx(BoundBoth, { ctx, runtimeAtom, storedAtom })
  if (runtimeAtom) return jsx(BoundRuntime, { ctx, runtimeAtom })
  return jsx(SkillJudgeLive, { ctx, sessionId: null, storedId: null })
}

function BoundBoth({ ctx, runtimeAtom, storedAtom }) {
  const sessionId = useValue(runtimeAtom)
  const storedId = useValue(storedAtom)
  return jsx(SkillJudgeLive, { ctx, sessionId, storedId: storedId || sessionId, isNew: !storedId })
}

function BoundRuntime({ ctx, runtimeAtom }) {
  const sessionId = useValue(runtimeAtom)
  return jsx(SkillJudgeLive, { ctx, sessionId, storedId: sessionId })
}

function SkillJudgeLive({ ctx, sessionId, storedId, isNew }) {
  const [draft, setDraft] = useState('')
  const [ranking, setRanking] = useState({ candidates: [], none_probability: 1 })
  // A "/term" being typed: the chips become a skill search for that term.
  const [search, setSearch] = useState({ term: '', rows: [] })
  const searchRef = useRef('')
  const namesRef = useRef({ version: '', list: [] }) // every skill name, for instant /term search
  const [loaded, setLoaded] = useState([])
  const thresholdState = useState(0.9) // kept for re-renders when the setting changes
  const setThreshold = thresholdState[1]
  const [size, setSize] = useState(5)
  const [body, setBody] = useState(null)
  const [hint, setHint] = useState('')
  // The left pile: skills held for the next message. They travel outside the
  // prompt text (POST /attach); the agent takes them when the message is sent.
  const [held, setHeld] = useState([]) // [{ name, auto }]
  const [typed, setTyped] = useState([]) // /name tokens the user typed into the prompt
  const [busy, setBusy] = useState(false)
  const [flying, setFlying] = useState(null) // { names, dx: {name: px} } while the pile flies right
  const [justSent, setJustSent] = useState([]) // shown on the fan until /loaded reports them
  // The "More" list: { id, query, at, span } while open.
  const [menu, setMenu] = useState(null)
  // Trash pressed: nothing may be held for this message, until it is sent or unlocked.
  const [locked, setLocked] = useState(false)
  const lockedRef = useRef(false)
  const menuRowsRef = useRef([])
  const menuRef = useRef(null)
  const menuListRef = useRef(null)
  const actionsRef = useRef({})
  const [favs, setFavs] = useState({ favorites: [], most_used: [] })
  const reheldRef = useRef(new Set()) // kept skills already put back for a given compaction
  const pileReadyRef = useRef(false) // this chat's pile has been read from the backend
  const [activeName, setActiveName] = useState(null)
  const [pinnedName, setPinnedName] = useState(null)
  const [spread, setSpread] = useState(false)
  const [barWidth, setBarWidth] = useState(0) // the overlay's width; both piles scale with it
  const barWidthRef = useRef(0)
  const draftRef = useRef('')
  const suppressRef = useRef(false)
  const settleRef = useRef(0)
  const dockRef = useRef(null)
  const commitRef = useRef(null)
  // Attach state lives in refs so the poll loop and rank callbacks see it live.
  const heldRef = useRef([]) // [{ name, auto }], mirrors `held`
  const declinedRef = useRef(new Set()) // auto picks the user removed; not re-added this draft
  const busyRef = useRef(false)
  const maxRef = useRef(Infinity) // max_attached; 0 in settings = no limit
  const sizeRef = useRef(5) // shortlist_size: the server returns exactly the row
  const thresholdRef = useRef(0.9)
  const onSentRef = useRef(null)
  const typedRef = useRef([])
  const rerankRef = useRef(null)
  const loadedRef = useRef(new Set())
  const restRef = useRef('')
  const reconcileRef = useRef(null)
  const postChainRef = useRef(Promise.resolve()) // POST /attach calls, in click order
  const sentTimerRef = useRef(0)
  const chatRef = useRef(sessionId) // the runtime id last seen: a new stored id alone is the same chat

  // Chips follow the draft AND the conversation. The backend ranks the draft
  // with the session's recent turns, first prompt and workspace AGENTS.md /
  // README as context; with an empty draft it returns conversation chips, so
  // the row stays up between turns. Re-rank on: draft change (debounced),
  // session change, a turn finishing (busy true -> false), and every 20 s.
  useEffect(() => {
    let dead = false
    let rankTimer = 0
    let lastBusy = null
    let seq = 0
    if (chatRef.current !== sessionId) {
      // Another chat: its own busy state is learned from the first poll.
      busyRef.current = false
      setBusy(false)
    }
    const timer = setInterval(() => {
      void poll()
    }, 450)
    const refresh = setInterval(() => scheduleRank(restRef.current, 0), 20000)
    diag('mounted', { session: Boolean(sessionId), stored: Boolean(storedId) })
    void poll()
    scheduleRank(restRef.current, 0)
    return () => {
      dead = true
      clearInterval(timer)
      clearInterval(refresh)
      clearTimeout(rankTimer)
    }

    async function poll() {
      try {
        const text = (await host.composer.getDraft(sessionId || null)) || ''
        if (dead) return
        const busyAtom = host.state && host.state.busy
        const busy = busyAtom && typeof busyAtom.get === 'function' ? Boolean(busyAtom.get()) : null
        if (busy === true && lastBusy !== true) {
          // A message just went out: the held pile rides with it.
          busyRef.current = true
          setBusy(true)
          if (lastBusy === false && onSentRef.current) onSentRef.current()
        }
        if (text !== draftRef.current) {
          draftRef.current = text
          const term = slashTerm(text)
          if (term !== searchRef.current) {
            searchRef.current = term
            if (term && namesRef.current.list.length) {
              // Instant: searched in the app, no round trip.
              setSearch({ term, rows: localSearch(term, namesRef.current.list, heldRef.current.map((item) => item.name), sizeRef.current + 3) })
            } else if (term) void loadSearch(term)
            else setSearch({ term: '', rows: [] })
          }
          const split = splitTokens(term ? text.replace(SLASH_TAIL_RE, '') : text)
          typedRef.current = split.tokens
          setTyped(split.tokens)
          if (!text.trim()) declinedRef.current.clear()
          setDraft(split.rest)
          if (split.rest !== restRef.current) {
            restRef.current = split.rest
            scheduleRank(split.rest, 280)
          }
        }
        if (lastBusy === true && busy === false) {
          diag('turn-finished')
          busyRef.current = false
          setBusy(false)
          scheduleRank(restRef.current, 600)
        }
        lastBusy = busy
      } catch (err) {
        console.warn('[triage] getDraft poll error:', err)
      }
    }

    // After a pick the spares refill the row at once; the re-rank (the pile
    // is context for the next chips) waits until the clicking stops.
    rerankRef.current = () => scheduleRank(restRef.current, 700)
    function scheduleRank(text, delay) {
      clearTimeout(rankTimer)
      rankTimer = setTimeout(() => {
        void loadRank(text)
      }, delay)
    }

    async function loadSearch(term) {
      try {
        const data = await ctx.rest(`/search?q=${encodeURIComponent(term)}&session_id=${encodeURIComponent(storedId || '')}&limit=${sizeRef.current + 3}`)
        if (dead || searchRef.current !== term) return
        setSearch({ term, rows: (data && Array.isArray(data.candidates)) ? data.candidates : [] })
        diag('search', { term, n: data && data.candidates ? data.candidates.length : 0 })
      } catch (err) {
        console.warn('[triage] search API error:', err)
      }
    }

    async function loadRank(text) {
      const query = (text || '').trim()
      const mine = ++seq
      try {
        // The server knows the held pile and the loaded skills of this chat;
        // only /name tokens typed into the prompt are sent along.
        const params = `q=${encodeURIComponent(query)}&session_id=${encodeURIComponent(storedId || '')}&limit=${sizeRef.current}`
          + `&exclude=${encodeURIComponent(typedRef.current.join(','))}&spare=10`
        const data = await ctx.rest(`/rank?${params}`)
        if (dead || mine !== seq) return
        const rows = data && Array.isArray(data.candidates) ? data.candidates : []
        diag('rank', {
          draft: query.length,
          n: rows.length,
          top: rows.slice(0, 4).map((row) => `${row.name}:${Math.round((row.probability || 0) * 100)}:${row.source || '?'}`),
        })
        setRanking(data || { candidates: [], none_probability: 1 })
        setHint('')
        if (reconcileRef.current) void reconcileRef.current(rows, query, (data && data.held_scores) || {})
      } catch (err) {
        console.warn('[triage] rank API error:', err)
        if (!dead) setHint('Rank API unavailable. Enable the Python plugin.')
      }
    }
  }, [ctx, sessionId, storedId])

  useEffect(() => {
    let dead = false
    async function pull() {
      try {
        const [settings, context] = await Promise.all([
          ctx.rest('/settings'),
          ctx.rest(`/loaded?session_id=${encodeURIComponent(storedId || '')}`),
        ])
        if (dead) return
        if (settings && typeof settings.auto_accept_threshold === 'number') {
          setThreshold(settings.auto_accept_threshold)
          thresholdRef.current = settings.auto_accept_threshold
        }
        if (settings && Number(settings.shortlist_size) > 0) {
          sizeRef.current = Math.min(8, Number(settings.shortlist_size))
          setSize(sizeRef.current)
        }
        if (settings) DIAG.on = Boolean(settings.debug)
        if (settings) maxRef.current = Number(settings.max_attached) > 0 ? Number(settings.max_attached) : Infinity
        if (settings && settings.catalog_version && settings.catalog_version !== namesRef.current.version) {
          namesRef.current.version = settings.catalog_version
          ctx.rest('/names').then((data) => {
            if (data && Array.isArray(data.skills)) namesRef.current = { version: data.version, list: buildNameIndex(data.skills) }
          }).catch(() => { namesRef.current.version = '' })
        }
        const skills = (context && context.skills) || []
        // "released": the skill's text left the context at a compaction. It is
        // not loaded any more, so it may be suggested, picked and attached again.
        loadedRef.current = new Set(skills.filter((skill) => skill.state !== 'released').map((skill) => String(skill.name || '').toLowerCase()))
        setLoaded(skills)
        if (actionsRef.current.rehold) actionsRef.current.rehold(skills, context && context.compacted_at)
        setJustSent((current) => (current.length ? current.filter((name) => !loadedRef.current.has(name.toLowerCase())) : current))
      } catch {
        /* backend off */
      }
    }
    void pull()
    const timer = setInterval(() => void pull(), 2000)
    return () => {
      dead = true
      clearInterval(timer)
    }
  }, [ctx, storedId])

  // A reload or a chat switch picks the held pile back up from the backend;
  // everything else on screen belongs to the previous chat and is cleared.
  useEffect(() => {
    let dead = false
    if (chatRef.current !== sessionId) {
      // Another chat: the pile still flying belongs to the one just left.
      clearTimeout(sentTimerRef.current)
      setFlying(null)
    }
    chatRef.current = sessionId
    setJustSent([])
    setLoaded([])
    loadedRef.current = new Set()
    declinedRef.current.clear()
    reheldRef.current.clear()
    pileReadyRef.current = false
    lockedRef.current = false
    setLocked(false)
    setMenu(null)
    ctx.rest(`/attach?session_id=${encodeURIComponent(storedId || '')}`)
      .then((data) => {
        if (dead) return
        const names = (data && Array.isArray(data.names)) ? data.names : []
        const autos = new Set(((data && Array.isArray(data.autos)) ? data.autos : []).map((name) => String(name).toLowerCase()))
        heldRef.current = names.map((name) => ({ name, auto: autos.has(String(name).toLowerCase()) }))
        setHeld(heldRef.current)
        pileReadyRef.current = true
      })
      .catch(() => { if (!dead) pileReadyRef.current = true })
    return () => {
      dead = true
    }
  }, [ctx, storedId])

  const searching = Boolean(search.term)
  // Visible chips first, then the spares that step in when one is picked.
  const candidates = searching ? search.rows : [
    ...(Array.isArray(ranking.candidates) ? ranking.candidates : []),
    ...(Array.isArray(ranking.spare) ? ranking.spare : []),
  ]
  const inContext = loaded.filter((skill) => skill.state !== 'released')
  const loadedNames = new Set([...inContext.map((skill) => String(skill.name || '').toLowerCase()), ...justSent.map((name) => name.toLowerCase())])
  const heldKeys = held.map((item) => item.name.toLowerCase())
  const taken = new Set([...heldKeys, ...typed, ...loadedNames])
  const suggestions = candidates
    .filter((candidate) => !taken.has(String(candidate.name || '').toLowerCase()))
    .slice(0, size)
    .map((candidate, index, row) => ({ ...candidate, color: rowColors(row.map((item) => item.name))[index], inContext: false }))
  const fanSource = []
  const seenFan = new Set()
  function pushFan(skill) {
    const key = String(skill && skill.name || '').toLowerCase()
    if (!key || seenFan.has(key)) return
    seenFan.add(key)
    fanSource.push(skill)
  }
  for (let index = justSent.length - 1; index >= 0; index -= 1) pushFan({ name: justSent[index], description: '' })
  // The fan shows what is in context now; skills a compaction removed leave it.
  for (let index = inContext.length - 1; index >= 0; index -= 1) pushFan(inContext[index])
  const fanSkills = fanSource.map((skill) => ({
    ...skill,
    color: chipColor(skill.name),
  }))

  const suggestionKey = fanSkills.map((skill) => skill.name).join('\n')

  useEffect(() => {
    const known = new Set(suggestionKey ? suggestionKey.split('\n') : [])
    setActiveName((current) => (current && known.has(current) ? current : null))
    setPinnedName((current) => (current && known.has(current) ? current : null))
  }, [suggestionKey])

  // Clicking a chip ATTACHES the skill: it contracts onto the left pile and the
  // next chip steps in. The prompt text is never touched (no Hermes internals
  // either): the pile is sent to the backend, and when the message goes out the
  // agent loads every held skill with it. See docs/designs/how-triage-works.md.
  function setPile(next) {
    heldRef.current = next
    setHeld(next)
    if (rerankRef.current) rerankRef.current()
    postChainRef.current = postChainRef.current
      .then(() => ctx.rest('/attach', {
        method: 'POST',
        // fresh: a chat with no stored id yet; only such a pile may be picked
        // up by that chat's first message.
        body: {
          session_id: storedId || '',
          names: next.map((item) => item.name),
          autos: next.filter((item) => item.auto).map((item) => item.name),
          fresh: Boolean(isNew),
        },
      }))
      .catch(() => {
        if (next.length) host.notify({ kind: 'warning', message: 'Triage backend is off; held skills will not be loaded.' })
      })
  }

  function isHeld(key) {
    return heldRef.current.some((item) => item.name.toLowerCase() === key)
  }

  function attach(name, auto) {
    const key = String(name || '').toLowerCase()
    if (!key || isHeld(key)) return
    if (lockedRef.current) return
    if (heldRef.current.length >= maxRef.current) {
      if (!auto) host.notify({ kind: 'info', message: `Triage attaches at most ${maxRef.current} skills per message (setting: Skills per message).` })
      return
    }
    if (!auto) haptic('tap')
    setPile(heldRef.current.concat({ name, auto: Boolean(auto) }))
    diag('attach', { name: key, auto: Boolean(auto) })
  }

  function detach(key, byUser) {
    const item = heldRef.current.find((entry) => entry.name.toLowerCase() === key)
    if (!item) return
    if (byUser && item.auto) declinedRef.current.add(key)
    setPile(heldRef.current.filter((entry) => entry !== item))
    diag('detach', { name: key, byUser: Boolean(byUser) })
  }

  // Sent: the suggestion row hides (busy) and the held pile flies right onto
  // the loaded fan. The backend already holds the names, so nothing is posted.
  function onSent() {
    const names = heldRef.current.map((item) => item.name)
    declinedRef.current.clear()
    lockedRef.current = false
    setLocked(false)
    setMenu(null)
    if (!names.length) return
    // Measured on the bar's own elements only.
    const root = dockRef.current
    const fan = root ? root.querySelector('.hsj-fan') : null
    const target = fan ? fan.getBoundingClientRect().right : (root ? root.getBoundingClientRect().right : 0)
    const mag = root ? root.querySelector('.hsj-mag') : null
    const dx = mag ? Math.max(0, target - mag.getBoundingClientRect().right) : 0
    setFlying({ names, dx })
    sentTimerRef.current = setTimeout(() => {
      heldRef.current = []
      setHeld([])
      setFlying(null)
      setJustSent((current) => current.concat(names.filter((name) => !current.includes(name))))
    }, 620)
    diag('sent', names)
  }
  onSentRef.current = onSent

  // Auto-attach: a draft-supported skill at or above the threshold joins the
  // pile for the user; it leaves again when the draft stops supporting it
  // (10 points below the threshold) or the draft is cleared without sending.
  // Manual picks and conversation chips are never touched.
  function reconcile(rows, rest, heldScores) {
    // Right after a send the prompt is empty while the reply runs: that is not
    // "the user cleared the draft", so nothing is detached then.
    if (busyRef.current && !rest) return
    const th = thresholdRef.current
    const autos = heldRef.current.filter((item) => item.auto)
    if (!rest) {
      for (const item of autos) detach(item.name.toLowerCase(), false)
      return
    }
    if (!(th > 0)) return
    const draftRows = new Map()
    for (const row of rows) {
      if (row && row.source !== 'conversation') draftRows.set(String(row.name || '').toLowerCase(), row)
    }
    // Held skills never come back as chips; the server reports their draft
    // score separately (held_scores). No score at all = the draft dropped it.
    const scores = new Map(Object.entries(heldScores || {}).map(([name, value]) => [name.toLowerCase(), Number(value)]))
    for (const item of autos) {
      const key = item.name.toLowerCase()
      const row = draftRows.get(key)
      const score = scores.has(key) ? scores.get(key) : (row ? Number(row.probability) : 0)
      if (score < th - AUTO_DETACH_MARGIN) detach(key, false)
    }
    for (const [key, row] of draftRows) {
      if (Number(row.probability) < th) continue
      // Only a skill whose own name or description the draft hits may load
      // itself; a body sentence or a generic verb alone is not enough.
      if (row.anchored === false) continue
      if (isHeld(key) || loadedRef.current.has(key) || declinedRef.current.has(key)) continue
      attach(row.name, true)
    }
  }
  reconcileRef.current = reconcile

  function commit(name) {
    if (name && searchRef.current) {
      // Picked from a "/term" search: hold it and take the typed "/term" out.
      attach(name, false)
      void clearSlashTerm()
      return undefined
    }
    if (name) return attach(name, false)
    return undefined
  }

  async function clearSlashTerm() {
    const sid = sessionId || null
    try {
      const text = (await host.composer.getDraft(sid)) || ''
      const next = text.replace(SLASH_TAIL_RE, '').replace(/\s+$/, '')
      if (next !== text) await host.composer.setDraft(sid, next ? `${next} ` : '')
    } catch {
      /* the chip is held either way */
    }
    searchRef.current = ''
    setSearch({ term: '', rows: [] })
  }

  commitRef.current = commit
  const detachRef = useRef(null)
  detachRef.current = detach

  function toggleMenu() {
    if (menu) return setMenu(null)
    if (lockedRef.current) return undefined
    ctx.rest('/favorites').then(takeFavs).catch(() => {})
    return setMenu({ id: Date.now(), query: searchRef.current || '', at: 0 })
  }

  // A row of the list: hold the skill, or take it out again. The list stays open.
  function pickFromMenu(name) {
    const key = String(name || '').toLowerCase()
    if (!key || loadedRef.current.has(key)) return
    if (isHeld(key)) detach(key, true)
    else attach(name, false)
    if (searchRef.current) void clearSlashTerm()
  }

  function clearAll() {
    haptic('tap')
    if (heldRef.current.length) setPile([])
    lockedRef.current = true
    setLocked(true)
    setMenu(null)
  }

  function unlock() {
    lockedRef.current = false
    setLocked(false)
    if (rerankRef.current) rerankRef.current()
  }

  function takeFavs(data) {
    if (data && Array.isArray(data.favorites)) setFavs({ favorites: data.favorites, most_used: Array.isArray(data.most_used) ? data.most_used : [] })
  }

  function toggleFavorite(name) {
    const key = String(name || '').toLowerCase()
    const on = !favs.favorites.some((item) => String(item).toLowerCase() === key)
    setFavs((current) => ({
      ...current,
      favorites: on ? current.favorites.concat(name) : current.favorites.filter((item) => String(item).toLowerCase() !== key),
    }))
    ctx.rest('/favorites', { method: 'POST', body: { name, on } }).then(takeFavs).catch(() => {})
  }

  // The card's Keep button: this chat wants the skill back after a compaction.
  function keepSkill(name) {
    const key = String(name || '').toLowerCase()
    const current = loaded.find((skill) => String(skill.name || '').toLowerCase() === key)
    const on = !(current && current.sticky)
    setLoaded((list) => list.map((skill) => (String(skill.name || '').toLowerCase() === key ? { ...skill, sticky: on } : skill)))
    ctx.rest('/sticky', { method: 'POST', body: { session_id: storedId || '', name, on } }).catch(() => {})
  }

  // A kept skill whose text left the context at a compaction goes back on the
  // pile once per compaction, so it loads with the next message.
  function rehold(skills, compactedAt) {
    // Wait for this chat's own pile, and never fight the trash lock or the per-message limit.
    if (!pileReadyRef.current || lockedRef.current) return
    const back = []
    for (const skill of skills || []) {
      if (!skill || !skill.sticky || skill.state !== 'released') continue
      const key = String(skill.name || '').toLowerCase()
      const mark = `${key}|${compactedAt || ''}`
      if (reheldRef.current.has(mark)) continue
      // Only a skill Triage can load itself (one loaded by Hermes alone may not be in the catalog).
      if (!namesRef.current.list.some((item) => item.lower === key)) continue
      if (heldRef.current.length + back.length >= maxRef.current) break
      reheldRef.current.add(mark)
      if (!isHeld(key)) back.push({ name: skill.name, auto: false })
    }
    if (back.length) setPile(heldRef.current.concat(back))
  }

  function onMenuKey(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      setMenu((current) => (current ? { ...current, at: Math.max(0, Math.min(menuRowsRef.current.length - 1, current.at + step)) } : current))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const row = menuRef.current ? menuRowsRef.current[menuRef.current.at] : null
      if (row) pickFromMenu(row.name)
    }
  }

  actionsRef.current = { toggleMenu, pick: pickFromMenu, clearAll, unlock, rehold }
  menuRef.current = menu


  async function viewBody(name) {
    if (body && body.name === name) {
      setBody(null)
      return
    }
    try {
      const data = await ctx.rest(`/skill/${encodeURIComponent(name)}`)
      setBody(data && data.success ? data : { name, body: data && data.error ? data.error : 'Unavailable.' })
    } catch {
      setBody({ name, body: 'Could not read this skill.' })
    }
  }

  function showSkill(name) {
    if (suppressRef.current || pinnedName) return
    if (activeName && performance.now() < settleRef.current) return
    if (name !== activeName) settleRef.current = performance.now() + 190
    setActiveName(name)
    setSpread(true)
  }

  function spreadFan() {
    if (suppressRef.current) return
    if (!spread && !pinnedName) settleRef.current = performance.now() + 190
    setSpread(true)
  }

  function collapseFan() {
    suppressRef.current = false
    if (pinnedName) return
    setSpread(false)
    setActiveName(null)
  }

  function pinSkill(name) {
    suppressRef.current = false
    settleRef.current = performance.now() + 190
    setPinnedName(name)
    setActiveName(name)
    setSpread(true)
  }

  const fanOpen = Boolean(spread || pinnedName || activeName)
  const fanOpenRef = useRef(false)
  fanOpenRef.current = fanOpen

  useEffect(() => {
    function dismiss() {
      if (!fanOpenRef.current) return
      suppressRef.current = true
      setPinnedName(null)
      setActiveName(null)
      setSpread(false)
    }
    function onKey(event) {
      if (event.key === 'Escape') dismiss()
    }
    function onPointerDown(event) {
      const target = event.target
      // The card is a popover outside the fan; a press inside it is not "outside".
      if (target && target.closest && target.closest('.hsj-fan, .hsj-detail-pop')) return
      dismiss()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointerDown)
    }
  }, [])

  const hasFan = held.length > 0 || suggestions.length > 0 || fanSkills.length > 0
  // While a reply runs the row stays hidden until you start typing again.
  const shownSuggestions = busy && !draft.trim() ? [] : suggestions
  const menuQuery = menu ? menu.query.trim() : ''
  const favKeys = new Set(favs.favorites.map((name) => String(name).toLowerCase()))
  function namedRows(names, section, seen) {
    const out = []
    for (const name of names) {
      const key = String(name).toLowerCase()
      if (seen.has(key)) continue
      seen.add(key)
      const known = namesRef.current.list.find((item) => item.lower === key)
      out.push({ name: known ? known.name : name, description: known ? known.description : '', section })
    }
    return out
  }
  let menuRows = []
  if (menu && menuQuery) {
    menuRows = localSearch(menuQuery, namesRef.current.list, [], MENU_LIMIT).map((row) => ({ ...row, section: 'Skills' }))
  } else if (menu) {
    const seen = new Set()
    menuRows = [
      ...namedRows(favs.favorites, 'Favourites', seen),
      ...namedRows(favs.most_used.slice(0, 5), 'Most used', seen),
      ...candidates.filter((row) => !seen.has(String(row.name || '').toLowerCase())).slice(0, size).map((row) => ({ ...row, section: 'Suggested' })),
    ]
  }
  menuRowsRef.current = menuRows
  // Why the row shows what it shows (debug setting only; one line per change).
  diag('row', { held: held.length, ranked: candidates.length, shown: shownSuggestions.length, busy })

  // The bar is a row in Hermes' composer.top slot, inside the composer card.
  // Only its own width is measured: it sizes the pile slot and the fan.
  useLayoutEffect(() => {
    const root = dockRef.current
    if (!root || typeof ResizeObserver !== 'function') return undefined
    function measure() {
      const width = Math.round(root.getBoundingClientRect().width)
      if (width && width !== barWidthRef.current) {
        barWidthRef.current = width
        setBarWidth(width)
      }
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(root)
    return () => observer.disconnect()
  }, [])

  // Keep the highlighted row inside the scrolling list.
  const menuAt = menu ? menu.at : -1
  useLayoutEffect(() => {
    const list = menuListRef.current
    const on = list ? list.querySelector('[data-selected="true"]') : null
    if (!on) return
    if (menuAt === 0) { list.scrollTop = 0; return }
    if (on.offsetTop < list.scrollTop) list.scrollTop = on.offsetTop
    else if (on.offsetTop + on.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = on.offsetTop + on.offsetHeight - list.clientHeight
  }, [menuAt])

  // The search list opens from the magnifier, which is its own trigger.
  const searchMenu = jsxs(Popover, {
    open: Boolean(menu && hasFan),
    onOpenChange: (open) => { if (!open) setMenu(null); else if (!menu) toggleMenu() },
    children: [
      jsx(PopoverTrigger, { asChild: true, children: jsx(Button, {
        type: 'button',
        variant: 'ghost',
        size: 'icon-xs',
        className: 'hsj-more',
        disabled: locked,
        title: 'Search all skills',
        'aria-label': 'Search all skills',
        'aria-expanded': menu ? 'true' : 'false',
        children: jsx(icons.Search, { 'aria-hidden': 'true' }),
      }) }, 'trigger'),
      menu && hasFan ? jsxs(PopoverContent, {
        variant: 'menu',
        side: 'top',
        align: 'start',
        sideOffset: 6,
        className: 'hsj-menu shadow-nous',
        'aria-label': 'Matching skills',
        collisionPadding: 8,
        children: [
          jsxs('div', { className: 'hsj-menu-head', 'data-slot': 'command-input-wrapper', children: [
            jsx(icons.Search, { 'aria-hidden': 'true' }, 'icon'),
            jsx('input', {
              className: 'hsj-menu-input',
              'data-slot': 'command-input',
              type: 'text',
              autoFocus: true,
              spellCheck: false,
              value: menu.query,
              placeholder: 'Search skills',
              'aria-label': 'Search skills',
              onChange: (event) => {
                const value = event.target.value
                setMenu((current) => (current ? { ...current, query: value, at: 0 } : current))
              },
              onKeyDown: onMenuKey,
            }, 'input'),
          ] }, 'head'),
          jsx('div', {
            className: 'hsj-menu-list dt-portal-scrollbar',
            ref: menuListRef,
            role: 'listbox',
            children: menuRows.length ? menuRows.map((row, index) => {
              const key = String(row.name || '').toLowerCase()
              const state = loadedNames.has(key) ? 'loaded' : (heldKeys.includes(key) ? 'held' : '')
              const starred = favKeys.has(key)
              const first = index === 0 || row.section !== menuRows[index - 1].section
              return [
                first ? jsx('div', { className: 'hsj-menu-heading', children: row.section === 'Skills' ? `Skills (${menuRows.length})` : row.section }, `heading-${row.section}`) : null,
                jsxs('div', {
                  role: 'option',
                  className: 'hsj-menu-row',
                  'data-selected': index === menu.at ? 'true' : 'false',
                  'aria-selected': index === menu.at ? 'true' : 'false',
                  'aria-disabled': state === 'loaded' ? 'true' : 'false',
                  title: row.description || row.name,
                  onClick: () => pickFromMenu(row.name),
                  onMouseMove: () => {
                    if (menuRef.current && menuRef.current.at !== index) setMenu((current) => (current ? { ...current, at: index } : current))
                  },
                  children: [
                    jsx('button', {
                      type: 'button',
                      className: starred ? 'hsj-menu-star on' : 'hsj-menu-star',
                      title: starred ? 'Remove from favourites' : 'Add to favourites',
                      'aria-label': starred ? `Remove ${row.name} from favourites` : `Add ${row.name} to favourites`,
                      'aria-pressed': starred ? 'true' : 'false',
                      onClick: (event) => {
                        event.stopPropagation()
                        toggleFavorite(row.name)
                      },
                      children: STAR,
                    }, 'star'),
                    jsx('span', { className: 'hsj-menu-name', children: row.name }, 'name'),
                    jsx('span', { className: 'hsj-menu-desc', children: row.description || '' }, 'desc'),
                    state === 'held' ? jsx(icons.Check, { className: 'hsj-menu-check', 'aria-hidden': 'true' }, 'state')
                      : (state === 'loaded' ? jsx('span', { className: 'hsj-menu-note', children: 'loaded' }, 'state') : null),
                  ],
                }, `row-${row.name}`),
              ]
            }) : jsx('div', { className: 'hsj-menu-heading', children: menuQuery ? 'No skill matches' : 'Type to search every skill' }, 'empty'),
          }, 'list'),
        ],
      }, 'content') : null,
    ],
  }, 'menu')

  return jsxs('div', {
    className: 'hsj-chrome',
    ref: dockRef,
    hidden: !hasFan && !hint,
    children: [
          hasFan ? jsxs('div', {
            className: 'hsj-bar',
            children: [
              jsxs('div', {
                className: 'hsj-suggest',
                children: [
                  jsxs('div', {
                    className: 'hsj-chips',
                    children: [
                      locked
                        ? jsx(Button, { type: 'button', variant: 'outline', size: 'icon-xs', className: 'hsj-unlock', onClick: unlock, title: 'No skills go with this message. Click to allow skills again.', 'aria-label': 'Allow skills again', children: jsx(icons.Lock, { 'aria-hidden': 'true' }) })
                        : jsx(Button, { type: 'button', variant: 'ghost', size: 'icon-xs', className: 'hsj-trash', onClick: clearAll, disabled: held.length === 0, title: held.length ? 'Remove all held skills and take none with this message' : 'No skills held', 'aria-label': 'Remove all held skills and take none with this message', children: jsx(icons.Trash2, { 'aria-hidden': 'true' }) }),
                      // The shelf keeps its slot even when empty, so chips never shift.
                      jsx(Magazine, { items: held, flying, width: magazineWidth(barWidth), locked, onDetach: (key) => detach(key, true) }),
                      searchMenu,
                      jsx('div', { className: 'hsj-chip-list', children: shownSuggestions.map((candidate) => jsxs('button', {
                        type: 'button',
                        disabled: locked,
                        className: candidate.source === 'conversation' ? 'hsj-suggest-chip hsj-from-convo'
                          : (candidate.source === 'search' ? 'hsj-suggest-chip hsj-search-hit' : 'hsj-suggest-chip'),
                        'data-name': candidate.name,
                        onClick: () => { if (commitRef.current) void commitRef.current(candidate.name) },
                        title: `${candidate.name} — hold it for your next message (nothing is sent). ${candidate.source === 'conversation' ? 'From the conversation' : 'From your draft'}${candidate.why ? ' · ' + candidate.why : ''}`,
                        style: { '--identity': candidate.color },
                        children: [
                          jsx('strong', { children: candidate.source === 'search' ? '/' : formatPct(candidate.probability) }),
                          jsx('span', { className: 'hsj-chip-name', children: candidate.name }),
                          candidate.inContext ? jsx('span', { className: 'hsj-ctx', children: 'ctx' }) : null,
                        ],
                      }, candidate.name)) }),
                    ],
                  }),
                ],
              }),
              fanSkills.length ? jsx(Fan, {
                skills: fanSkills,
                barWidth,
                activeName,
                pinnedName,
                body,
                onShow: showSkill,
                onSpread: spreadFan,
                onCollapse: collapseFan,
                onPin: pinSkill,
                onView: viewBody,
                onKeep: keepSkill,
              }) : null,
            ],
          }) : null,
      hint ? jsx('div', { className: 'hsj-quiet', children: hint }) : null,
    ],
  })
}

const MAG_WIDTH = 176
// A held skill's pill; a click takes it off the pile.
function heldPill(item, onDetach, extraClass, style) {
  return jsxs('button', {
    type: 'button',
    className: ['hsj-held', item.auto ? 'hsj-held-auto' : '', extraClass || ''].join(' ').trim(),
    'data-name': item.name,
    title: `${item.auto ? 'Auto-attached' : 'Attached'} to your next message. Click to take it out.`,
    style,
    onClick: (event) => { event.stopPropagation(); if (onDetach) onDetach(String(item.name || '').toLowerCase()) },
    children: [BOLT, jsx('span', { children: item.name })],
  }, `held-${item.name}`)
}

// The pile keeps one fixed slot in the row: the newest pill and a count. The
// card clips anything that rises out of the row, so hovering it opens an SDK
// Popover with every held skill instead of spreading the stack upward.
function Magazine({ items, flying, width = MAG_WIDTH, locked, onDetach }) {
  const [open, setOpen] = useState(false)
  const closeRef = useRef(0)
  useEffect(() => () => clearTimeout(closeRef.current), [])
  const count = items.length
  if (!count) {
    return jsx('div', {
      className: locked ? 'hsj-mag hsj-mag-empty hsj-mag-locked' : 'hsj-mag hsj-mag-empty',
      style: { width: `${width}px` },
      title: locked ? 'No skills go with this message' : 'Skills you pick load here and go with your next message',
      children: locked ? 'No skills this message' : null,
    })
  }
  const top = items[count - 1]
  const fly = flying && flying.names.includes(top.name)
  const show = () => { clearTimeout(closeRef.current); if (count > 1) setOpen(true) }
  const hide = () => { clearTimeout(closeRef.current); closeRef.current = setTimeout(() => setOpen(false), 180) }
  return jsxs(Popover, {
    open: open && count > 1 && !flying,
    // Clicks on the slot remove pills; only hover opens the list.
    onOpenChange: (next) => { if (!next) setOpen(false) },
    children: [
      jsx(PopoverTrigger, { asChild: true, children: jsxs('div', {
        className: 'hsj-mag',
        style: { width: `${width}px` },
        onMouseEnter: show,
        onMouseLeave: hide,
        children: [
          heldPill(top, onDetach, 'hsj-held-top', fly ? { transform: `translateX(${flying.dx}px) scale(.8)`, opacity: 0 } : undefined),
          count > 1 ? jsx(Badge, { variant: 'solid', size: 'xs', className: 'hsj-mag-count', title: `${count} skills held for the next message`, children: String(count) }, 'count') : null,
        ],
      }) }, 'trigger'),
      jsx(PopoverContent, {
        side: 'top',
        align: 'start',
        sideOffset: 6,
        collisionPadding: 8,
        className: 'hsj-pop hsj-pile',
        'aria-label': 'Held skills',
        onMouseEnter: show,
        onMouseLeave: hide,
        onOpenAutoFocus: (event) => event.preventDefault(),
        onCloseAutoFocus: (event) => event.preventDefault(),
        children: items.slice().reverse().map((item) => heldPill(item, onDetach, '', undefined)),
      }, 'pile'),
    ],
  })
}

// The loaded-skill fan (v3). Every chip has the same width and the newest sits
// on top at the left, so each later chip shows an equal slice of its END (names
// are right-aligned). Hover is decided by fixed strips that match those
// visible slices, not by the chips, so the chip that lifts can never slide out
// from under the pointer and flicker. The fan never grows past FAN_MAX: it
// condenses as skills are added. Released skills (their text left the context
// at a compaction) are faint and dashed; loaded again after it, green.
function Fan({ skills, barWidth, activeName, pinnedName, body, onShow, onSpread, onCollapse, onPin, onView, onKeep }) {
  const rootRef = useRef(null)
  const live = useRef({})
  live.current = { onShow, onSpread, onCollapse, onPin, onView, onKeep, pinnedName }
  const layout = fanLayout(skills.length, fanMaxWidth(barWidth), skills.map((skill) => fanChipWidth(skill.name)))
  const activeIndex = skills.findIndex((skill) => skill.name === activeName)
  const shown = activeIndex >= 0 ? skills[activeIndex] : null
  // The card is an SDK Popover over the hovered strip (the composer card clips
  // anything that rises out of the row). Leaving the fan closes it after a
  // short delay, so the pointer can move from the strip into the card.
  const closeRef = useRef(0)
  const holdOpen = () => clearTimeout(closeRef.current)
  const closeSoon = () => { clearTimeout(closeRef.current); closeRef.current = setTimeout(() => live.current.onCollapse(), 220) }
  useEffect(() => () => clearTimeout(closeRef.current), [])

  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root) return undefined
    function laneName(target) {
      const lane = target && target.closest ? target.closest('.hsj-fan-lane') : null
      return lane ? lane.getAttribute('data-name') || '' : ''
    }
    function onOver(event) {
      const name = laneName(event.target)
      if (name) {
        clearTimeout(closeRef.current)
        live.current.onSpread()
        live.current.onShow(name)
      }
    }
    function onLeave() {
      clearTimeout(closeRef.current)
      closeRef.current = setTimeout(() => live.current.onCollapse(), 220)
    }
    function onClick(event) {
      const name = laneName(event.target)
      if (name) live.current.onPin(name)
    }
    root.addEventListener('pointerover', onOver)
    root.addEventListener('pointerleave', onLeave)
    root.addEventListener('click', onClick)
    return () => {
      root.removeEventListener('pointerover', onOver)
      root.removeEventListener('pointerleave', onLeave)
      root.removeEventListener('click', onClick)
    }
  }, [])

  const lane = shown ? layout.lanes[activeIndex] : null
  const card = jsxs(Popover, {
    open: Boolean(shown),
    onOpenChange: (next) => { if (!next) live.current.onCollapse() },
    children: [
      jsx(PopoverTrigger, { asChild: true, children: jsx('span', {
        className: 'hsj-card-anchor',
        'aria-hidden': 'true',
        style: lane ? { left: `${lane.left}px`, width: `${lane.width}px` } : { left: '0px', width: '1px' },
      // A new anchor per skill, so the card re-positions over each strip.
      }) }, `anchor-${shown ? shown.name : ''}`),
      shown ? jsx(PopoverContent, {
        side: 'top',
        align: 'center',
        sideOffset: 8,
        collisionPadding: 8,
        className: 'hsj-pop hsj-detail-pop',
        'aria-label': shown.name,
        onMouseEnter: holdOpen,
        onMouseLeave: () => { if (!live.current.pinnedName) closeSoon() },
        onOpenAutoFocus: (event) => event.preventDefault(),
        onCloseAutoFocus: (event) => event.preventDefault(),
        children: jsxs('article', {
          className: body && body.name === shown.name ? 'hsj-detail open hsj-detail-body' : 'hsj-detail open',
          style: { '--identity': shown.color },
          children: [
            jsxs('div', {
              className: 'hsj-detail-head',
              children: [
                jsx('span', { className: 'hsj-detail-dot' }, 'dot'),
                jsx('span', { className: 'hsj-detail-name', children: shown.name }, 'name'),
                jsx('span', { className: 'hsj-tag', children: categoryLabel(shown) }, 'tag'),
              ],
            }, 'head'),
            jsx('p', { className: 'hsj-detail-desc', children: shown.description || 'No description.' }, 'desc'),
            jsxs('div', {
              className: 'hsj-detail-foot',
              children: [
                jsx('span', { children: detailMeta(shown) }, 'meta'),
                jsxs('span', { className: 'hsj-detail-actions', children: [
                  jsxs(Button, {
                    type: 'button',
                    variant: 'outline',
                    size: 'xs',
                    className: shown.sticky ? 'hsj-keep on' : 'hsj-keep',
                    'data-name': shown.name,
                    'aria-pressed': shown.sticky ? 'true' : 'false',
                    title: shown.sticky ? 'Comes back to your pile after the chat is compacted. Click to stop.' : 'Put this skill back on your pile after the chat is compacted',
                    onClick: () => live.current.onKeep(shown.name),
                    children: [jsx(icons.Pin, { 'aria-hidden': 'true' }, 'icon'), shown.sticky ? 'Kept' : 'Keep'],
                  }, 'keep'),
                  jsx(Button, { type: 'button', variant: 'outline', size: 'xs', className: 'hsj-view', 'data-name': shown.name, onClick: () => live.current.onView(shown.name), children: 'View body' }, 'view'),
                ] }, 'actions'),
              ],
            }, 'foot'),
            body && body.name === shown.name ? jsx('pre', { className: 'hsj-body', children: body.body || '' }, 'body') : null,
          ],
        }),
      }, 'card') : null,
    ],
  })

  return jsxs('div', {
    className: 'hsj-fan',
    ref: rootRef,
    style: { width: `${layout.width}px` },
    children: [
      jsxs('div', {
        className: 'hsj-fan-row',
        style: { width: `${layout.width}px` },
        children: [
          ...skills.map((skill, index) => jsx('div', {
            className: [
              'hsj-fan-chip',
              skill.name === activeName ? 'active' : '',
              skill.state === 'released' ? 'released' : '',
              skill.state === 'reloaded' ? 'reloaded' : '',
            ].join(' ').trim(),
            style: { left: `${layout.left[index]}px`, width: `${layout.widths[index]}px`, zIndex: skills.length - index, '--identity': skill.color },
            'aria-hidden': 'true',
            children: jsx('span', { className: 'hsj-fan-name', children: skill.name }),
          }, `chip-${skill.name}`)),
          card,
          ...skills.map((skill, index) => jsx('button', {
            type: 'button',
            className: 'hsj-fan-lane',
            'data-name': skill.name,
            'aria-label': pinnedName === skill.name ? `${skill.name}, pinned` : skill.name,
            'aria-pressed': skill.name === pinnedName ? 'true' : 'false',
            style: { left: `${layout.lanes[index].left}px`, width: `${layout.lanes[index].width}px` },
          }, `lane-${skill.name}`)),
        ],
      }),
    ],
  })
}


function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return
  const style = document.createElement('style')
  style.id = STYLE_ID
  style.textContent = `
.hsj-chrome { display: block; width: 100%; min-width: 0; color: var(--ui-text-secondary); }
.hsj-chrome[hidden] { display: none; }
/* Chips overlap in the fan and sit on whatever the composer card paints, so their fill
   is the theme's elevated colour made opaque. The popovers carry the same token. */
.hsj-chrome, .hsj-pop { --hsj-surface: rgb(from var(--ui-bg-elevated) r g b / 1); }
.hsj-bar { display: flex; align-items: center; justify-content: space-between; gap: 16px; width: 100%; min-width: 0; }
.hsj-suggest { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.hsj-hint, .hsj-status { font-size: 0.6875rem; color: var(--ui-text-tertiary); }
.hsj-chips { display: flex; flex-wrap: nowrap; gap: 6px; align-items: center; width: 100%; min-width: 0; }
.hsj-chip-list { display: flex; flex-wrap: nowrap; gap: 6px; align-items: center; flex: 1 1 auto; min-width: 0; overflow: hidden; padding: 2px 0; }
.hsj-suggest-chip { flex: 0 1 auto; min-width: 64px; max-width: 260px; white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--ui-stroke-secondary); border-radius: 3px; background: var(--hsj-surface); color: var(--ui-text-secondary); font: inherit; font-size: 0.75rem; line-height: 1.3; padding: 2px 8px; cursor: pointer; }
.hsj-suggest-chip { border-color: var(--identity, var(--ui-stroke-secondary)); background: color-mix(in srgb, var(--identity, var(--ui-stroke-secondary)) 12%, var(--hsj-surface)); }
.hsj-suggest-chip strong { font-weight: 600; }
.hsj-suggest-chip.hsj-from-convo { border-style: dashed; }
.hsj-suggest-chip strong { flex: none; }
.hsj-chip-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.hsj-suggest-chip.hsj-search-hit strong { opacity: .7; }
.hsj-held { display: inline-flex; align-items: center; gap: 4px; border: 1px solid color-mix(in srgb, var(--ui-accent) 55%, transparent); border-radius: 3px; background: color-mix(in srgb, var(--ui-accent) 14%, var(--hsj-surface)); color: var(--ui-accent); font: inherit; font-size: 0.75rem; line-height: 1.3; padding: 2px 8px; cursor: pointer; transition: transform 520ms cubic-bezier(.2,.8,.2,1), opacity 520ms ease-out; }
.hsj-held svg { width: 12px; height: 12px; flex: none; }
.hsj-held.hsj-held-auto { border-style: dashed; }
.hsj-held:hover { border-color: var(--ui-accent); }
.hsj-mag { position: relative; flex: 0 0 auto; height: 22px; align-self: center; }
.hsj-pile { display: flex; flex-direction: column; gap: 4px; width: max-content; max-width: 320px; max-height: 50vh; overflow-y: auto; padding: 6px; }
.hsj-pile .hsj-held { justify-content: flex-start; }
.hsj-mag-empty { box-sizing: border-box; border: 1px dashed var(--ui-stroke-secondary); border-radius: 3px; opacity: .35; }
.hsj-mag .hsj-held { position: absolute; left: 0; bottom: 0; width: 100%; height: 22px; overflow: hidden; padding: 0 8px; box-sizing: border-box; transform-origin: left center; transition: transform 520ms cubic-bezier(.2,.8,.2,1), opacity 520ms ease-out; }
.hsj-mag .hsj-held span { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.hsj-mag .hsj-held-top { animation: hsj-load 260ms cubic-bezier(.2,.8,.2,1); }
@keyframes hsj-load { from { margin-left: 34px; opacity: 0; } to { margin-left: 0; opacity: 1; } }
.hsj-mag-count { position: absolute; right: -6px; top: -7px; z-index: 99; pointer-events: none; }
.hsj-ctx { font-size: 0.6rem; letter-spacing: 0.04em; border: 1px solid currentColor; border-radius: 3px; padding: 0 3px; }
.hsj-quiet { font-size: 0.6875rem; color: var(--ui-text-tertiary); }
.hsj-fan { position: relative; margin-left: auto; flex: 0 0 auto; align-self: center; max-width: 100%; }
.hsj-card-anchor { position: absolute; top: 0; height: 22px; pointer-events: none; }
/* The card's own frame is the article; the popover around it paints nothing. */
.hsj-detail-pop { width: 324px; max-width: calc(100vw - 16px); padding: 0 !important; border: 0 !important; background: transparent !important; box-shadow: none !important; }
.hsj-fan-row { position: relative; height: 22px; margin-left: auto; }
.hsj-fan-lane { position: absolute; top: 0; height: 22px; z-index: 1000; padding: 0; border: 0; background: transparent; cursor: pointer; }
.hsj-fan-chip.released { border-style: dashed; color: var(--ui-text-tertiary); }
.hsj-fan-chip.reloaded { border-color: color-mix(in srgb, var(--ui-green) 70%, var(--ui-stroke-secondary)); color: var(--ui-green); }
.hsj-fan-chip { position: absolute; top: 0; display: flex; justify-content: flex-end; align-items: center; gap: 6px; width: 140px; box-sizing: border-box; height: 22px; padding: 0 8px; pointer-events: none; overflow: hidden; border: 1px solid var(--ui-stroke-secondary); border-radius: 3px; background: var(--hsj-surface); color: var(--ui-text-secondary); font: 0.65rem/20px var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); transition: left 180ms ease-out, transform 160ms ease-out, width 160ms ease-out, opacity 200ms, border-color 180ms ease-out; }
.hsj-fan-chip.active { border-color: var(--identity, var(--ui-accent)); }
.hsj-fan-name { display: block; flex: 0 1 auto; min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: right; }
.hsj-fan-pct { display: block; flex: 0 0 auto; border-radius: 3px; padding: 0 4px; background: color-mix(in srgb, var(--identity, var(--ui-accent)) 12%, transparent); color: var(--identity, var(--ui-accent)); font: 0.6rem/14px var(--font-sans, sans-serif); }
.hsj-chevron { display: block; flex: 0 0 6px; width: 6px; height: 6px; position: relative; }
.hsj-chevron:after { content: ''; position: absolute; left: 0; top: 0; width: 4px; height: 4px; border-top: 1px solid var(--ui-text-tertiary); border-right: 1px solid var(--ui-text-tertiary); transform: rotate(-45deg); }
.hsj-fan-chip.active .hsj-chevron:after { border-color: var(--identity, var(--ui-accent)); }
.hsj-detail { width: 100%; margin-left: auto; box-sizing: border-box; padding: 0 10px; border: 1px solid transparent; border-radius: 8px; background: var(--hsj-surface); max-height: 0; opacity: 0; overflow: hidden; transform: translateY(8px); pointer-events: none; transition: max-height 180ms ease-out, opacity 180ms ease-out, transform 180ms ease-out, margin 180ms ease-out, padding 180ms ease-out; }
.hsj-detail.open { max-height: 280px; opacity: 1; transform: translateY(0); padding: 10px; pointer-events: auto; border-color: color-mix(in srgb, var(--identity, var(--ui-accent)) 50%, var(--ui-stroke-secondary)); }
.hsj-detail-body { max-height: 460px; }
.hsj-detail-head { display: flex; align-items: center; gap: 6px; min-height: 18px; }
.hsj-detail-dot { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: var(--identity, var(--ui-accent)); }
.hsj-detail-name { flex: 1 1 auto; min-width: 0; font: 600 0.65rem/16px var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hsj-detail .hsj-tag { margin-left: auto; border: 0; background: color-mix(in srgb, var(--identity, var(--ui-accent)) 12%, transparent); color: var(--identity, var(--ui-accent)); font-size: 0.6rem; letter-spacing: 0.02em; }
.hsj-detail-desc { margin: 9px 0 10px; font-size: 0.75rem; line-height: 17px; color: var(--ui-text-secondary); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.hsj-detail-foot { padding-top: 8px; border-top: 1px solid var(--ui-stroke-secondary); display: flex; gap: 6px; align-items: center; justify-content: space-between; color: var(--ui-text-tertiary); font-size: 0.65rem; line-height: 20px; }
.hsj-card { padding: 6px 8px 8px; display: flex; flex-direction: column; gap: 4px; }
.hsj-card-top { display: flex; align-items: center; gap: 4px; min-height: 16px; }
.hsj-tag { flex: 0 0 auto; font-size: 0.6rem; letter-spacing: 0.04em; border: 1px solid var(--ui-stroke-secondary); border-radius: 3px; padding: 0 3px; color: var(--ui-text-tertiary); }
.hsj-desc { margin: 0; font-size: 0.6875rem; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.hsj-meta { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 0.65rem; color: var(--ui-text-tertiary); }
.hsj-body { margin: 0; max-height: 140px; overflow: auto; white-space: pre-wrap; font-size: 0.65rem; line-height: 1.35; color: var(--ui-text-secondary); }
.hsj-trash, .hsj-unlock, .hsj-more { flex: 0 0 auto; }
.hsj-unlock { color: var(--ui-accent); border-color: var(--ui-accent); }
.hsj-suggest-chip:disabled { cursor: default; border-color: var(--ui-stroke-secondary); background: var(--hsj-surface); color: var(--ui-text-tertiary); opacity: .55; }
.hsj-mag-locked { display: flex; align-items: center; padding: 0 8px; border-style: solid; opacity: 1; font-size: 0.65rem; color: var(--ui-text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hsj-menu { width: 26rem; max-width: calc(100vw - 16px); padding: 0; overflow: hidden; border-radius: var(--radius-xl, 0); border: 1px solid var(--stroke-nous, var(--ui-stroke-secondary)); background: var(--ui-chat-bubble-background, var(--ui-bg-elevated)); color: var(--ui-text-primary, var(--ui-text-secondary)); font-size: 0.75rem; }
.hsj-menu-head { display: flex; align-items: center; gap: 8px; height: 2.75rem; padding: 0 12px; border-bottom: 1px solid var(--border, var(--ui-stroke-secondary)); background: color-mix(in srgb, var(--ui-text-primary, currentColor) 4%, transparent); }
.hsj-menu-head:focus-within { border-bottom-color: var(--ring, var(--theme-primary, var(--ui-accent))); }
.hsj-menu-head svg { width: 16px; height: 16px; flex: none; color: var(--ui-text-tertiary); }
.hsj-menu-input { flex: 1 1 auto; min-width: 0; height: 2.5rem; border: 0; outline: 0; background: transparent; color: inherit; font: inherit; }
.hsj-menu-input::placeholder { color: var(--ui-text-tertiary); }
.hsj-menu-heading { padding: 10px 14px 4px; font-size: 0.64rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.16em; color: var(--theme-primary, var(--ui-accent)); }
.hsj-menu-list { position: relative; height: min(15rem, 56vh); overflow-x: hidden; overflow-y: auto; padding: 0 4px 4px; }
.hsj-menu-row { display: flex; align-items: center; gap: 8px; width: 100%; box-sizing: border-box; padding: 4px 8px; border: 0; border-radius: var(--radius-sm, 0); background: transparent; color: inherit; font: inherit; text-align: left; cursor: default; }
.hsj-menu-row[data-selected="true"] { background: var(--accent, var(--ui-control-active-background)); color: var(--accent-foreground, inherit); }
.hsj-menu-row[aria-disabled="true"] { opacity: .5; }
.hsj-menu-row svg { width: 14px; height: 14px; flex: none; color: var(--ui-text-tertiary); }
.hsj-menu-row svg.hsj-menu-check { margin-left: auto; color: var(--theme-primary, var(--ui-accent)); }
.hsj-menu-name { flex: none; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hsj-menu-desc { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ui-text-tertiary); }
.hsj-menu-note { flex: none; margin-left: auto; color: var(--ui-text-tertiary); }
.hsj-menu-list .hsj-menu-heading { padding: 10px 10px 4px; }
.hsj-menu-star { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 3px; background: transparent; color: var(--ui-text-tertiary); cursor: pointer; }
.hsj-menu-star:hover { color: var(--ui-text-primary, var(--ui-text-secondary)); }
.hsj-menu-star.on { color: var(--ui-yellow); }
.hsj-menu-row .hsj-menu-star svg { color: inherit; }
.hsj-menu-star.on svg { fill: currentColor; }
.hsj-detail-actions { display: inline-flex; align-items: center; gap: 6px; flex: none; }
.hsj-keep.on { color: var(--ui-accent); border-color: var(--ui-accent); }
.hsj-suggest-chip:focus-visible, .hsj-held:focus-visible, .hsj-fan-lane:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) {
  .hsj-fan-chip, .hsj-detail, .hsj-held { transition: none; }
  .hsj-mag .hsj-held-top { animation: none; }
}
`
  document.head.appendChild(style)
}

// Hermes' stacked loader handles at most 5 leading /skill tokens the user types.
// Skills on the left pile are not tokens; their cap is the max_attached setting.
const HERMES_STACK_LIMIT = 5
const BOLT = jsx(icons.Zap, { 'aria-hidden': 'true' })
const STAR = jsx('svg', {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': 'true',
  children: jsx('polygon', { points: '12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2' }),
})
const AUTO_DETACH_MARGIN = 0.1
const TOKEN_RE = /^\/(?:triage:)?([A-Za-z0-9_-]+)$/
// The "/term" being typed at the end of the draft (no space after it yet).
const SLASH_TAIL_RE = /(^|\s)\/(?:triage:)?([A-Za-z0-9_-]*)$/

// ---- instant /term search, in the app (no server round trip) -------------
// fzf / VS Code style: whole name > name start > a name part's start >
// letters in order with word-start and run bonuses (k-c-e finds
// kotlin-coroutines-expert); a trigram overlap catches typos (andriod).

function buildNameIndex(rows) {
  return rows.map(([name, description]) => {
    const lower = String(name).toLowerCase()
    return { name, description: description || '', lower, parts: lower.split(/[-_]/), grams: trigrams(lower) }
  })
}

function trigrams(text) {
  const padded = `  ${text} `
  const out = new Set()
  for (let i = 0; i < padded.length - 2; i += 1) out.add(padded.slice(i, i + 3))
  return out
}

function subsequenceScore(term, lower) {
  let score = 0
  let at = 0
  let run = 0
  for (const char of term) {
    const found = lower.indexOf(char, at)
    if (found < 0) return 0
    const boundary = found === 0 || lower[found - 1] === '-' || lower[found - 1] === '_'
    run = found === at ? run + 1 : 0
    score += 1 + (boundary ? 3 : 0) + run * 2 - Math.min(3, found - at) * 0.5
    at = found + 1
  }
  return score / (term.length * 6)
}

// "architec-senior" finds "senior-architect": every typed part starts a
// different part of the name (or is one typo away from it), in any order.
function partsAnyOrder(term, parts) {
  const wanted = term.split(/[-_\s]+/).filter(Boolean)
  if (wanted.length < 2 || wanted.length > parts.length) return false
  const used = new Set()
  return wanted.every((piece) => {
    const at = parts.findIndex((part, index) => !used.has(index)
      && (part.startsWith(piece) || (piece.length >= 4 && editDistance(piece, part.slice(0, piece.length), 1) <= 1)))
    if (at < 0) return false
    used.add(at)
    return true
  })
}

function editDistance(a, b, cap) {
  // Optimal string alignment (Damerau-Levenshtein with adjacent swaps).
  if (Math.abs(a.length - b.length) > cap) return cap + 1
  const rows = [Array.from({ length: b.length + 1 }, (_, j) => j)]
  for (let i = 1; i <= a.length; i += 1) {
    rows[i] = [i]
    let low = i
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      let value = Math.min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) value = Math.min(value, rows[i - 2][j - 2] + 1)
      rows[i][j] = value
      low = Math.min(low, value)
    }
    if (low > cap) return cap + 1
  }
  return rows[a.length][b.length]
}

function localSearch(rawTerm, list, exclude, limit) {
  const term = String(rawTerm || '').toLowerCase().replace(/^triage:/, '')
  if (!term) return []
  const skip = new Set((exclude || []).map((name) => String(name).toLowerCase()))
  const scored = []
  for (const item of list) {
    if (skip.has(item.lower)) continue
    let score = 0
    let why = ''
    if (item.lower === term) { score = 1; why = 'name' }
    else if (item.lower.startsWith(term)) { score = 0.95 - Math.min(0.2, (item.lower.length - term.length) * 0.005); why = 'name starts' }
    else if (item.parts.some((part) => part.startsWith(term))) { score = 0.85; why = 'name part' }
    else if (partsAnyOrder(term, item.parts)) { score = 0.8; why = 'name parts, any order' }
    else if (item.lower.includes(term)) { score = 0.75; why = 'in name' }
    else {
      const sub = subsequenceScore(term, item.lower)
      if (sub > 0.35) { score = 0.5 + 0.2 * Math.min(1, sub); why = 'letters in order' }
    }
    if (score) scored.push({ item, score, why })
  }
  if (term.length >= 4) {
    // Typos: edit distance to the name or one of its parts (a swapped pair
    // counts as one edit), after a cheap trigram check.
    const grams = trigrams(term)
    const allowed = term.length >= 8 ? 2 : 1
    for (const item of list) {
      if (skip.has(item.lower) || scored.some((hit) => hit.item === item)) continue
      let shared = 0
      for (const gram of grams) if (item.grams.has(gram)) shared += 1
      if (shared / grams.size < 0.3) continue
      let best = editDistance(term, item.lower, allowed)
      for (const part of item.parts) best = Math.min(best, editDistance(term, part, allowed))
      if (best <= allowed) scored.push({ item, score: 0.62 - 0.1 * best, why: 'close to name' })
    }
  }
  scored.sort((a, b) => b.score - a.score || a.item.name.localeCompare(b.item.name))
  return scored.slice(0, limit).map(({ item, score, why }) => ({
    name: item.name, description: item.description, probability: score, why, source: 'search', anchored: true,
  }))
}

const MENU_LIMIT = 200

function slashTerm(text) {
  const match = SLASH_TAIL_RE.exec(String(text || ''))
  return match ? match[2] : ''
}

/** Leading /skill tokens (Hermes stacks up to 5) and the text after them. */
function splitTokens(text) {
  const words = String(text || '').trim().split(/\s+/).filter(Boolean)
  const tokens = []
  let index = 0
  for (; index < words.length && tokens.length < HERMES_STACK_LIMIT; index += 1) {
    const match = TOKEN_RE.exec(words[index])
    if (!match) break
    tokens.push(match[1].toLowerCase())
  }
  return { tokens, rest: words.slice(index).join(' ') }
}

function wordsKey(text) {
  return String(text || '').toLowerCase().split(/\s+/).filter(Boolean).join(' ')
}

// Each skill keeps one colour of the host's palette, chosen from its name, so
// a chip does not change colour when the row refills. Green is left out: it
// marks a skill loaded again after a compaction.
const CHIP_COLORS = ['var(--ui-accent)', 'var(--ui-blue)', 'var(--ui-purple)', 'var(--ui-yellow)', 'var(--ui-cyan)', 'var(--ui-orange)']

function formatPct(probability) {
  const pct = Math.max(0, Number(probability) || 0) * 100
  if (pct >= 10) return `${Math.round(pct)}%`
  if (pct >= 1) return `${pct.toFixed(1)}%`
  if (pct > 0) return '<1%'
  return '0%'
}

function colorSlot(name) {
  const key = String(name || '').toLowerCase()
  let hash = 0
  for (let index = 0; index < key.length; index += 1) hash = (hash * 31 + key.charCodeAt(index)) >>> 0
  return hash % CHIP_COLORS.length
}

function chipColor(name) {
  return CHIP_COLORS[colorSlot(name)]
}

// Outline colours for one row of chips. A skill starts from its own colour;
// when that one is already on the row it takes the next free one, so no two
// chips match until the row is longer than the palette.
function rowColors(names) {
  const used = new Set()
  return names.map((name) => {
    if (used.size === CHIP_COLORS.length) used.clear()
    let slot = colorSlot(name)
    while (used.has(slot)) slot = (slot + 1) % CHIP_COLORS.length
    used.add(slot)
    return CHIP_COLORS[slot]
  })
}

// Fan v3 geometry: equal chips; the fan condenses within its width, which
// is FAN_MAX on a wide window and shrinks with a narrow one.
const FAN_MAX = 300
const FAN_MIN = 150
const FAN_CHIP_W = 140
const FAN_CHIP_MIN = 96
const FAN_STEP_MAX = 56

// Share of the bar each pile may take, so the suggestions keep the middle.
function fanMaxWidth(barWidth) {
  if (!barWidth) return FAN_MAX
  return Math.round(Math.max(FAN_MIN, Math.min(FAN_MAX, barWidth * 0.32)))
}

function magazineWidth(barWidth) {
  if (!barWidth) return MAG_WIDTH
  return Math.round(Math.max(96, Math.min(MAG_WIDTH, barWidth * 0.2)))
}

// Width a loaded chip needs to show its whole name (0.65rem monospace, padding, border).
function fanChipWidth(name) {
  return Math.ceil(String(name || '').length * 6.4) + 20
}

function fanLayout(count, max = FAN_MAX, natural = null) {
  if (!count) return { step: 0, width: 0, chip: FAN_CHIP_W, left: [], lanes: [], widths: [] }
  const chip = Math.min(FAN_CHIP_W, Math.max(FAN_CHIP_MIN, Math.round(max * 0.47)), max)
  if (natural && natural.length === count) {
    // Fitted: a chip is as wide as its name needs, never wider than `chip`.
    // The first chip (on top) shows whole. The others show whole while the fan
    // fits in `max`; past that, the widest visible ends are cut down to `cap`.
    const want = natural.map((width) => Math.max(24, Math.min(chip, Number(width) || chip)))
    const total = (cap) => want.reduce((sum, width, index) => sum + (index === 0 ? width : Math.min(width, cap)), 0)
    let cap = chip
    if (total(cap) > max) {
      let low = 0
      let high = chip
      for (let pass = 0; pass < 24; pass += 1) {
        const mid = (low + high) / 2
        if (total(mid) <= max) low = mid
        else high = mid
      }
      cap = low
    }
    const fitLanes = []
    const fitLeft = []
    const widths = []
    let end = 0
    for (let index = 0; index < count; index += 1) {
      const lane = index === 0 ? want[0] : Math.min(want[index], cap)
      fitLanes.push({ left: end, width: lane })
      end += lane
      // A chip never sticks out on the left: what is hidden lies under the chips above it.
      fitLeft.push(Math.max(0, end - want[index]))
      widths.push(end - fitLeft[index])
    }
    return { step: cap, width: end, chip, left: fitLeft, lanes: fitLanes, widths }
  }
  const step = count > 1 ? Math.min(FAN_STEP_MAX, (max - chip) / (count - 1)) : 0
  const width = chip + (count - 1) * step
  const left = Array.from({ length: count }, (_, index) => index * step)
  // Each chip's visible slice is its end, from the previous chip's end to its
  // own; the first (on top) shows whole.
  const lanes = left.map((x, index) => (index === 0
    ? { left: 0, width: chip }
    : { left: x - step + chip, width: step }))
  return { step, width, chip, left, lanes, widths: left.map(() => chip) }
}

function estimateChipWidth(name) {
  const label = Math.min(String(name || '').length * 6.6, 139)
  return 16 + label + 6 + 28 + 6 + 6 + 2
}

function detailMeta(skill) {
  if (skill.state === 'released') return 'Released at a compaction · not in context until loaded again'
  if (skill.state === 'reloaded') return 'Loaded again after a compaction'
  return skill.turn ? `In context · loaded ${ordinal(skill.turn)}` : 'In context'
}

function ordinal(n) {
  const rest = n % 100
  const suffix = rest >= 11 && rest <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th')
  return `${n}${suffix}`
}

function withoutLoaded(candidates, loaded) {
  const taken = new Set((loaded || []).map((skill) => String(skill.name || '').toLowerCase()))
  return (candidates || []).filter((candidate) => !taken.has(String(candidate.name || '').toLowerCase()))
}


function categoryLabel(skill) {
  const tag = Array.isArray(skill.tags) && skill.tags[0] ? String(skill.tags[0]) : ''
  if (tag) return tag.replace(/[-_]/g, ' ').toUpperCase()
  const origin = String(skill.origin_path || '')
  const segment = origin.split('/').filter(Boolean)[0]
  if (segment && segment.toLowerCase() !== 'skill.md') return segment.replace(/[-_]/g, ' ').toUpperCase()
  return 'SKILL'
}
