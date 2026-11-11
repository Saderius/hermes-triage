# Conversation tests for an outside tester

For a tester (a person or a bot) who has this repository, a Hermes install and a model to talk to. A free model on OpenRouter is enough.

The unit tests and `python run.py eval` check Triage without a model. They cannot check two things, and this document does:

- what the chips do in the middle of a real conversation, where earlier turns are context;
- what the model does with the skills (finds them, loads them, follows them, is stopped by the guard).

## Rules for the tester

- Use a test Hermes profile: set `HERMES_HOME` to an empty folder before you start. Do not run these tests in a profile that holds real chats.
- Your model key stays in your own Hermes configuration. Never paste a key into a prompt, a file in this repository or the report.
- Change no file in this repository. If a step fails, report it; do not fix it.
- The report holds no personal data: no user names, no home-folder paths, no keys, no text from chats other than the test prompts below.

## Setup checklist

- [ ] Hermes is version 0.23.0 or newer (`requires_hermes` in `plugin.yaml`).
- [ ] The repository root is copied to `$HERMES_HOME/plugins/triage` (see "Install" in `README.md`).
- [ ] `hermes plugins enable triage` ran and the gateway was restarted.
- [ ] Demo catalog: `python run.py migrate --from examples/demo-skills` ran. `python run.py status` reports 3 skills.
- [ ] Large catalog (scenarios E and F only): a folder with at least 200 `SKILL.md` skills is set under Settings → Plugins → triage → Skill folders, and `python run.py status` reports 200 or more. Below 200 skills the rare-word rule is off by design.
- [ ] A model answers: `hermes chat --oneshot --provider openrouter -m <model id> -q "Reply with only: ok"` prints `ok`.
- [ ] Write down: Hermes version, operating system, model id, number of skills.

## How to observe

| What | How |
| --- | --- |
| Model tool calls | `hermes chat --oneshot -q "<prompt>" --format stream-json` prints one JSON line per event. Look for `skill_search`, `skill_load`, `skill_view`. |
| A second turn in the same chat | `hermes chat --oneshot --resume <session id> -q "<prompt>"` |
| Chips for a draft alone | `python run.py peek "<draft>"` |
| Chips with the conversation | `GET /api/plugins/triage/rank?q=<draft>&session_id=<session id>` on your Hermes dashboard address. Each candidate has `name`, `probability`, `source` (`draft` or `conversation`) and `anchored` (may attach itself). In Hermes Desktop the same thing is the chip row above the prompt. |
| Loaded skills of a chat | `GET /api/plugins/triage/loaded?session_id=<session id>` |

One side effect to plan for: a `/rank` call with a `session_id` marks that chat as a desktop chat for 10 minutes. During that time the server puts no shortlist into the model's turn for that chat. Call `/rank` after the turns you want to observe, or use a new chat per scenario.

"Attaches itself" below means: a candidate with `anchored: true` and `probability` at or above `auto_accept_threshold` (default 0.90). In Hermes Desktop it shows as a dashed pill on the left pile that you did not click.

## Scenarios

Each scenario is one new chat unless it says otherwise. Record pass or fail for every box.

Where to run them: scenario H is for the command line. Run every other scenario in a Hermes Desktop chat (type the prompt in the composer, with the chips showing). On the command line Triage behaves as scenario H describes: it puts its own choice list into the turn and the model asks you to pick, and a prompt that names a skill loads it at once, so B and C cannot be judged there. `/compress` (scenarios I and J) runs only in Hermes Desktop.

### A. The skills index is cut

1. Send: `List every skill you can see in your system prompt. If you see none, say "none".`

- [ ] The model lists no catalog skills (it may mention the tools `skill_search` and `skill_load`).

### B. The model finds and loads a skill

1. Send: `Use your skill tools to find a skill for writing a commit message, load it, and quote its line that contains the word "Marker".`

- [ ] The model calls `skill_search`, then `skill_load` with `git-commit`.
- [ ] The answer quotes `COMMIT_BODY_ONLY`.
- [ ] `/loaded` for this chat lists `git-commit`.
- [ ] The answer does not quote `REVIEW_BODY_ONLY` or `WEATHER_BODY_ONLY`.

### C. The guard

1. New chat. Send: `Call the tool skill_view with name "weather-brief" and show me exactly what it returns.`

- [ ] The call is blocked: the model reports a refusal, not the skill text. `WEATHER_BODY_ONLY` is not in the answer.

2. Same chat. Send: `Now load weather-brief with skill_load, then call skill_view for it again.`

- [ ] `skill_load` succeeds and the answer can quote `WEATHER_BODY_ONLY`.
- [ ] `skill_view` of `pr-review` is still blocked in this chat.

### D. Chips follow the conversation

This is the scenario the offline tests cannot cover. No prompt names a skill.

1. Send: `I am about to finish some work in git. What makes a good message for the change I staged?`
2. Send: `Give me an example for a bug fix in a login form.`
3. Call `/rank` with an empty `q` and this chat's `session_id`.

- [ ] `git-commit` is among the candidates and its `source` is `conversation`.
- [ ] No candidate attaches itself (an empty draft never attaches anything).

4. Call `/rank` with `q=thanks, that looks good`.

- [ ] `git-commit` is still offered as a `conversation` chip.
- [ ] Nothing attaches itself.

5. Topic change. Send: `Different question: will it rain in Lisbon tomorrow, and how windy will it be?` Then call `/rank` with an empty `q`.

- [ ] `weather-brief` is offered. Record whether `git-commit` is still shown and at what probability.

6. Call `/rank` with `q=write the forecast as three short lines`.

- [ ] `weather-brief` is the first candidate. Record every candidate with its `source` and `probability`.

Known on 2026-11-11, from a simulation with hand-written assistant replies: this box fails. `git-commit`, the earlier topic, comes first at about 53% and `weather-brief` second at about 15%. Report whether a real conversation shows the same.

### E. Ordinary sentences attach nothing (large catalog)

For each draft below call `/rank` with a new chat's `session_id` (or type it in Hermes Desktop without sending).

| Draft |
| --- |
| `let's go with the second option` |
| `the build is still red after my change` |
| `what is the base branch here?` |
| `I have issues with this layout on small screens` |
| `open C:\projects\github\demo\notes.txt and read it` |
| ``why is `class="grid aria-hidden calc-width"` not applied?`` |
| `see https://example.com/docs/firebase/setup for the steps` |
| `ok, commit to that plan and tell me the risks` |

- [ ] No draft makes a skill attach itself.
- [ ] For every draft, record the first three candidates with `probability` and `anchored`. A skill named after an ordinary word (`go`, `build`, `base`, `issues`) may be shown as a chip; it must not attach itself.

### F. Clear requests still attach (large catalog)

Pick from your catalog one skill whose name has two or more words (`<two-word-name>`) and one whose one-word name is rare (a product or tool name, `<rare-name>`).

1. Draft: `use <two-word-name> for this task`

- [ ] That skill attaches itself.

2. Draft: `do you have access to <rare-name> here?`

- [ ] That skill attaches itself. If it does not, record in how many skills the word occurs (`python run.py list` and a text search).

3. Clear the draft.

- [ ] In Hermes Desktop the dashed pill leaves the pile.

### G. A held skill travels with the message (Hermes Desktop only)

1. Type `help me with my change`, click the `git-commit` chip, and send.

- [ ] The pill flies to the fan on the right after sending.
- [ ] Ask `Quote the Marker line of the skill you were given.` The answer is `COMMIT_BODY_ONLY` and the model did not call `skill_view` or `skill_load` to get it.

### H. A non-desktop turn (command line)

1. New chat. Send: `please run git-commit on the staged diff`

- [ ] The turn loads the skill without a question: the model can quote `COMMIT_BODY_ONLY`.

2. New chat. Send: `review the commit message`

- [ ] The model shows a short list with percentages and loads nothing. `COMMIT_BODY_ONLY` is not in the answer.

### I. After a compression (large catalog preferred)

A compression leaves few real messages in context, plus Hermes' own notes (the compaction summary, "[System: ...]", "[Context from the interrupted assistant response]"). The chips must follow the conversation, not those notes. With the demo catalog only the `/loaded` boxes say much; with a large catalog the `/rank` boxes matter most.

1. New chat. Run scenario B's prompt (loads `git-commit`).
2. Same chat, five turns about one topic, for example: `Plan a three-day trip to Lisbon in March.`, `What should I pack for the weather?`, `Which day is best for the coast?`, `Give me a rainy-day plan.`, `Summarise the plan in five lines.`
3. Same chat. If your model list allows it, switch the model once (`/model <other model id>`) and send `continue`. This leaves a "[System: the active model ... changed]" note in the chat.
4. Same chat. Send `/compress`. If the command line cannot run it, use Hermes Desktop, or mark the scenario "could not run" and say why. Write down the chat's session id after the compression (it may change).
5. Call `/loaded` for the chat and `/rank` with an empty `q`.

- [ ] `/loaded` lists `git-commit`. Record its `state` (`in`, `released` or `reloaded`). It is `released` only if the compression dropped the message that loaded it.
- [ ] No `/rank` candidate is chosen for the words "context", "compaction", "summary", "handoff", "interrupted" or "restore" alone. Record the first five candidates with `probability` and the words in their `why`.
- [ ] The first candidates fit the trip conversation, or the list is empty. Record them either way.

6. Same chat. Call `/rank` with `q=add a museum to day two`.

- [ ] The candidates fit the draft or the trip. Nothing about context management comes first.

7. Hermes Desktop only. Open the chat.

- [ ] A `released` skill is not drawn on the right side, and its chip can be clicked again.
- [ ] No chip on the right is see-through.

### J. Keep a skill through a compression

1. New chat. Run scenario B's prompt (loads `git-commit`).
2. `POST /api/plugins/triage/sticky` with `{"session_id": "<id>", "name": "git-commit", "on": true}`. In Hermes Desktop the same thing is the **Keep** button on the skill's card (hover its chip on the right).
3. Send three more turns on any other topic, then `/compress`.
4. Call `/loaded`.

- [ ] `git-commit` has `sticky: true`.
- [ ] Hermes Desktop only: after the compression `git-commit` is back on the left pile, and the next message carries it (ask for the Marker line: `COMMIT_BODY_ONLY`, with no `skill_view` or `skill_load` call).

5. `POST /sticky` with `"on": false`.

- [ ] `/loaded` no longer shows `sticky: true` for it.

### K. Favourites and most used

1. `GET /api/plugins/triage/favorites`. Record `favorites` and `most_used`.
2. `POST /api/plugins/triage/favorites` with `{"name": "weather-brief", "on": true}`, then `GET /favorites` again.

- [ ] `weather-brief` is in `favorites`.
- [ ] After scenario B or G ran, `git-commit` is in `most_used`.

3. `POST /favorites` with `"on": false`.

- [ ] `weather-brief` is no longer in `favorites`.

4. Hermes Desktop only. With an empty prompt, click the magnifier left of the chips.

- [ ] The list shows Favourites, then at most 5 Most used, then Suggested; the search strip at its top looks like Hermes' Ctrl+K palette.
- [ ] Clicking a row's star adds or removes the favourite; it does not attach the skill.
- [ ] Hermes' "N messages" button (scroll up in a long chat) shows above the chips, and the chips do not move while you scroll.

### L. A chat that changes its session id

Hermes gives a chat a new session id when it compacts it or continues an ended one.

1. In the chat from scenario I or J, find both ids: the one before the compression and the new one (if it changed).
2. Call `/loaded` with each id.

- [ ] Both list the same skills. Record the ids' order (old, new) and the `state` of each skill.
- [ ] If the id did not change, record that and skip this scenario.

## Report

One block per scenario:

```text
Scenario: D
Model: <model id>   Hermes: <version>   OS: <name>   Skills: <count>
Result: pass | fail | could not run
Boxes failed: <which, with one line each on what happened instead>
Evidence: <the /rank JSON or the stream-json lines, trimmed to what matters>
```

End the report with anything that surprised you, even where every box passed.
