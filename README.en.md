# QQ Group Chat Bot · AstrBot Plugins

[![ci](https://github.com/lifefuck/astrbot-plugin-fish/actions/workflows/ci.yml/badge.svg)](https://github.com/lifefuck/astrbot-plugin-fish/actions/workflows/ci.yml)

[中文](README.md) | English

Two plugins for [AstrBot](https://github.com/AstrBotDevs/AstrBot) that make a QQ bot behave like an ordinary group member: it remembers people, picks its moments to jump in, typo-then-corrects, reads pictures posted in the group, and sends memes.

Protocol side is NapCat (or any OneBot 11 implementation); the model side is any OpenAI-compatible endpoint.

```
astrbot_plugin_fish/        the chat bot: persona, memory, timing, splitting, memes, vision, hearing, drawing
astrbot_plugin_memory_cmd/  /memary memory lookup, separate plugin, read-only
```

The original hand-rolled framework (`bot.py`, its own event loop and dashboard) is retired and archived at [qq-deepseek-realistic-bot](https://github.com/lifefuck/qq-deepseek-realistic-bot). This repo only holds the plugins.

## What it does

- **Persona**: one fixed character sheet, the current time, and samples of how this group actually talks, assembled into the system prompt.
- **Long-term memory**: every group message is run through an extractor (facts, preferences, plans, events) and stored per group and per person; before replying it pulls only the two most relevant items instead of flooding the context.
- **Impression atoms**: a second, shorter layer distilled from memory ("who drinks what", "what who is up to"), with decay and dedup so the same thing isn't mentioned twice.
- **Timing**: score whether a message looks like a question, whether there's a gap, whether it's a repeat; only pass the bar do anything at all. A second social pass can veto before a single model call is made.
- **Follow-ups without re-@**: @ it once and for the next 5 minutes (at most 2 more turns) it keeps treating that thread as its own business.
- **Anti-template**: colliding openings, colliding endings, repeated filler words, and "so you're saying…" restatements are stripped at the output stage.
- **Typos**: occasionally mistypes a character, and with small probability follows up with a correction.
- **Splitting**: anything over 26 characters becomes at most 3 messages (2 when its mood is bad), sent with random gaps; links, long ASCII words and ZWJ emoji are never cut, and when the budget runs out it re-balances instead of stacking one 300-character wall.
- **Memes**: picked by emotion tag, at most one per turn; blocked if it just sent one or if the other person just posted an image.
- **Meme duels**: @ it with an image and it picks one from its own stock to fire back — a line of trash talk first, then the picture.
- **Vision**: an image in the group gets downscaled, described by Gemini, and the description goes into the prompt; failures degrade silently.
- **Recognise first, decide later**: an image is described in the background the moment it arrives, not only when the line happens to be worth answering. The description is written back into the timeline, so the next few turns actually have the picture instead of an `[image]` placeholder. Each image is described once — the reply decision and the timeline share one call.
- **Bilibili video passthrough**: paste a link and say "send it to me" and it downloads and sends the file itself. AV1 by default (about a sixth the size of H.264); if someone reports a black screen it re-sends as H.264. Works when NapCat lives on another machine — the plugin serves the file over a temporary local HTTP endpoint, and the relay address is auto-detected when left empty.
- **Link parsing**: Bilibili, GitHub, NetEase Cloud Music, Douyin, Xiaohongshu, Kuaishou, and plain web pages get their title and summary pulled in, so it doesn't have to guess what a link is.
- **Web access**: only when it's addressed *and* the question is concrete ("where's this image from", "look this up", "any new anime"). Ordinary chat makes zero outbound calls.
- **Hot memes and group slang**: three layers of injection — always-on memes, memes relevant to this line, and words this specific group uses daily (mined from real chat, see "Where the memes come from").
- **Relationship graph**: remembers who likes @-ing whom and surfaces it as a one-liner before replying, not as a statistics table.
- **Cross-group**: keeps a short summary of what other groups talked about, and can mention it when relevant.
- **Text games**: number bomb, dice, draws, name-picking — all local state machines, no tokens spent.
- **Hearing**: a voice message goes silk → pcm → mp3 → model transcription, then gets answered like any text message. If transcription fails it treats the clip as an unreadable attachment instead of inventing content.
- **Drawing**: when a line deserves a placard, it picks one of the built-in text meme templates and prints the words itself — no command, same as song requests.
- **Song requests**: say "I want to hear Sunny Day" in the group and it searches Bilibili, downloads, sends the original file as a voice message, and posts a progress line while it works. Downloaded tracks are cached locally for seven days, and a failed search retries with cleaned-up spellings. See "Song request dependency".
- **Meme explanations**: when someone asks "what's the deal with X", it looks X up in the meme wiki and injects the answer; if it's not in there it looks it up once and remembers it.
- **Moderation reports**: in whitelisted groups, ads/recruiting, image spam, and serious abuse get a line in the group plus a DM to the owner (the judgement lives in the prompt; code only strips markers and forwards).
- **Pokes**: pokes back, then picks up the message that was left unanswered before the poke.
- **Guardrails**: explicit attempts to rewrite the persona, extract the system prompt, or override instructions get a flat refusal without a model call; normal questions always pass.

## The output gate

Every line the model produces passes through code (`fish_core/safety_guard.py`) before it reaches the group. This layer does not depend on the model behaving, it always runs:

- strips tags the model invents (`[mood:sad]`, `(sighs)`), allowing only `[MEME:…]`, `[draw:…]`, `[image:…]` and CQ codes, and leaves numeric ratios like `[16:9]` alone;
- swaps internal vocabulary for plain speech: BV id → clip, plugin → feature, favour score → impression, prompt → wording; real BV ids are untouched;
- drops any line that fakes having seen something ("I can't see the image on my end", "where's the picture") — pretending beats admitting far worse;
- drops any line that admits being an AI, bot or program, or that mentions the persona, the settings, the prompt;
- drops any line containing a private address, a host path, a key, or a deployment filename — no substitution, since a redacted address still leaves half the sentence.

None of this lives in the persona. Anything that must hold 100% of the time belongs in code; the prompt only sets the tone. Past a few hundred characters the rules start fighting each other, the model picks whichever is easiest to satisfy, and out comes support-desk voice and repetition. The persona is capped at 600 characters and the assembled system prompt stays under 900; before adding anything, ask: can this be code? does it apply every time? can it be an example instead?

## Install

Requires a running AstrBot 4.x with NapCat connected to AstrBot's reverse WebSocket.

```bash
git clone https://github.com/lifefuck/astrbot-plugin-fish.git
cd astrbot-plugin-fish

cp -r astrbot_plugin/astrbot_plugin_fish        /path/to/astrbot/data/plugins/
cp -r astrbot_plugin/astrbot_plugin_memory_cmd  /path/to/astrbot/data/plugins/

systemctl --user restart astrbot
journalctl --user -u astrbot -f --no-pager | grep fish
```

If you see this line, it's up:

```
[fish] 插件已加载：记忆库 …，心境 …，表情包 N 张
```

## Configure

AstrBot dashboard → Plugins → 小鲸鱼·群聊拟真. The ones you must touch:

| Key | Meaning |
| :--- | :--- |
| `enabled_groups` | Groups where it's active. **Empty by default = ignores every group.** Fill it in, or use `["*"]` |
| `bot_nickname` | What it's called when @-ed; keep it in sync with the QQ nickname |
| `interject_desire` | Willingness to chime in. ~0.3 is natural; 0.6 is the loudest person in the group |
| `vision_api_key` | Gemini key for image reading; leave empty to skip vision entirely |
| `vision_proxy` | Proxy for reaching Google |
| `poke_back` | Whether to poke back |
| `enable_games` | Text games (number bomb, dice, draws, name-picking) |
| `meme_battle_enabled` | Fire a meme back when @-ed with an image |
| `media_parse_enabled` | Parse links and video cards in messages |
| `connectors_enabled` | Go online only when addressed with a concrete question (anime lookup, web search, Bilibili trending) |
| `meme_pool_size` / `meme_rotation_sec` | How many memes to inject per round, and how often to rotate |

Provider and the `/` wake prefix are AstrBot-wide settings, not plugin config.

A few that are easy to miss:

| Key | Meaning |
| :--- | :--- |
| `enabled_private_users` | Private-chat whitelist. **Empty = only the owner can DM it** |
| `split_max_chars` / `split_max_parts` | Split threshold (26 chars) and message cap (3) |
| `reply_max_chars` | Per-message length cap, to stop essay-length replies |
| `tech_repeat_window_sec` | Technical-repeat window: the same spec or model number isn't repeated within it |
| `natural_song` | Natural-language song requests on/off; off means commands only |
| `hearing_enabled` | Transcribe voice messages. Off falls back to ignoring audio |
| `hearing_model` / `hearing_timeout_sec` | Which model transcribes, and the per-clip timeout |
| `meme_gen_enabled` / `meme_gen_timeout_sec` | Draw text memes on its own, and the per-image timeout |
| `tattle_watch_groups` | Groups where it reports proactively. **Empty = reports nowhere** |
| `order_guard_enabled` / `order_guard_cooldown_sec` | Moderation watch master switch and per-group report cooldown |
| `video_http_host` | Relay address used to hand Bilibili videos to NapCat; **empty = auto-detect the local LAN address** |

Memes are an optional dependency: drop a pack at `astrbot_plugin_fish/fish_core/meme-pack/` and the index loads automatically; without it the bot sends text only. The repo ships no assets.

## Song request dependency

Song requests don't reimplement search or download; they reuse another plugin:

- install [astrbot_plugin_listen_music](https://github.com/57Darling02/astrbot_plugin_listen_music), which provides Bilibili search, audio download, and audio processing;
- have `ffmpeg` on the system, used only when a file exceeds the size threshold;
- keep both plugins under the same AstrBot `data/plugins/`, with the directory still named `astrbot_plugin_listen_music`.

Missing either one, song requests degrade to "couldn't find that one" and nothing else breaks.

## Voice and drawing dependencies

Both are "works if installed, chats fine without".

**Hearing**: `pip install silk-python` plus `ffmpeg` on the system. QQ voice is silk; it gets decoded to raw pcm, converted to mp3, then transcribed. Without these, clips are skipped as unreadable media.

**Drawing**: `pip install meme_generator==0.2.3`, then pull the template assets once:

```bash
python -c "import meme_generator.resources as r; r.check_resources()"
```

Assets run about 403MB (roughly 25s through a proxy) into `~/.meme_generator`. The engine registers 300 templates; the plugin only uses the 70 text-only ones — templates that need an input photo (head-pat, tea-offering) are skipped, and it just talks instead of faking it.

## Where the memes come from

Two layers. With only one of them, it can't keep up with the group.

**Layer one is the general meme pool** (`data/hot_words.json`), built from trending lists plus hand-written evergreen memes:

```bash
python3 scripts/sync_hot_words.py --dry-run   # see what it would pull
python3 scripts/sync_hot_words.py             # write it
```

Most of a trending list is news headlines ("team X wins", "why this meeting matters") and nobody in a group treats those as memes. The sync script tags them `news` and keeps them **out of the rotation pool** — otherwise real memes get crowded out and what gets injected is long and unanswerable.

**Layer two is this group's own slang** (`data/group_slang.json`), mined from your own chat logs:

```bash
python3 scripts/mine_group_slang.py            # see what it finds
python3 scripts/mine_group_slang.py --apply    # write it
```

The idea is simple: words that aren't in jieba's default dictionary are words this circle invented (game characters, tactics, nicknames). Filter by occurrence count and how many distinct people used them and what's left is basically slang. Mined words are stored per group and only injected into that group — feeding group A's slang to group B just makes the model misuse it.

Both layers reach the prompt as usage guidance ("only if it fits, one at most", "don't force it if you don't get it"), not as definitions to recite.

## Console

Once installed, the AstrBot dashboard gains a **大肥鱼控制台** page (Plugins → 小鲸鱼·群聊拟真 → Pages). It shares the dashboard's login, so no extra port or password.

Five panels: runtime status and switches, per-group hot/cold state (whether it takes part, interjection desire, the three mood axes, last spoken time), member favorability, memory leaderboard, and a recent event stream.

Per-group overrides are only three fields: `enabled` (does it answer here), `interject_desire` (0–1), and `note`. They're written to `data/group_overrides.json` inside the plugin directory, separate from AstrBot's global config — a bad edit for one group can't corrupt the whole config.

The backend is `fish_core/panel.py` with its endpoints under `/astrbot_plugin_fish/panel/*`; the page is `pages/console/index.html`, a single file with no build step.

## Memory lookup

`/memary` in a group or in DM:

- ordinary members see only their own record;
- group owner, admins, and the bot owner see the whole group;
- with many people it uses a merged forward message, falling back to paged text when the protocol side doesn't support it.

It only reads `astrbot_plugin_fish/data/fish_memory.db` and `favor_state.json` — no writes at all, so it keeps working even when the chat plugin is down. The old bot's `user_memories.json` is an optional historical extra; leave `user_mem_path` empty and it isn't read.

## About the data

Group IDs, QQ numbers, and nicknames in the repo are placeholders (`999999`, `张三`, `100012345`), and the sample conversations are invented.

Real runtime data lives in each plugin's `data/` — memory DB, mood snapshots, expression samples, prompt archives — and is covered by `.gitignore`. API keys are read from environment variables or the AstrBot dashboard; no keys ship in the repo.

A privacy scan runs before every commit:

```bash
./scripts/install_hooks.sh                 # install into .git/hooks/pre-commit
python3 scripts/privacy_scan.py --all --audit   # manual full-repo check
```

Secrets, phone numbers, private IPs, and home directory paths are blocked outright; 9–11 digit numbers (QQ-number scale) raise a warning, and once you confirm they're invented you add them to `.privacy-allowlist`.

## CI

Every push runs three things: `compileall` for syntax, `scripts/check_plugin.py` for structural checks (metadata fields, config types and descriptions, whether the entry point looks like a plugin), and `privacy_scan.py` over the whole repo. Both Python 3.10 and 3.12.

## License

MIT. Change the persona and the sample dialogue freely — make it your own character.
