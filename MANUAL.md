# DA Assistant manual

Everything DA Assistant does, and how. For installing it and a first run, see the
[README](README.md).

- [The operations](#the-operations)
- [Choosing models](#choosing-models)
- [Reasoning effort](#reasoning-effort)
- [Privacy Mode](#privacy-mode)
- [Web search](#web-search)
- [Look up word](#look-up-word)
- [Search Encyclopedia and Ask Encyclopedia](#search-encyclopedia-and-ask-encyclopedia)
- [Global shortcuts](#global-shortcuts)
- [Starting automatically](#starting-automatically)
- [What it uses from the system](#what-it-uses-from-the-system)
- [Where things are kept](#where-things-are-kept)
- [Troubleshooting](#troubleshooting)
- [For developers](#for-developers)

## The operations

Eight operations, from the tray menu or a global shortcut you choose (see [Global
shortcuts](#global-shortcuts)) - the last two only once you turn them on (see [Search
Encyclopedia and Ask Encyclopedia](#search-encyclopedia-and-ask-encyclopedia)). No
shortcut is set until you choose one; these are the suggested keys:

| Action | Suggested key | What it does |
|---|---|---|
| Translate clipboard text | `Ctrl+Shift+1` | Translates the clipboard into your target language |
| Translate image selection | `Ctrl+Shift+2` | Transcribes a screen region, then translates it |
| Query clipboard text | `Ctrl+Shift+3` | Ask a question about the clipboard |
| Query image selection | `Ctrl+Shift+4` | Ask a question about a screen region |
| OCR only | `Ctrl+Shift+5` | Transcribes text from a screen region |
| Look up word | `Ctrl+Shift+6` | Shows every entry your own dictionaries have for a word, to ask about |
| Search Encyclopedia | `Ctrl+Shift+7` | Reads your encyclopedia DVD's articles, and answers questions about the ones you tag |
| Ask Encyclopedia | `Ctrl+Shift+8` | Answers a question from the encyclopedia alone, quoted, every quotation checked |

**Ask anything**, at the top of the tray menu, opens a chat window with nothing captured:
type a question, attach files if you like (see [Attachments](#attachments)), and it is
answered by your answering model, privately in Privacy Mode. Its suggested key is
`Ctrl+Shift+9`.

Answers open in a chat window that streams as it generates, renders Markdown properly
(tables, code blocks with syntax highlighting, URLs as links that open in your browser)
and accepts follow-up questions. The header's buttons copy the whole conversation as
Markdown, save it to a `.md` file, or clear it to start over (Ctrl+L) - keeping the
window's model, its settings and any capture, with **Undo** beside Send for a few
seconds after. **Ctrl+F** finds text in the conversation: every match is marked, Enter
or F3 goes to the next and Shift+Enter to the one before, Escape closes it. In Search
Encyclopedia it searches the article when that is where you were reading.
A conversation about a screen region shows the
capture above it, as you took it, to view full size or save as a PNG; saved as Markdown,
the capture is written beside the `.md` under the same name and linked from it. Until you
save it, the capture exists only in memory - in Privacy Mode too.

**Each operation's first step goes to the model chosen for that job, and the rest of
the conversation belongs to your answering model**: the text model, or the premium model
when **Use premium text model** is ticked (their private versions in Privacy Mode). A
cheap OCR model is fine for transcribing, but not for the questions and searches that
might follow, so those never stay with it.

- **OCR only**: the vision model's transcription is the first answer, verbatim and
  nothing else. Ask for changes, explanations or searches after, and those go to the
  answering model.
- **Translate image selection**: the vision model transcribes the region, then the
  answering model translates the result. Those are different jobs — the cheap, accurate
  OCR models are usually poor translators — so they get different models.
- **Query image selection**: answered by the answering model directly, as below.

**The model menu** in the chat window's header names the model your next message goes
to, and switches it at any point: Text, Premium, Vision, or **Other model…** for any
model in the provider's list. Every answer is labelled with the model that wrote it.
**Ask again with…**, beside Send, asks your last question again with another model, for
when an answer is not good enough. The earlier answer stays on screen, dimmed, but only
the new one is carried into the conversation.

The capture stays with every image conversation, whatever model it moves to. A model
that accepts images is given the image with each message. For one that cannot, the
vision model reads the image once — its text, verbatim, and a description of the rest —
and that reading goes with every message to such a model instead.

**Query image selection** is answered by your answering model. How depends on whether
that model can see images, which the app looks up in the provider's model list:

- **If it can**, it is given the image itself. The vision model reads the region first,
  and its text goes beside the question. The answering model has the picture, but a
  literal reading from a model given only that job is what keeps an error code, a
  version or a stack frame exact — reading pixels while also reasoning about them is
  where a digit goes astray, and this app is mostly pointed at error dialogs, consoles
  and text in page images.
- **If it cannot**, or the model list does not include it, the vision model reads the
  image for it: every piece of text, verbatim, and a description of the rest — the kind
  of window, its layout, what is selected or disabled, icons and colours that mean
  something. The text model answers from that reading, which is shown under your
  question so you can see what it had to go on.

Either way the reading arrives whole; only the search query is cut down to a distinctive
line or two. One cheap vision call, on the first message of an image query.

Web search can be switched on or off for each individual request, from the **Search the
web** box on the send dialog. The three operations that cannot benefit from it —
translate clipboard text, translate image selection and OCR — start with it off
regardless of your default, since they work from text already in front of the model. The chat window's header carries a matching magnifier for
follow-ups, beside the model menu. Both start from your saved defaults and never write
back to them.

**Privacy Mode**, from the tray menu or **Settings > Privacy**, sends everything to two
models of your choosing that run in an attested trusted execution environment instead:
nano-gpt's `TEE/` models, or its end-to-end encrypted `private/` ones. Each enclave proves
itself before anything is sent to it, and every conversation shows what was proved. See
[Privacy Mode](#privacy-mode).

### Blacking out part of a capture

The send dialog for a screen region has **Black out…** beside Re-crop. Drag rectangles
over anything that should not be sent - names, addresses, keys, tokens - then **Apply**.
Undo (or Ctrl+Z) takes back the last one, Clear all of them, and **Actual size** / **Fit
to window** switches the zoom. The black is painted into the capture's own pixels, its
edges snapped outwards to whole pixels so no blended border is left, and the blacked-out
capture replaces the original outright: it is what is sent, what the chat window shows
and saves, and what a re-crop starts from. Nothing of the original goes anywhere.

Every capture is also rebuilt from its pixels alone before it is sent or shown, so
whatever the screenshot tool attached to it - an ICC profile, text chunks, EXIF - is
dropped.

### Attachments

Every chat window - Ask anything, a conversation about a capture or the clipboard, Look
up word, the encyclopedia windows - has a paperclip beside the message box. Attach
pictures and text files to your next message with it, or drop files on the message box
or the conversation. Each shows as a chip above the message box until you send, with ×
to take it off again. A message can be pictures alone.

- **Pictures** (PNG, JPEG, WebP, GIF, BMP) open in the same black-out editor first;
  **Attach** sends the picture as it looks there. It is then decoded to pixels and
  re-encoded from those pixels alone: no EXIF (location, camera, timestamps, the
  embedded thumbnail), XMP, ICC profile, text chunks or comments, only the first frame
  of an animation, and nothing appended after the image. A photo is turned upright from
  its EXIF orientation first. Photos that were JPEG are sent as JPEG, everything else
  as PNG, scaled down to 3000 pixels on the long side (`capture_max_long_edge` in
  `config.json`) if larger.
- **Text files** are anything that reads as UTF-8 (or UTF-16 with a byte-order mark),
  up to 512 KB: Markdown, JSON, CSV, YAML, logs, source code. The text goes into your
  message in a fenced block under the file's name; nothing else of the file is sent.
  Files that are neither text nor a supported picture are refused, and the status line
  says why.

Attached pictures go with the conversation the way a capture does: a model that accepts
images is given each picture, and for one that cannot, the vision model reads each
picture once - its text verbatim and a description - and that reading is sent in its
place. The transcript shows a thumbnail of each picture and the name of each file;
Markdown exports list them by name.

## Providers

A model is asked of a provider, and each of the three models names its own. **Settings >
Providers** holds what each one needs; **Settings > General** has a provider beside every
model.

| Provider | Needs | Model list says | Also |
|---|---|---|---|
| NanoGPT | its API key | prices, context, which models see images, reasoning levels | the only one with `TEE/` and `private/` models, so [Privacy Mode](#privacy-mode) is its alone; runs the [web search](#web-search) |
| OpenRouter | its API key | the same, from `openrouter.ai/api/v1/models` | |
| Ollama | the server's address, `http://localhost:11434` unless you say otherwise; no key | which models see images and which think, from the server's `/api/tags` | everything is free, and stays on your network |
| Other OpenAI-compatible endpoint | its address, and a key if it wants one | whatever its `/v1/models` says, which is often only the names | |

So OCR can run on a machine of your own while the answers come from somewhere else: set
the vision model's provider to Ollama and leave the text models where they are. The chat
window's model menu names the provider of any model that is not NanoGPT's, and **Other
model…** can pick from any of them.

An address is typed as the server's: `http://192.168.1.20:11434` for Ollama, whatever
path follows it. For another endpoint, its base (`https://host/v1`) and its full
`/chat/completions` address are both understood. Addresses follow the rule under
[Requests](#requests): https, or this machine and its local network.

Only the providers your models use have to be set up. With all three on Ollama no key is
wanted at all - but then there is no web search, which runs on NanoGPT with its key
whichever provider answers, and no Privacy Mode.

**Test connection** asks the text model a one-word question. If the premium or vision
model is on another provider, that provider is asked for its model list instead, which
finds a wrong address, a refused key and a model it does not have.

**An Ollama model's own temperature** (and `top_p`), from its Modelfile, is sent with
every request to it. Ollama's OpenAI-compatible endpoint, which the app uses, would
otherwise run every model at OpenAI's default of 1: glm-ocr's Modelfile says 0, and
qwen3's 0.6.

**Thinking models on Ollama** think unless told not to, and for most the levels are on
and off in all but name. Set **Reasoning effort** to **None** to switch it off: measured
on `qwen3:1.7b`, a one-line sum took 64 seconds at its default and under one with None.

**glm-ocr on Ollama does not stop when it has finished** (Ollama 0.30 to 0.40.2 at
least): it transcribes the image and carries on to the token limit, from `ollama run` as
much as from here, because Ollama does not count the model's end-of-turn token as an
end. The app tells it where to stop for the endings that have a recognisable start, and
watches the rest: it is cut off where it starts the transcription over, says one line
eight times running, or goes round the same few lines five times. That makes it usable,
not exact: now and then a reply still ends with a stray line or a second copy, and a
capture that really does repeat one line eight times is cut short there. The cure is on the server - a newer Ollama that stops it, or
the model rebuilt with `<|user|>` named as its end-of-turn token
(`gguf-new-metadata --special-token eot "<|user|>"`, then `ollama create`).

A settings file from before providers had names, whose one API URL was not NanoGPT's, is moved over when
the app starts: the three models are given the provider that URL was - Ollama for port
11434, otherwise the other endpoint - with its address and key.

## Choosing models

Settings shows the three model fields, each with its provider and a **Browse…** button.
That opens a searchable list of everything the provider offers, with price per million
tokens, context length and capability flags. Type to narrow it, or tick **Vision only** /
**Free only**. The vision field filters to models that actually accept images, unless the
provider's list does not say which do; then all are listed, and the browser says so.

| Field | Used by | Wants |
|---|---|---|
| Text model | answering: clipboard operations, translation, questions about images, and follow-ups | everyday quality |
| Premium text model | answering instead of the text model when **Use premium text model** is ticked; **Premium** in the model menu | your best text model |
| Vision model (OCR) | OCR's transcription, stage one of Translate image selection, and reading the image for a model that cannot see | a cheap, literal reader |

The vision model runs on every capture, so it is worth keeping cheap. Whether a question
about an image is answered from the picture itself comes down to the answering model:
pick one that accepts images (the **Capabilities** column says `vision`) and it will be
given the image. Any other model is one menu away in the chat window.

The list comes from the provider (NanoGPT's `/v1/models?detailed=true`, OpenRouter's
`/api/v1/models`, an Ollama server's `/api/tags`) and is cached for six hours, one file
per provider; the refresh button re-fetches it.

## Reasoning effort

Some models always think before they answer — GLM 5.3, Kimi K3, Claude's `:thinking`
models — and at their own default some think at great length. Measured: `private/glm-5-3`
spent a 20,480-token limit reasoning about a 5,000-character translation and never
started it. **Settings > Advanced > Reasoning effort** sets how hard they think: **none**,
**low** (the default), **medium** or **high**. At low, that translation took 13 seconds.

**None only works for models that support switching their thinking off** — those whose
listed levels include `none`, such as `deepseek/deepseek-v4-pro`. Models that always think
cannot: nano-gpt refuses it for GLM 5.3 ("always thinks and does not support disabling
reasoning"), so with None they are asked for their lowest level instead, which for GLM
measured 0 to 7 thinking tokens on ordinary requests.

It is sent only to models that always reason, which the provider's model list marks by the
effort levels it offers, and each gets the nearest level it has (GLM offers low, high and
max, so medium becomes high). A model that thinks only when asked, such as
`deepseek/deepseek-v4-pro`, is never sent it: asking switched its thinking on. **Max
response tokens** is the answer's length; a model asked to reason gets room to think on
top of it, and is billed only for what it uses. A reply that still runs out of room says
so — cut off, or all thinking and no answer — and names the setting to change.

## Privacy Mode

Tick **Privacy Mode (TEE models only)** in the tray menu, or in **Settings > Privacy**, and
every operation - translation, OCR, questions about text or a screen region, and every
follow-up - goes only to the models set there, never to the ones above:

| Field | Used by |
|---|---|
| Private text model | answering, as the text model does outside Privacy Mode |
| Private premium model | answering instead, when **Use premium text model** is ticked |
| Private vision model | OCR's transcription, stage one of Translate image selection, and reading the image for a private model that cannot see |

In a private conversation the model menu offers these three, and **Other TEE model…**
lists only `TEE/` and `private/` models; whatever is chosen is attested before anything
is sent to it. A conversation lists only the models it has used or is about to: its
first step's and its answering model's, then any chosen along the way.

**Browse…** beside them lists only models that run in an enclave. There are two kinds:

- **`TEE/` models** run inside a trusted execution environment. Before anything is sent,
  the app attests the enclave with a fresh nonce, and checks every signature itself:
  - the Intel TDX quote must bind the key that signs replies to that nonce;
  - the quote is verified the way Intel's own Quote Verification Library does it: its PCK
    certificate chain up to Intel's SGX Root CA (pinned in the app, not taken from the
    quote), the quoting enclave's report, the quote's signature, Intel's revocation lists,
    and Intel's signed TCB information for the platform, its TDX module and its quoting
    enclave. A debug TD is refused;
  - the GPU evidence, made with the same nonce, goes to NVIDIA's attestation service, and
    its answer is checked in turn: every token's ES384 signature against NVIDIA's
    published keys, the nonce, the validity window, each GPU's token against the digest
    the overall verdict lists, and each GPU's measurements, secure boot and debug state.

  Every reply is then checked to be signed by the attested key. Your text still passes
  nano-gpt's gateway in the clear on its way in - attestation proves where the model
  runs, not that nano-gpt cannot read the request.
- **`private/` models** are nano-gpt's Private Mode: **end-to-end encrypted**. The body is
  sealed on this machine to a key only the attested enclave holds (Tinfoil's router,
  measured by AMD SEV-SNP or Intel TDX, its release checked against Sigstore), and the
  reply comes back sealed. nano-gpt relays ciphertext: it sees your account, the model,
  timing, sizes and cost, never the text. It costs 5% more than the `TEE/` twin.

The defaults, `TEE/glm-5.3-flash` and `TEE/qwen3.6-27b`, attested fully with GPU evidence
and signed their replies when checked (Sept 2026), and need nothing installed. **Check
attestation** in Settings attests both enclaves on the spot, which is free; **Test
connection** sends the private text model one short request, checked end to end.

What you see:

- **Before sending**, the compose dialog carries a green Privacy Mode note naming the models,
  and its button reads **Send privately**. Web search is unticked and cannot be ticked.
- **In the chat window**, the header shows a lock and `Privacy Mode · <model>`, and a banner
  above the transcript reports each enclave: neutral before anything is checked, green
  once it has proved itself (**TEE attested**, or **End-to-end encrypted**), amber if it
  proved less than it could - no GPU evidence, an Intel TCB status other than
  `UpToDate` (named as Intel names it), Intel's or NVIDIA's services out of reach, or an
  enclave running an older release - and red if it was refused. **Details…** lists everything that was checked, and what
  was not. The status line follows each step: attesting, sending, receiving, checking the
  signature.
- **On every turn**, a badge: your messages read *Sent to attested TEE* or *Sealed
  end-to-end*; answers read *TEE · signed by attested enclave* or *End-to-end encrypted*.
  A signature from any other key reads *signature mismatch*, in red.

A conversation that starts private **stays private until its window is closed**. Its
models are fixed when it starts, and nothing in it reads the setting again: switching
Privacy Mode off, or choosing different models, changes only conversations that start
afterwards. Its model menu can reach only TEE models, and it has no web search - a search
query is made from your text and would carry it to a search provider in the clear. A compose dialog opened in
Privacy Mode sends privately even if the mode is switched off before you press Send.

Nothing is ever sent if an enclave cannot be attested. A `TEE/` model is refused with the
reason if it offers no attestation, if its quote does not bind the nonce, fails a
signature, chains to anything but Intel's root, is revoked or comes from a debug TD, if
Intel has revoked its TCB or lists none it meets, or if NVIDIA's verdict is unsigned, for
another nonce, out of date or negative. So is an operation started with a non-TEE model
configured. It never falls back to the ordinary models.

nano-gpt retires Private Mode models from time to time, and the enclave - Tinfoil's
router, whatever the model - would still attest. So before a `private/` model is first
used in a conversation, it is looked up in nano-gpt's public list of them (no key is
sent). If it is gone, nothing is sent: the banner reads **No longer offered**, the model
button says so, and a dialog offers the end-to-end encrypted models the list still has -
**Ask again with it** asks your question again with the one you pick, and the
conversation carries on with it. **Check attestation** in Settings shows such a model as
*no longer offered*; new conversations use the models under **Settings > Privacy**, so
choose another there too. A list that cannot be read says nothing either way.

`TEE/` models come from several providers, which attest differently. Checked across
nano-gpt's list (Sept 2026):

- **Most** send one enclave's quote, binding the key that signs its replies to our nonce.
- **Chutes' models** (`TEE/kimi-k3`, for one) send every instance that could answer. Each
  instance's quote binds its own key, and that key signs a body carrying our nonce, the
  quote and its GPU evidence. Every instance is checked in full, and one that failed to
  attest refuses them all. They sign no replies, so their answers read *signature
  unavailable*.
- **Tinfoil-hosted models** (`TEE/gemma4-31b`, `TEE/deepseek-v4.1-flash`,
  `TEE/llama3-3-70b`) offer no attestation here. nano-gpt says fresh attestation for them
  only comes through Tinfoil's own flow, which is what their `private/` versions use.
- **Models relayed through RedPill** (`TEE/glm-5.1`, `TEE/qwen3.5-27b`,
  `TEE/qwen3.5-397b-a17b`) are refused. When the relay answers at all, its quotes are made
  for a nonce the relay chose rather than ours, so they would read the same if replayed.

An error names the model it came from, and its part in the conversation: the vision
model reading an image, or the text, premium or private model answering.

Not checked by this app: whether a `TEE/` reply's *content* matches its signed record -
the record hashes the bodies as nano-gpt's gateway saw them, which match nothing the app
can reproduce, hence "signed" rather than "verified" - and which software the TEE runs:
its measurements are proved genuine by Intel's chain, but not compared with a known
build. The signature checks need Python's `cryptography` package (`python3-cryptography`;
the AppImage carries it); without it the banner says *signatures unchecked*.

**End-to-end encryption needs Tinfoil's client.** The AppImage carries it. Running from
source, install it once:

```bash
tools/install-private-mode.sh           # restart the assistant afterwards
tools/install-private-mode.sh --remove  # and to take it out again
```

That installs Tinfoil's SDK and what it depends on - exactly the wheels
`packaging/private-mode.lock` pins by sha256, nothing else - into
`~/.local/share/da-assistant/private-mode`, which the app puts first on its path. It does
not touch system packages, and it runs no code from nano-gpt: nano-gpt's own client is a
Node proxy run with `npx …@latest`, which would let the party the encryption keeps out
update the code that holds your plaintext. `--probe` and **Diagnostics** say whether it is
installed. To move to a newer SDK, change `packaging/private-mode.in` and run
`packaging/lock-private-mode.sh`.

The implementation is ported from StoryLoom, which talks to the same models.

## Web search

The search runs on NanoGPT, with its API key, whichever [provider](#providers) answers
the question; without that key nothing is searched and the model answers from what it
has.

Three levels of control, narrowest first:

- **Per request** — the **Search the web** box on the send dialog, next to the character
  count or capture size. This is the one that governs the message you are composing.
- **Per follow-up** — the magnifier in the chat window header, which applies to follow-up
  questions in that conversation.
- **Default** — **Settings > Web search**, which seeds the other two.

The first message is already streaming by the time the chat window appears, so the send
dialog is what decides whether that message searches; the header toggle picks up from the
follow-up onwards.

The app runs the search itself, against `POST /api/web`, and hands the results to the
model — rather than appending `:online` to the model name and letting the chat endpoint
search on its behalf.

That is not a matter of taste. With `:online`, the query is derived server-side from your
latest user message, so anything else in that message is what gets searched for: a
paragraph asking the model to cite its sources once turned a question about Japanese song
lyrics into a search about how search engines work. And the sources never come back —
measured against the live API, a request that demonstrably searched (16,821 prompt tokens
against a 73-token control) carried none of the documented `x_nanogpt_search_*` fields,
streaming or not.

Searching directly fixes both. The text model first turns the question and the material
into a query — the exception name rather than the whole traceback, `0x80070643 KB5034441`
rather than "what does this error mean" — because a stack trace pasted into a search box
finds nothing. Measured, that literal paste returned HTTP 504 and no usable results, while
the crafted `psycopg2.errors.DeadlockDetected deadlock detected` returned four exact
Stack Overflow and GitHub issue threads. It costs one cheap call per searched request and
can be turned off under **Settings > Web search**.

Failing that, the query is built from the question you asked and the material it is about — for an image query, the text the OCR pass read out of the capture —
rather than scraped back out of the prompt, which carries labels the model needs and a
search engine trips over. Measured, `Can you identify this song? --- Text visible in the
image: <lyrics>` returned two on-target results out of five, the rest generic "identify
any song" tools; the same lyrics without the scaffolding returned five out of five. The
results come back as titles, URLs and page text, and the **Sources** list is built from
what was actually fetched. It also keeps working on any model — the Responses endpoint, which does return
proper citations, refuses Anthropic models for hosted tools.

| Setting | Values | Effect |
|---|---|---|
| Let the model write the search query | on/off | crafts the query from the capture |
| Mode | `off`, `standard`, `deep` | search depth, where the provider has one |
| Provider | linkup, tavily, exa, kagi, perplexity, valyu, brave, sofya | which search runs |
| Results | `low` 3, `medium` 5, `high` 8 | how many pages reach the model |
| Exa depth | `fast`, `auto`, `neural`, `deep`, … | exa's depth, in place of the mode |
| Kagi source | `web`, `news`, `search` | see below |
| Valyu search type | `all`, `web` | scope |

**Kagi is worth understanding before you pick it.** `web` and `news` are enrichment tiers
at \$0.002; `search` is full web search at \$0.025. Enrichment will not identify a song
from its lyrics, and does not announce that it cannot — it simply returns nothing useful.
The dropdown says which is which, and the default is `search`.

**Which provider returns anything** is worth measuring rather than assuming. Asking the
same question about the same screenshot, through this app, against the live API:

| Provider | Prompt tokens injected | Cost | Identified the song |
|---|---|---|---|
| `linkup` | ~15,000 | ~\$0.041 | 3/3 |
| `kagi` (search tier) | next to nothing | ~\$0.013 | 2/3 |

**Citations.** Answers end with a **Sources** section of Markdown links, rendered as
clickable links that open in your browser rather than navigating the transcript. Because
the app ran the search, these are the pages it fetched — not a list the model was asked to
remember, and not something that has to be parsed back out of the prose.

The provider does expose search metadata of its own — `x_nanogpt_search_results`,
`x_nanogpt_search_annotations`, and `choices[0].message.citations` on search-native
models — and the client still reads those when they appear. Measured, they appear only
when the search ran through OpenAI-native search on a GPT-5 model, and never on a streamed
response, which is why they cannot be the app's source of citations.

Turn it off with *List the sources searched* under **Settings > Web search** if you
prefer terser answers.

## Look up word

A simplified GoldenDict: it looks a word up in every dictionary in a folder you choose
and shows each one's entries under its name, with cross-references as links that look
their word up in turn. Below them is the ordinary chat box, so you can ask about what
you are reading - "what is the difference between senses 2 and 3?", "which of these is
British usage?" - and the answering model is given the entries, the latest few lookups'
worth, to answer from and quote.

- **Dictionaries**: DSL (`.dsl`, `.dsl.dz`, as GoldenDict and Lingvo use) and StarDict
  (`.ifo` with its `.idx`/`.dict`, compressed or not). Choose the folder under
  **Settings > Reference**, where each dictionary found can be ticked or unticked, and
  dragged by its handle to its place - or nudged with its arrows: results come in that
  order. Dragging near the top or bottom of the list scrolls it. Lingvo's `_abrv`
  abbreviation files are skipped. Sounds, and the pictures in DSL entries, are not
  shown.
- **Entries look as GoldenDict shows them**: each dictionary's own colours, type sizes
  and the small pictures a StarDict dictionary keeps in its `res` folder (the Shorter
  Oxford's boxed sense numbers, its daggers and bullets). A dictionary's HTML is not
  filtered as a model's answer is - these are your own files - only closed off, so one
  entry cannot run into the next. In the dark theme each colour is lifted to a shade of
  itself that reads on the dark background.
- **Each word gets a page of its own**: looking up another - typed, or a
  cross-reference clicked - shows its entries in place of the last word's, and the
  questions asked about the last word go with it. **Back** and **Forward** in the header
  (Alt+Left, Alt+Right) go through the words, each with its questions and answers.
  **Shift+Enter** in the box adds a word below the ones on the page instead, to compare.
- **The bar under the header** has a button per dictionary - its own icon where it ships
  one, its initials where not, its name and description on hover (its `.ann`, or what
  its `.ifo` says of itself and whose it is). Release one to hide that dictionary's
  entries in every lookup in this window, and leave them out of what questions are sent
  with; press it again to bring them back. Only that window is affected.
- **The files are read in place**; nothing is copied. The first lookup indexes the
  headwords into `~/.cache/da-assistant/dictionaries.sqlite` - about 15 seconds and
  120 MB for 2 GB of dictionaries - and a dictionary is indexed again only when its
  files change.
- **Matching** ignores case, accents and spacing, finds DSL headwords with or without
  their optional parts, and falls back to the stem of a common inflection
  ("studies" -> "study"). With no match, headwords starting with what you typed are
  offered instead.
- The word comes from the clipboard when it holds a word or a short phrase, and is
  looked up at once.
- The lookup never leaves this machine and needs no API key. Questions about it are
  answered like any other conversation - privately in Privacy Mode - with web search
  off unless you turn it on.

## Search Encyclopedia and Ask Encyclopedia

For owners of the 2015 encyclopedia DVD the [README](README.md#the-encyclopedia) describes.
Both are off until you turn on **Settings > Reference > Search Encyclopedia and Ask
Encyclopedia**; until then they are not in the tray menu, and their shortcuts, if you set
any, are not bound.

**Search Encyclopedia** is for reading. The article fills the window as
the DVD presented it - every picture with its caption, each section's contributors,
films and sounds playable in place, the page's own "To cite this page" box at the end
(a picture's page carries its credit), links to other articles followed with **Back**
and **Forward** (Alt+Left,
Alt+Right). Search from the box in the header: the best match opens at once - an article
titled exactly what you typed, the longest of several - and the others wait under
**Results**. What is on the clipboard waits in the box, selected: Enter searches for it,
typing replaces it. Flash animations open in a window of their own.

Questions are optional, and only about what you choose. **☆ Ask about this** tags the
article you are reading; tagged articles are listed above the question box, where ×
untags them. Tagging sends nothing: when you ask a question, the tagged articles go with
it - each whole, or for a very long one, the sections that best match the question -
and nothing else you have read does. With nothing tagged, the question is not sent.
**Hide answers** gives the reading the room.

**Ask Encyclopedia** is for an answer that rests on the encyclopedia, not on
whatever else the model learned. Each question goes through four steps:

1. **Plan**: the model turns the question into what an answer would contain - names,
   terms, likely article titles - since the search matches words, not meaning.
2. **Search**: those searches, the question's own words and the titles, across every
   section of all three encyclopedias, their rankings combined; at most three sections
   from one article. A follow-up is searched together with the question before it.
3. **Check**: shown what was found, the model says whether it answers the question. If
   not, it says what to look for, and one more search follows.
4. **Answer**, from the passages found, treated as the authority: each statement cited by
   passage number, key facts quoted word for word.

**Strict**, on by default, keeps the answer to the encyclopedia: where it has nothing,
the answer says so rather than filling in - and if even the second search found little,
the sources say so, and suggest switching Strict off. Off, what the encyclopedia does not
cover may be answered too, but only under a heading of its own, *Not from the
encyclopedia*. **Written for** chooses the reader: a General reader is answered from the
main Encyclopedia first, a Student from the Student Encyclopedia, a Young reader from the Elementary one -
each falling back on the others - and in words for that reader. The defaults for both are
under **Settings > Reference**.

Every quotation in an answer is then checked against the passage it cites. One not found
there word for word is marked ⚠ where it stands, and listed under the answer. Click the
⚠ (or **show it** in the list) and the article opens in Search Encyclopedia with the
passage's own words highlighted - where it was found under another number, or where the
passage comes closest to it, so you can see straight away what the source really says.
A ⚠ that is not a link is a quotation nothing in the passages comes near. Each
citation is a link: it opens the article in Search Encyclopedia at what was quoted, found
and highlighted on the page. Under the answer are its sources - those cited first, the
rest folded away, each with its article's contributors - and the pictures and films of
the sections it cited, each with its credit. A copied question
waits in the message box, selected.

Setting it up, once:

1. Copy the DVD's `data` folder to your computer - the one holding `ARTICLE_DATA.zip`,
   `INSTALL_DATA.zip`, `IMAGE_DATA` and `MEDIA1_DATA` to `MEDIA4_DATA`.
2. Tick **Settings > Reference > Search Encyclopedia and Ask Encyclopedia**, point
   **Encyclopedia DVD folder** at the copy and press **Import**. Or from a terminal:
   `./run-assistant --import-encyclopedia /path/to/data`, then tick the box.
3. Give them shortcuts in **Settings > Shortcuts** if you want them.
4. For the animations, run `./tools/install-ruffle.sh` (the AppImage carries it already).

The import reads all three encyclopedias - the main one (103,092 articles), the Student (18,934) and the Elementary (3,874) - into a searchable database
of the app's own, `~/.local/share/da-assistant/encyclopedia.sqlite`: 45 seconds and
about 960 MB. Pictures and films are not copied; keep the DVD folder where it is.

What it takes:

- **Nothing on the DVD is encrypted.** Its articles are gzip-compressed HTML in tables of
  AxionDB, an open-source Java database whose format is public; pictures are stored
  back to back in one file. The app reads both with its own code and never runs, or
  decompiles, the DVD's own software. `Endec.jar` on the DVD is not used.
- **Films** (FLV) and sounds (MP3) play in WebKit through GStreamer. The FLV films use
  the Sorenson codec, which needs `gstreamer1.0-libav`.
- **Animations** (160 SWF files) are played by [Ruffle](https://ruffle.rs), a Flash
  Player emulator, version-pinned and hash-checked by `packaging/ruffle.lock`. It plays
  the older ActionScript 1 and 2 animations (117 of them) well and the newer
  ActionScript 3 ones (43) only partly.
- Searching and reading never leave this machine. A question sends the tagged
  articles, or Ask Encyclopedia's passages, to the answering model - privately in Privacy
  Mode. Ask Encyclopedia never searches the web.

None of the encyclopedia's content is part of this app, and nothing it writes holds any:
the database is built on your machine from your copy.

## Global shortcuts

Set them in **Settings > Shortcuts**: click an action's button and press the combination,
Escape to cancel, Backspace (or the clear button) for none. **Use suggested keys** fills
in `Ctrl+Shift+1` to `9`; **Clear all** empties the list. A combination needs Ctrl, Alt
or Super, or must be a function key, so that a shortcut never takes a plain key from
every program; `Ctrl+Alt+F1` to `F12` are refused, since they switch to a text console
and never reach any program. Giving a key to a second action takes it from the first.
Nothing is bound until you press Save, and a new install has none.

A Wayland program cannot listen for keys on its own, so how they are bound depends on
the session. The page says which applies; **Settings > Advanced > Shortcuts by** can
force one if the automatic choice is wrong.

| Session | How | What you see |
|---|---|---|
| X11 - Cinnamon, MATE, XFCE, KDE or GNOME on Xorg | The app grabs the keys itself while it runs | A key another program holds is marked "taken by another program" |
| Wayland with a GlobalShortcuts portal - KDE Plasma 6, GNOME 48 and later | The desktop binds them. The keys you choose are suggestions; it may ask you to confirm them, and keeps them until you change them in its own shortcut settings | The key the desktop actually bound, beside each action and in the tray menu. **Change in system settings…** opens its dialog where the portal supports that |
| Wayland on GNOME before 48 or Cinnamon | Written into the desktop's custom keyboard shortcuts as `DA Assistant: …`, each running `run-assistant --action=NAME` | They appear in the desktop's own keyboard settings too |
| Hyprland, Sway | Written into `~/.config/hypr/hyprland.conf` or `~/.config/sway/config`, between `# --- DA Assistant hotkeys` marker lines, and the compositor reloaded. A backup of the file is kept beside it as `.da-assistant.bak`; a config that does not exist is not created | |
| Anything else | Nothing is written | The commands to bind by hand, with a Copy button |

Hyprland has a portal too, but it binds nothing until you write `global` binds into its
config by hand, so the app writes ordinary binds there instead.

On KDE and GNOME's portal the app has to tell the portal who it is, and the portal
accepts that only from an app with a `.desktop` entry. So the app keeps a hidden one,
`~/.local/share/applications/org.digitalarchon.DAAssistant.desktop`, which is also the
name its shortcuts appear under in the desktop's shortcut settings. KDE names a key with
Shift by the symbol it makes: Ctrl+Shift+9 shows as `Ctrl+(` on a US layout, in KDE's
dialog, beside each action in Settings, and in the tray menu. It is the same key.

Only the app's own entries are ever touched - those named `da-assistant-…`, or the block
between the markers - and the rest of your shortcuts stay as they are. A binding names
the `.AppImage` file itself when you run the AppImage, so move the file and the next
start rewrites them. When something else takes over - you switch shortcuts off, or log
into an X11 session after a Wayland one - what was written into the desktop is taken back
out.

Upgrading from 3.0: the keys you had working stay, and anything `install-hotkeys.sh`
wrote is taken over or, on KDE, removed in favour of the portal. The script itself is
gone, and so is `--install-hotkeys`.

Run `--probe` any time to see which mechanism is in use.

## Starting automatically

```bash
cp tools/da-assistant.desktop ~/.config/autostart/
sed -i "s|@LAUNCHER@|$PWD/run-assistant|;
        s|@ICON@|$PWD/assistant/data/icons/hicolor/256x256/apps/da-assistant.png|" \
    ~/.config/autostart/da-assistant.desktop
```

The icon path is spelled out because nothing has installed the icon into a system theme;
the app finds its own copy at runtime, but a desktop entry cannot. With the AppImage,
point `Exec` at the `.AppImage` file and drop the `Icon` line — it carries its own.

## What it uses from the system

The AppImage carries its own Python and Python packages, and uses the system's GTK 3 and
WebKitGTK (see [packaging/README.md](packaging/README.md) for exactly what is in it). Run
from source, every package the [README](README.md#from-source) lists is optional except
`python3-gi`, `python3-pil` and `python3-requests`:

- Without WebKitGTK the chat window falls back to plain text.
- Without an indicator library the tray falls back to `XApp.StatusIcon` or
  `Gtk.StatusIcon`.
- Without `python3-xlib` the app cannot listen for shortcuts on X11; it writes them into
  the desktop's keybindings instead where it can (Settings > Advanced > Shortcuts by).
- Without `python3-keyring`, or a keyring for it to use (GNOME Keyring, KWallet), the API
  key is not remembered between runs.
- End-to-end encrypted Privacy Mode needs one more thing that no distribution packages;
  see [Privacy Mode](#privacy-mode).

### Screenshots

Nothing extra is needed if `xdg-desktop-portal` is running with a backend for your
desktop — that is the preferred path. Otherwise install your desktop's native tool:

| Desktop | Package |
|---|---|
| KDE Plasma | `spectacle` |
| GNOME | `gnome-screenshot` |
| Hyprland / Sway | `grim` and `slurp` |
| Cinnamon | built in (`org.Cinnamon` D-Bus) |

KDE Plasma is the exception to "nothing extra": its portal's *Request screenshot* dialog
offers full screen, current screen and active window, and no rectangular region at all,
so `spectacle` is what selects a region there. Without it, KDE falls back to grabbing the
whole screen and cropping it in a window of ours.

On X11 nothing extra is required at all.

### Clipboard

Clipboard fallbacks, used when GTK's own clipboard read comes back empty:
`wl-clipboard` on Wayland, `xclip` or `xsel` on X11, and Plasma's Klipper over D-Bus on
KDE. On Wayland these are not optional extras: a client is only offered the clipboard
while it holds keyboard focus, which a tray app never does, so GTK returns nothing on
every read. KDE is covered by Klipper without installing anything; elsewhere on Wayland,
install `wl-clipboard`.

### Requests

Requests to NanoGPT are gzipped above 16 KB — every turn resends the conversation and any
capture, and the body has to finish uploading before the model is dispatched. No other
provider is sent a gzipped body: Ollama answers one with HTTP 400. Claude models are
also sent `promptCaching`, since they cache nothing unless asked; other routes cache
implicitly.

**Requests go over https, or not at all.** A provider's address that is plain `http://` is
accepted only for this machine and its local network - `localhost`, `127.x`, `10.x`,
`172.16`-`31.x`, `192.168.x`, link-local addresses and `.local` names - which is how a
model server of your own is reached. For any other address, Settings will not save it,
and a `config.json` edited by hand to hold one sends nothing: the chat says why instead.
The address is never rewritten to https for you, and certificates are always checked.
(Up to 3.2.0 such a URL was only warned about, and then used.)

## Where things are kept

| What | Where |
|---|---|
| Settings | `~/.config/da-assistant/config.json`, readable only by you (`0600`) |
| API keys | your desktop's keyring - GNOME Keyring, or KWallet on Plasma - under `da-assistant`, one entry per provider |
| Model lists, dictionary index, error log | `~/.cache/da-assistant` |
| The encyclopedia's database, the Private Mode client and Ruffle when run from source | `~/.local/share/da-assistant` |
| A hidden launcher entry, for the GlobalShortcuts portal (KDE, GNOME 48+) only | `~/.local/share/applications/org.digitalarchon.DAAssistant.desktop` |

An API key is never written to `config.json`. The keyring encrypts it and unlocks it
with your login. A key that an older version left in `config.json` is moved into the
keyring the next time the app starts. Where there is no keyring, a key typed into
Settings is used until the app quits, and Settings shows why it cannot be kept.

## Troubleshooting

Run `./run-assistant --probe`, or open **Diagnostics** from the tray menu, which shows the
same information and has a Copy button.

**Screenshots produce nothing.** `--probe` prints the capture fallback chain. If it says
`NONE`, install a tool from the table under [Screenshots](#screenshots). The chain has six rungs and the last one only
needs a fullscreen grab, so a total failure usually means no portal *and* no native tool.

**A long identifier comes back wrong.** Captures are downscaled above
`capture_max_long_edge` (3,000 px). Measured on a 3200x1100 console capture, three runs
per size, the old 2,000 px cap lost a tracking number every time and 2,600 px and above
lost nothing — so raise the cap in `config.json` if you routinely capture wider regions
of dense text. Downscaling a crisp screenshot also *costs* bytes, since resampling noise
compresses worse than the original.

**The model misreads text in screenshots.** Turn on *Save every image sent* in Settings and
look at the PNGs in `~/.cache/da-assistant/captures` — that is exactly what was
transmitted. Check the capture is not dimmed or clipped, and that the region is tight
around the text. Small captures are upscaled automatically (up to 3x); the confirm dialog
shows the dimensions actually sent.

**Shortcuts do nothing.** Confirm the app is running (`--probe` reports it and how
shortcuts are bound), then open **Settings > Shortcuts**: a key another program holds,
or one the desktop did not assign, is marked there. On KDE and GNOME's portal, the key
the desktop actually bound is shown beside each action, and is the one to press.

**It does not follow the desktop's theme.** `--probe` prints the GTK theme name, where
the theme was looked for, and whether it was found. GTK is the system's, the AppImage's
included, so a theme the desktop names and the system does not ship leaves GTK drawing
its built-in Adwaita: on KDE that means installing `breeze-gtk`. On Wayland the
name itself comes from `~/.config/gtk-3.0/settings.ini` rather than an XSettings daemon,
which on KDE is written by `kde-gtk-config` - so the same machine can be themed on X11
and not on Wayland. `GTK_THEME=Adwaita:dark` overrides the lot.

**Nothing appears in the system tray.** GNOME needs the AppIndicator extension. `--probe`
reports which tray backend was selected.

**The clipboard always reads as empty on Wayland.** See [Clipboard](#clipboard):
nothing can be read without focus. On KDE this goes through Klipper, so check Klipper is
enabled in Plasma's clipboard settings; elsewhere install `wl-clipboard`.

**A window closes and no answer arrives.** Something failed where there was no terminal
to print it to. Unhandled errors are shown as a notification and appended to
`~/.cache/da-assistant/errors.log`, whose path `--probe` prints.

**The chat window says it is using plain text.** The host's webkit2gtk could not be
loaded into this process, so the transcript is built from widgets instead: the same
bubbles and role labels, with headings, emphasis, code blocks, quotes and lists, drawn
with GTK text tags and coloured from the theme. What is missing there is tables, syntax
highlighting and the per-block copy buttons. `--probe` says which of the two
reasons it was — a missing typelib, or a library that loaded and then did not work —
and for the second names the library or symbol at fault: usually a half-installed or
partly updated WebKitGTK, which reinstalling or updating fixes. `DA_ASSISTANT_NO_WEBKIT=1` forces plain text by hand, and if WebKit ever ends the
process outright the app switches to plain text by itself after the second time. That
verdict belongs to the version that recorded it, so an update always gets a fresh try
(or delete `~/.cache/da-assistant/webkit-crash`).

## For developers

How the AppImage is built, pinned and verified is in
[packaging/README.md](packaging/README.md).

### Layout

```
run-assistant              launcher
README.md, MANUAL.md       what it is and how to install it; everything else
LICENSE                    GPL-3.0
assistant/
  config.py                settings, stored 0600; API keys go to keychain.py
  providers.py             NanoGPT, OpenRouter, Ollama, another endpoint: what differs
  netguard.py              https, or nothing: which API addresses may be used
  keychain.py              the desktop's keyring (Secret Service), for the API keys
  session.py               display server / desktop / portal probing
  imaging.py               upscale, flatten, black out, encode before sending
  attachments.py           pictures and text files attached to a message
  llm.py                   OpenAI-compatible client, streaming, vision, web search
  privacy.py               Privacy Mode: TEE attestation, reply signatures, the session
  sealed.py                end-to-end encrypted Private Mode, via Tinfoil's SDK
  ethsig.py                Keccak-256 and secp256k1 recovery, for TEE signatures
  dcap.py                  Intel TDX quote verification: chain, revocation, TCB
  nras.py                  NVIDIA's GPU verdict, its signatures checked
  vendored.py              puts the Private Mode client ahead of system packages
  models.py                model catalogue: fetch, cache, search
  prompts.py               all prompt text
  hotkeys.py               shortcuts: which mechanism, X11 grabs, the portal, migration
  shortcuts.py             key combinations, and each desktop's spelling of them
  keybindings.py           shortcuts written into GNOME, Cinnamon, Hyprland and Sway
  ipc.py                   single-instance D-Bus service
  clipboard.py             clipboard reads with CLI fallbacks
  hostenv.py               hands spawned host binaries their original environment
  bundle.py                in the AppImage: what the system's GTK stack lacks, from fallbacks
  errors.py                unhandled failures -> notification + ~/.cache log
  collector.py             Python's cycle collector, on the main loop only (WebKit)
  theming.py               which GTK theme was asked for, and whether it was found
  webkitguard.py           drops the WebKit view if it has been ending the process
  websearch.py             web search we run ourselves, for the query and the sources
  reference/               the user's own reference works, read in place
    dsl.py, stardict.py    the two dictionary formats; dictzip.py for their .dz files
    dictionaries.py        finding, indexing and looking up across a folder of them
    axion.py               the DVD's AxionDB tables, zipped or unpacked
    encyclopedia.py        the DVD's import, search and articles for the model
    encyclopedia_html.py   an article as a page for the viewer
    ruffle.py              where Ruffle, for the DVD's Flash animations, is installed
  icon.py                  the app icon, and the theme path that finds it by name
  data/icons/              generated by packaging/make-icon.py, committed
  capture/                 screenshot backends and the region selector
  ui/                      tray, chat window, dialogs, model browser, Markdown
    article_view.py        the encyclopedia's article pane and the Flash window
    encscheme.py           enc://, which serves the DVD's pictures and animations
    encyclopedia_windows.py  Search Encyclopedia, the reader, and Ask Encyclopedia
    bubbles.py             the transcript as widgets, when WebKit is unavailable
    redact.py              the black-out editor, for captures and attached pictures
    shortcuts.py           Settings > Shortcuts, and its key-capture button
tools/install-private-mode.sh  installs the end-to-end encryption client from the lock
tools/install-ruffle.sh    installs Ruffle from packaging/ruffle.lock
packaging/                 the reproducible AppImage build (see packaging/README.md), the
                           icon source, and the hash-pinned locks for the Private Mode
                           client and Ruffle
tests/                     regression tests
```

### Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite opens real GTK and WebKit windows. To keep them off your screen, run it on a
virtual display:

```bash
xvfb-run -a python3 -m unittest discover -s tests
```

It peaks at about 1 GB. To be sure a regression cannot take the machine with it, run
it under a memory cap:

```bash
systemd-run --user --scope -p MemoryMax=2500M -p MemorySwapMax=0 \
  xvfb-run -a python3 -m unittest discover -s tests
```

GTK's own Broadway backend runs the suite too, but WebKit lays pages out nonsensically
there - a negative device pixel ratio - so it is no use for looking at anything.

The dictionary tests build their dictionaries on the spot. Keep it that way: no real
dictionary or encyclopedia text belongs in this repository.

`tests/test_no_overlay_contamination.py` guards the defect that made OCR unreliable: the
selector must crop from a buffer captured *before* any UI is shown, and must have no way
to re-grab the screen.

`tests/test_chat_streaming.py` drives the real chat window against a stubbed client and
counts the elements in the live WebKit DOM. It guards the bug where a finished response
appeared twice, because the streaming placeholder was left in the document instead of
being replaced.

`tests/test_image_query_model.py` guards who answers a question about an image: the
selected text model, given the image when it accepts images and the vision model's
reading when it does not, with OCR staying on `vision_model` - in Privacy Mode too.
Nothing in the UI would show the plumbing going wrong — some model still answers, just
worse — so it calls the real `Assistant` methods rather than a copy of them.

`tests/test_privacy.py` guards Privacy Mode against a fake nano-gpt that attests, signs
and streams like the real one: nothing is sent to an enclave that fails attestation or
to a model outside the conversation, a private request never searches, every reply's
signature is checked, and a chat window keeps its private models after Privacy Mode is
switched off. The sealed-transport tests run where the Private Mode client is installed.

`tests/test_websearch.py` guards the search being ours: the query comes from the user's
own message, the provider settings reach `/api/web`, and the citations are built from the
results actually fetched. It also pins gzip upload and the Claude cache request.

`tests/test_icon.py` guards the tray icon resolving by name. The tray backends take a
name rather than a file, so a PNG in the source tree is invisible unless its directory
is on the icon theme's search path — and, for an indicator, unless that path is
published to the panel, which draws the icon in a different process.

`tests/test_appimage_packaging.py` guards the AppImage's handover to the host: every
variable `AppRun` overrides has to be stashed for `hostenv.py` to give back, or the
screenshot and clipboard tools start failing on somebody else's distribution. It also
checks that a fallback is used only where the system lacks it, and that every input to
the build stays pinned by hash.

`tests/test_https.py` guards the API key never crossing the network unencrypted: which
addresses may be used, that Settings will not save any other, and that the chat, the
model list, web search and Privacy Mode each send nothing to one.

`tests/test_providers.py` guards what differs between providers: where a request goes and
with which key, that only NanoGPT is sent a gzipped body, how each model list is read,
that a private conversation goes nowhere but NanoGPT, that an older settings file is
moved over, and that a model Ollama does not stop is cut off where it starts over.

`tests/test_keychain.py` guards the API key staying out of `config.json`: saved to the
keyring, moved there from a file an older version wrote, and kept rather than lost when
the keyring refuses it. No test reads or writes the real keyring.
