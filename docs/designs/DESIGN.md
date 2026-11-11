# Triage (formerly Hermes Skill Judge) — Locked Design (2026-11-05)

Hackathon: Nous Research Hermes Plugin Hackathon · due Nov 11, 2026 5PM EST
Working name: **triage** (aka probability skill router / skill chips)

This is the design as locked on 2026-11-05 (plugin version 0.1.2, Hermes plugin contract v0.22.x). Behaviour has moved on since. The current spec is `how-triage-works.md`; where the two differ, that file is right.

## Problem

Hermes injects a full skills index (names + descriptions) into the system prompt after Discovery. That bloats context and forces the model to pick skills from a static list. Closest marketplace prior art (`skill-router`, Switchyard, skill-retriever) does model-side routing; none replace the index with a **user-facing, probability-tagged, tappable** shortlist, and none **own** the skill catalog outside Hermes’s prompt path.

## Locked cut (user-annotated graph)

**Cut the edge:** `Discovery + catalog resolve` → `System prompt skills index`.

- Discovery may still feed slash commands / plugin registry if useful. This plugin does not remove Hermes slash commands.
- The **system prompt skills index is empty / gone**. Skills live in the **plugin’s own store**.
- Hermes only sees skill bodies the plugin injects after a judge shortlist + user tap (or auto-accept above a threshold).

The annotated graph is the red X on Discovery → System prompt skills index. The rest of the pipeline (slash table, `skill_view`, telemetry) stays in Hermes. This plugin does not register its catalog through `ctx.register_skill`, because v0.22.7 would put those rows back into the index under the `plugin` category. **Changed 2026-11-08:** the catalog is now registered as `/triage:<name>` so Hermes' `/` menu searches all of it (owner's request); the rows that come back into the index are removed by the `llm_request` strip on every request (measured: 235,609 → 230 characters).

## Architecture

```
Skill sources (migrated into plugin DB)
        ↓
Plugin catalog / optional embedder / FTS
        ↓
pre_llm_call: judge → top-K scored candidates
        ↓
a probability shortlist in the user message
        ↓
Inject the chosen SKILL.md body into user-message context
        ↓
llm_request middleware strips leftover <available_skills>
        ↓
pre_tool_call blocks skill_view of names not approved this turn
```

### Cut mechanics (no first-class strip API in Hermes v0.22.7)

1. **Primary:** `run.py migrate` / `hermes triage migrate` copies a `SKILL.md` tree (default `~/.hermes/skills`) into the plugin database. `--apply-config` sets `skills.auto_load: []` and `skills.external_dirs: []` in `config.yaml` (backup written beside it). `--empty-roots --yes` renames the `skills` directory to `skills.bak-skill-judge-<timestamp>`, leaves an empty `skills/` directory, and writes `$HERMES_HOME/.no-bundled-skills`. Default migrate only copies.
2. **Secondary:** `llm_request` middleware replaces `## Skills` … `<available_skills>` … trailing index guidance with a constant stub, and removes the mid-session skills-index delta note (`[System: The skills available in this conversation changed…]`). A request that contains neither is returned unchanged so the prefix cache is not touched.
3. **Do not** rely on `register_system_prompt_section` — it only adds text. This plugin does not register one.

v0.22.7 still seeds essential bundled skills when the opt-out marker is set. The middleware is what keeps those names out of the outbound system message. `skills.auto_load` bodies live in the stable prompt tier and are not regex-stripped; the migrate checklist clears that list instead.

**Cache caveat.** The system prompt is frozen for prefix-cache warmth. The first rewritten request misses that cache. The stub is constant, so subsequent turns can share the stripped prefix. Documented in the README; disable with `strip_skills_index: false` if you would rather depend on an empty skills root alone.

### Hooks / surfaces

| Piece | Mechanism (shipped) |
| --- | --- |
| Judge + shortlist | `pre_llm_call` → `{context: ...}` with probability chips, or the loaded body |
| User pick | On desktop, the chip row. Elsewhere, a reply with the chip number, the skill name, `use <name>`, or `skip`. Hook `ask_choice` is attempted and, on v0.22, returns None; the shortlist is the fallback. |
| Auto-accept | `auto_accept_threshold` (default 0.95) against the no-skill bucket |
| Guard loads | `pre_tool_call` on `skill_view` — block unless approved for this session |
| Serve loads | `tool_execution` returns skill_view JSON from the plugin mirror; `transform_tool_result` rewrites a core not-found if the builtin still ran |
| Refinement writes | `skill_manage` is handled in `tool_execution` and stored under the mirror, not `~/.hermes/skills` |
| Large bodies | Under 8000 characters, inline in `pre_llm_call`. Larger skills are a short pointer plus `skill_view`, so Hermes' 10000-char hook spill never truncates them |
| Strip index | empty roots + `llm_request` middleware. Skills are not passed to `register_skill` |
| Setup | `migrate` copies supporting trees into `plugin-data/.../mirrors/`; `list`, `peek`, `status`, `cut-snippet` |

Probabilities are a softmax over the shortlist pool plus an explicit no-skill score (`NONE_PRIOR`). They are printed on every chip and in the no-skill line. They are not renormalized to hide the no-skill mass.

### Data model (plugin-owned)

SQLite at `$HERMES_HOME/plugin-data/triage/catalog.db` (Hermes `plugin_data_dir` when that module imports):

- `skills`: id, name, description, body, tags (JSON), source (absolute mirror directory under plugin-data, not the Hermes skills root), origin_path, content_hash (whole skill directory), embedding (nullable blob, unused by the v1 ranker), use_count, timestamps
- `mirrors/<name>/`: the copied skill tree (`SKILL.md`, `references/`, `templates/`, `scripts/`, `assets/`). This is what approved `skill_view` and `skill_manage` read and write after `--empty-roots`
- `skills_fts`: FTS5 index (porter) used as a prefilter above 200 skills; smaller catalogs are scanned in full
- `selections`: turn_id, session_id, query, candidates JSON `[{name, p, why}]`, chosen, accepted, created_at

Import walks a tree for `SKILL.md`, reads frontmatter `name` / `description` / `tags` (a small YAML subset, no PyYAML), and stores the Markdown body. Same name at two paths: first path in sorted walk order wins, the other is reported as shadowed. Re-import is hash-idempotent.

### Judge extension point

`SkillJudge` indexes skill content. FTS5 BM25 runs over the full `SKILL.md` body and the reference text stored at migrate. A verbatim sentence of five or more content words from that text outranks a name-only hit, which is what makes a deep sentence feel like a ~95% suggestion. Other routers keep skill names in memory; this one searches the body.

The query is the newest user message. Earlier turns are only a small boost, and that boost is ignored when the new message clearly changes topic. A long assistant reply cannot keep the previous skill on top. `pre_llm_call` on Hermes v0.22 does not include thoughts; `thoughts` / `reasoning` / `trace` are used only when a host passes them.

`embedder` remains optional (`score(query, skill) -> float` in `[0, 1]`). Empty means lexical-only, which is enough for exact and near-exact sentences. When an embedder is set, its ranking is fused with the lexical ranking by reciprocal rank fusion (k=60). The scores are not added. A MiniLM class with that method is the intended local dense path; see the README. `examples/constant_embedder.py` is the reference stub. Auto-accept still uses the lexical probability, so an embedding-only paraphrase is shown and not auto-loaded at the default 0.95.

### UX

- Ambiguous turn: chips such as `45% git-commit — name commit; description commit, message`, plus the no-skill probability. Nothing is loaded.
- Pick one (a reply that is only the name or the number) → the next model context contains that skill’s description and body and no other skill body.
- `skip` / `none` → a one-line note, no body, `skill_view` stays closed.
- High confidence (`please run git-commit on the staged diff` against the demo catalog) auto-loads at the default threshold.

### UI (Desktop, v7)

Shipped in `desktop/plugin.js` for the native Desktop app (`@hermes/plugin-sdk` only). v7 replaces the v6 chip row. Enable it under Capabilities → Plugins, then Rescan so the app copies `desktop/plugin.js` into `desktop-plugins/triage/`.

Both rows sit above the prompt in a fixed overlay. The left row is the ranked shortlist: `NN% name`, **No skill / skip**, colored spans for draft words that match a chip, and a `ctx` badge when that skill is already loaded. Clicking a chip approves it with `POST /approve`, sends the draft, and removes it from the left row. The right row is the overlapping peek fan of skills loaded in this session. Hover or click opens that skill’s detail card. There is no separate in-context list. Auto-accept stays in the composer chrome (`COMPOSER_AREAS.top`). The overlay measures the prompt card and any strip painted immediately above it, such as the usage-stats banner, and sits a few pixels above that stack. Peeks are **16px** at rest and **44px** on hover. Newest loaded skill is leftmost.

### Bridges and compaction

Approving a skill writes a session bridge (the allowlist). The body may be injected once. Hermes `post_compaction` is an observer and ignores return values, so this plugin keeps the bridge, clears the pending shortlist, and on the **next** `pre_llm_call` injects a short “skills unlocked in this chat” list: names and one-line descriptions only. It does not restore `<available_skills>` and does not paste `SKILL.md` bodies. Those names can `skill_view` without being approved again. Suggestion chips are ranked again from the new message.

## Non-goals (v1)

- Patching Hermes core
- Painting color marks inside the composer document (no public hook, and the plugin does not echo the draft as a second line)
- Replacing slash commands for skills that still live on disk
- A hosted embedding model inside the plugin

## Success criteria (hackathon demo)

These are achievable with this repo and the demo in `README.md`:

1. With the plugin enabled, after migrate + empty roots (and the middleware, if anything was re-seeded), the outbound system prompt has **no** skill list. The stub does not name skills. Covered by `tests/test_strip.py` against the v0.22.7 `## Skills` / `<available_skills>` shape.
2. A user message triggers the judge and the shortlist shows probabilities (`45% git-commit — …`). Covered by `tests/test_judge.py` and `run.py peek`.
3. Choosing a skill injects only that skill’s body. Auto-accept and a numeric reply both check that the other fixtures’ markers are absent.
4. Setup migrates `~/.hermes/skills` (or any `SKILL.md` tree) into the plugin store and can clear `auto_load`, `external_dirs`, and the profile skills root. Covered by `tests/test_import.py`.
5. `plugin.yaml` plus `register(ctx)` is a directory Hermes discovers (`hermes plugins enable triage`). `provides_hooks` lists `pre_llm_call` and `pre_tool_call`. The middleware is registered in code because it is not a hook name.

## Reference

- Pipeline map (vendored): `docs/skill-pipeline-map.md` — Hermes v0.22.7 `b200f5bc`
- Hooks and middleware used as documented for that contract: `pre_llm_call` context injection, `pre_tool_call` `{action: block}`, `llm_request` `{request: ...}`, `ctx.register_cli_command`
