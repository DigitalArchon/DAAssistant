# Security and privacy

How DA Assistant handles your security and privacy: what leaves your computer, where it
goes and when, what is kept on it, and what the app cannot protect you from. It describes
the app as of 4.4.0. The [manual](../MANUAL.md) says how to use each part; this document
explains what each part does with your data, and points to the manual for the details.

## In short

What leaves this computer:

- **What you send, to the model you chose.** The text you type, the clipboard text or
  screen region an operation works on, and the pictures and files you attach go to the
  provider of the model that answers - and only when you send them. With
  [Privacy Mode](../MANUAL.md#privacy-mode) on, they go only to nano-gpt's attested
  models.
- **A search query, when a request searches the web.** It goes to NanoGPT, which runs
  the search with a search provider.
- **The providers' model lists are fetched**, with the provider's key where it takes one.
- **In Privacy Mode, attestation requests**, to nano-gpt and to the services that vouch
  for the hardware: Intel and NVIDIA, and for end-to-end encrypted models Tinfoil's and
  Sigstore's servers. None of them is sent anything you wrote.
- **Only with Name the provider behind each TEE reply on**: the id of a reply's session,
  to RedPill (`api.redpill.ai`).

What does not:

- **An API key goes only to its own provider**, and a password written into an address
  only to that address.
- **Nothing of yours is sent on the app's own initiative.** There is no telemetry, no
  crash reporting and no update check. In the background the app only refreshes the
  providers' model lists.
- **Looking a word up in your dictionaries, and searching or reading the encyclopedia
  DVD**, happen on this computer. A question you ask about them goes to the model like
  any other, with what it is about: the entries shown, the articles you tagged, or the
  passages Ask Encyclopedia found.
- **Conversations are not saved.** Each lives in memory until its window closes, unless
  you Copy or Save it.
- **An answer cannot make the app fetch anything.** The chat view loads nothing from the
  internet and runs no script that came in an answer, and a link opens in your browser
  only when you click it.

## The services it contacts

Every one of these is reached over https. Only Ollama and an endpoint of your own may be
plain http, and only on this machine, on your local network or as a Tor onion service
(see [The network rules](#the-network-rules)).

| Service | When | What it is sent | What it is not sent |
|---|---|---|---|
| The provider of each model you choose: NanoGPT, OpenRouter, an Ollama server, or an OpenAI-compatible endpoint of your own | When you send, and for follow-ups | The conversation: your messages, the capture or attached pictures (or the vision model's reading of them, for a model that cannot see), the instructions for the task, the model's name, and that provider's key | Any other provider's key; anything you did not send |
| NanoGPT's web search (`/api/web`) | When a request has **Search the web** ticked | The search query, the search provider chosen, and NanoGPT's key. The query is written by your text model, which is sent the turn to write it from, or, with **Let the model write the search query** off, taken from your message | The rest of the conversation. NanoGPT passes the query to the search provider (Perplexity by default; Linkup, which keeps nothing, for an account that requires zero data retention) |
| The providers' model lists | At start-up when a list is over six hours old, after Settings are saved, from **Browse…**, and before a request to a provider with no list yet | The provider's key where it takes one (NanoGPT, OpenRouter, your endpoint's) | Anything of yours. nano-gpt's list of end-to-end encrypted models is public, and fetched with no key |
| nano-gpt's TEE checks (Privacy Mode, `TEE/` models) | Before the first message to a model, and before a message once its attestation is 15 minutes old; on **Check attestation**; after each reply | For an attestation, the model's name and a random nonce; for a reply's check, its id or receipt id, and the route its headers named. NanoGPT's key | Your messages: those go with the request itself |
| nano-gpt's Private Mode relay (Privacy Mode, `private/` models) | Before each message (a preflight that names the model), and with it | The message sealed end to end; nano-gpt sees your account, the model, the timing, the sizes and the cost | The text: only the attested enclave can open it |
| Intel (`api.trustedservices.intel.com`, `certificates.trustedservices.intel.com`) | When a TEE/ enclave is attested, and by Tinfoil's client for an Intel TDX one | Which platform the quote comes from, to fetch Intel's signed TCB information, revocation lists and quoting enclave identity | No key, nothing you wrote |
| NVIDIA (`nras.attestation.nvidia.com`) | When a TEE/ enclave with GPUs is attested | The GPU evidence nano-gpt relayed, with the app's own nonce; and a request for NVIDIA's public keys | No key, nothing you wrote |
| Tinfoil (`github-proxy.tinfoil.sh`, `kds-proxy.tinfoil.sh`) and Sigstore (`tuf-repo-cdn.sigstore.dev`) | When a `private/` enclave is attested: before its first message, before a message once its attestation is five minutes old, and on **Check attestation** | Requests for the router's release and its Sigstore record, for AMD's certificate for the enclave's processor, and for Sigstore's trust root | No key, nothing you wrote. The enclave's own report comes through nano-gpt's relay |
| RedPill (`api.redpill.ai`) | Only with **Name the provider behind each TEE reply** on (Settings > Privacy, off by default), after a TEE/ reply whose signed receipt holds, at most once a session | The session's id, which the receipt names | Your messages and any key. It goes directly, not through nano-gpt: RedPill sees this computer's internet address (or your proxy's) and that one of its models was used around then. See [Naming the provider](#naming-the-provider-behind-a-tee-reply) |
| Your web browser | When you click a link in an answer, an article or a dictionary entry | The link, handed to the browser | A plain http link to the internet opens only after you say so, in a dialog that names it |

## The network rules

- **https, or nothing.** An address is used only if it is https, or if it never leaves
  this machine or its local network (`localhost`, `127.x`, `10.x`, `172.16`-`31.x`,
  `192.168.x`, link-local and IPv6 unique local addresses, `.local` names), or if it is a
  Tor onion service. Any other plain http address is refused, with the reason, before a
  byte is sent. It is never rewritten to https for you, and certificates are always
  checked.
- **Plain http on your own network is not encrypted.** The app allows it, because that is
  how a model server next door is reached, and it does not ask first: the key and the
  whole conversation cross that network as they are, readable by anyone on it - a café's
  or a hotel's Wi-Fi too. Give the server an https address if it can have one.
- **An address is judged as written**, not by looking up where its name points. One with
  a space, a backslash or a control character in it is refused outright, and so is a
  plain http host with a `%` escape in it, since the parsers involved disagree on where
  such a host ends.
- **Redirects are not followed by anything that carries a key or your words**: the
  models (your messages, a capture, a question, the query written for a search), their
  model lists, the web search, nano-gpt's TEE checks and Private Mode's relay. A redirect
  would send the request on to wherever it points, so the app stops and says where the
  address pointed, without any password written into it; if it has moved, the new one
  goes in Settings. Nothing is sent there. (Up to 4.3.1 one was followed wherever the
  rule below allowed, and a 307 or 308 re-sent the conversation to the new address,
  without the key.)
- **The public fetches** that carry neither - Intel's and NVIDIA's services, Tinfoil's
  releases and certificates, RedPill's session records - follow a redirect only to an
  address that could have been used in the first place: never from https to plain http,
  and never from a server on the internet into this machine or its network. Tinfoil's
  client's own fetches are held to the same rule.
- **A proxy** (Settings > Advanced > Proxy) carries every request the app makes - the
  models, the web search, the model lists, the attestation services, RedPill's session
  records when they are fetched, Tinfoil's client - except to this machine and its own
  network, which are always reached directly. `socks5h://` reaches Tor, and an onion
  service. A proxy elsewhere that takes a password must be https, or the password would
  cross the network as typed. If the proxy set in Settings cannot be used, nothing is
  sent but those local requests, rather than going around it. Through Tor, NVIDIA's and
  Sigstore's services refuse most exits: a `private/` model is then not attested, and
  nothing is sent to it, and a `TEE/` model's banner turns amber for the GPU evidence that
  could not be checked (see [Requests](../MANUAL.md#requests)).

## Keys and passwords

- **API keys are kept in your desktop's keyring** (GNOME Keyring, or KWallet on Plasma),
  under `da-assistant`, never in `config.json`. A key an older version left in the file
  is moved to the keyring. With no keyring, a key typed into Settings is used until the
  app quits, and not written anywhere.
- **A user name and password written into an address** - the proxy's, Ollama's or your
  own endpoint's - are kept in the keyring as well. `config.json` has the address without
  them, and the keyring's copy is put back only while the file still has that same
  address, so a password goes nowhere but where it was typed for. The cached model lists
  keep the address without them too (since 4.4.0).
- **No message shows a password.** Every message the app shows that names an address -
  in Settings, the chat, the startup notice, `--probe` and Diagnostics - names it without
  its user name and password. A key with a line break or a character no key has in it is
  refused without being repeated.
- **Each key goes only to its own provider.** NanoGPT's goes to NanoGPT alone - for the
  chat, its model list, the web search, the TEE checks and Private Mode's relay - and
  never to Intel, NVIDIA, Tinfoil, Sigstore or RedPill. Ollama is sent no key.

## AI models

### Ordinary models

An ordinary provider receives everything in the conversation you send it, and can read
it; what it keeps is up to its own policy. Before anything goes, the app shows the send
dialog for a screen region, where anything in it can be blacked out, and every capture
and attached picture is rebuilt from its pixels alone, so no metadata goes with it (see
[Blacking out part of a capture](../MANUAL.md#blacking-out-part-of-a-capture) and
[Attachments](../MANUAL.md#attachments)). A `private/` model is never sent this way: the
plain path refuses one however it is written.

### Privacy Mode

[Privacy Mode](../MANUAL.md#privacy-mode) sends every operation, and every follow-up, only
to the models set under Settings > Privacy, each attested before anything is sent to it.
A conversation that starts private stays private until its window closes: its models are
fixed when it starts, its model menu reaches only `TEE/` and `private/` models, and it
never searches the web, since a search query would carry your text to a search provider
in the clear. Switching Privacy Mode on makes the open conversations private from their
next message; switching it off changes only conversations started afterwards. If the
private models cannot be used, nothing is sent: it never falls back to the ordinary ones.

### End-to-end encrypted models (`private/`)

nano-gpt's Private Mode models run in enclaves operated by Tinfoil. Before anything is
sent, the enclave is attested: its hardware report (AMD SEV-SNP or Intel TDX) against
the vendor's root, and its software release against Sigstore's record of Tinfoil's
releases (one older than the latest is named). Each message is then sealed on this
machine to a key only that enclave holds, and the reply comes back sealed the same way.
nano-gpt relays ciphertext: it sees your account, the model, the timing, the sizes and
the cost, never the text.

- The enclave is attested again before a message once its attestation is five minutes
  old (time the computer spent asleep counts), and when it says its key has changed.
- Nothing is ever sent unsealed, and nothing falls back to the plain endpoint. Only a
  successful reply that came back sealed is opened; anything else is refused unread.
- No code from nano-gpt runs: the app uses Tinfoil's own client, pinned by hash in the
  AppImage and in `tools/install-private-mode.sh`.
- Before a `private/` model is first used, it is looked up in nano-gpt's public list of
  them, with no key, so that a retired model is never sent anything.

### TEE models (`TEE/`)

A `TEE/` model runs in a trusted execution environment, but **your text passes
nano-gpt's servers in the clear**, so nano-gpt can read it. Attestation shows where the
request is answered, not that nano-gpt cannot read it. For a model whose text should be
sealed end to end, use a `private/` one.

**Before anything is sent**, the enclave is attested with a fresh random nonce. Its Intel
TDX quote is verified the way Intel's own Quote Verification Library does it, up to
Intel's root, which is pinned in the app; the GPU evidence goes to NVIDIA's attestation
service with the app's own nonce, and NVIDIA's signed verdict is checked in turn; and
RedPill's gateway's own report is asked for beside the model's, with a nonce of its own.
Anything forged, revoked, for another nonce, or from a debug machine is refused, and
nothing is sent. What could not be checked, or is not current, turns the banner amber.

**What is attested** depends on who serves the model, and the banner says which, first
of all, with what that leaves to someone's word:

- **RedPill's gateway** (most `TEE/` models, which RedPill serves): the banner reads
  **TEE gateway attested**. The gateway passes each request on to the machine that runs
  the model, and that machine is not attested by this app: that it is a verified TEE is
  the gateway's word, in each reply's signed receipt. The GPUs NVIDIA vouched for are
  not tied to the gateway's quote, so they may be another machine's: *GPUs not tied to
  the quote*.
- **Chutes' instances** (models Chutes serves, such as `TEE/kimi-k3`): every instance
  that could answer is attested, each tied to its own GPUs. They sign no replies, so
  nothing shows which instance answered a request. Some models Chutes serves first are
  answered by RedPill's gateway now and then, so the gateway is attested too.
- **One enclave** not known to be either: the one whose key signs the replies.

**What attestation does not prove**: which software runs (no measurement in a quote is
compared with a published one); what you sent (a reply's record hashes the request as
nano-gpt passed it on); the model's own machine, where RedPill's gateway answers; and
that the GPUs are the attested machine's.

**Again every 15 minutes**: an enclave is attested again before the next message once
its attestation is 15 minutes old, time asleep included. While a model is being
attested, a message to it waits; while it is refused, nothing is sent to it.

**Each reply is checked**, where its provider signs one, against the record nano-gpt
keeps of it, signed by the attested key, and the app hashes every byte of the reply as
it arrives:

- *TEE · verified*: the attested key signed the record of the exact text that arrived,
  and RedPill's receipt for it, where it came, holds.
- *TEE · signed, text not compared*: signed by the attested key, but the text could not
  be compared (nano-gpt rebuilds some models' streams; a reply cut short has no whole).
- *TEE · check failed*: signed by another key, for other text, or its receipt does not
  hold.
- *TEE · reply not verified*: no record could be read - Chutes signs none, or the check
  could not be made. For a model attested through RedPill's gateway alone, a reply also
  reads this unless its receipt holds, since only the receipt says the model's own
  enclave answered.

Each badge's tooltip says what the check showed and what is someone's word.

**Your own message is marked too.** It reads *Received by the attested TEE* only once the
record of every reply made from it, signed by the attested key, holds. Otherwise it reads
*⚠ Not shown to reach the attested enclave*: nano-gpt routes each request, Chutes signs no
replies, and a model may be served elsewhere when its provider is busy. Once received, it
stays so: asking again cannot take that back. A message sealed end to end reads *Sealed
end-to-end*.

**Strict** (Settings > Privacy, off by default) refuses any enclave that proved less than
it could - anything that would turn the banner amber - and sends nothing to it. As
nano-gpt's models are in Nov 2026, that leaves `private/` models and the `TEE/` models
Chutes alone serves. After a reply whose check failed, or one the attested keys were to
sign that fell short of verified (no record read, or its text not compared), Strict
attests its model again before sending it anything more, and the reply's badge says so.

### Naming the provider behind a TEE reply

**Name the provider behind each TEE reply** (Settings > Privacy, off by default) shows
which provider RedPill's gateway passed a reply on to - Phala, Fortanix, NEAR AI, 0G,
Tinfoil's router - beside its badge: *TEE · verified · run by Fortanix*.

- **What it costs you.** After a `TEE/` reply whose signed receipt holds, the app asks
  `api.redpill.ai` for its public record of the session the receipt names. That request
  goes to RedPill directly, not through nano-gpt (through your proxy, if one is set), so
  RedPill sees this computer's internet address, or the proxy's, and that one of its
  models was used around then - which nano-gpt's relay otherwise keeps from it.
- **What it sends.** The session's id alone: never your messages, never a key.
- **How much.** Each session's record is read once while the app runs (a session lasts
  about an hour), never more than 1 MB of it, and never for a model Chutes serves itself,
  whose records are megabytes each and change from one reply to the next.
- **What it is worth.** The record is believed only if its SHA-256 is the session id the
  attested gateway signed in the receipt, and the reply was served within the session's
  time. What it says is still RedPill's word: this app does not check that machine. A
  record that cannot be read or believed names nobody, and the tooltip says why; the
  reply's badge never changes for it.
- **When it applies.** The setting is read at each reply: switched on, it applies from the
  next reply, in conversations already open too; switched off, nothing more is looked up.
  **Details…** in a private conversation, `--probe` and Diagnostics say whether it is on.

## What is kept on this computer

| What | Where |
|---|---|
| Settings | `~/.config/da-assistant/config.json`, readable only by you |
| API keys, and passwords written into addresses | your desktop's keyring, under `da-assistant` |
| Model lists, the dictionary index, the error log (readable only by you), a note the chat view keeps of WebKit failing | `~/.cache/da-assistant` |
| Every image sent, only with **Save every image sent** ticked (Settings > Advanced, off by default) | `~/.cache/da-assistant/captures` |
| The encyclopedia's database; Tinfoil's client and Ruffle, when the app is run from source | `~/.local/share/da-assistant` |
| Sigstore's trust root, once a `private/` model has been attested | Tinfoil's client keeps it in `sigstore-python`'s own folders, `~/.local/share/sigstore-python` and `~/.cache/sigstore-python` |
| A hidden launcher entry, for desktops whose shortcuts go through the GlobalShortcuts portal | `~/.local/share/applications/org.digitalarchon.DAAssistant.desktop` |
| Shortcuts, where the app writes them into your desktop's own keybindings | that desktop's own settings |

Kept in memory only, and gone when the app quits: conversations and their captures and
attachments, what each enclave proved, the RedPill session records read (64 at most),
and a key typed in with no keyring to keep it. **Copy** in a chat window puts the
conversation on the clipboard as Markdown, and **Save** writes it where you choose, the
badges' labels with it.

## Other protections

- **Captures and pictures.** A screen region is shown before it is sent, with Black out…
  to paint over anything that should not go; the black is in the pixels, and the
  original goes nowhere. Every capture and attached picture is rebuilt from its pixels
  alone, so no EXIF, location, ICC profile or text chunks go with it. Only the readers of
  the picture formats the app accepts ever look at an attached file.
- **The pages the app shows.** The chat view and the encyclopedia's articles load nothing
  from the internet and run no script that came in an answer or an article: nothing in
  them can call home or take the view to another page. A link opens in your browser only
  when you click it, and a plain http link to the internet only after you confirm it.
- **Web search results** are given to the model as untrusted web content, to use as
  evidence and never as instructions. Text in them that looks like instructions to the
  model, or that no reader can see, is named and the model told to ignore it, and the
  answer says so. The query searched is the one written for the search, not your whole
  message; when none can be written, nothing is searched, and the answer says that too.
- **What nano-gpt's TEE gateway or Private Mode's relay writes** in an error is shown on
  one short line, without line breaks or characters you could not see, and never as the
  model's words: nothing vouches for it.
- **The clipboard** is read only when an operation asks for it, at most 8 MB of text or a
  64 MB picture.
- **Builds can be checked.** Every AppImage can be rebuilt from its source and compared
  byte for byte with the one published, and everything in it is pinned by hash; see
  [packaging/README.md](../packaging/README.md).

## Limits

- **Your provider sees what you send it**, except a `private/` model's provider, which
  relays ciphertext. What it keeps is up to its own policy.
- **A `TEE/` model's text passes nano-gpt in the clear.** Attestation shows genuine,
  current hardware holding a key bound to this app's nonce. It does not show which
  software runs, and where RedPill's gateway answers, the machine that runs the model is
  the gateway's word.
- **End-to-end encryption rests on the enclave**: on Tinfoil's software, measured and
  checked against its published release, and on AMD's or Intel's hardware and the keys
  they vouch for. nano-gpt still sees your account and when, and how much, you send.
- **Plain http on your own network is not encrypted** (see
  [The network rules](#the-network-rules)).
- **Anything running as you on this computer** can read your settings, the cache, the
  keyring once it is unlocked, and the app's memory.
- **A screenshot can hold more than you meant to send.** Look at it, and black out what
  should not go, before you press Send.
- **Answers are written by language models**, and web search results come from the open
  web: either can be wrong, or written to mislead.

## Reporting a security problem

Please don't open a public issue for a vulnerability: [SECURITY.md](../SECURITY.md) says how
to report one privately, which versions get fixes, and what is in scope.
