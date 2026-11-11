# Security policy

## Reporting a vulnerability

Please email **jason@digitalarchon.com.au** with the details and, if you can, steps to
reproduce. Don't open a public issue or pull request for a vulnerability.

You'll get a reply as soon as I can manage. This is a one-person project with no bug bounty,
but reports are taken seriously and credited in the fix unless you'd rather not be named.

## Supported versions

Only the latest release, 4.4.x (or `main`), gets fixes.

## What's in scope

Anything that breaks one of DA Assistant's promises, which
[docs/security-and-privacy.md](docs/security-and-privacy.md) sets out in full, for example:

- a request going over plain http to anywhere but this machine, its local network or a Tor
  onion service, or a certificate going unchecked;
- a request that carries a key, the conversation, a question or a search query following a
  redirect, or a public fetch following one where it could not have been sent in the first
  place (from https to plain http, or from the internet into this machine or its network);
- an API key going to any service but its own provider, or a key or a password written into
  an address reaching `config.json`, the cache or a message the app shows;
- with Privacy Mode on, anything reaching a model outside the conversation's attested ones,
  a web search being made, or a private conversation falling back to the ordinary models
  before its window is closed;
- a `private/` model's text leaving this computer unsealed, or a reply being opened that the
  attested enclave did not seal;
- flaws in TEE attestation (a model's own evidence, Chutes' instances, or RedPill's gateway's
  report it may be attested through) or in the reply checks: a reply checked against a key no
  attested quote binds, verified for other bytes than arrived, or verified through RedPill's
  gateway alone without a receipt that holds;
- anything reaching a TEE model while its attestation is under way or after it was refused, or
  a banner, badge or message mark claiming more than was attested (RedPill's gateway is not
  the machine that runs the model);
- the optional TEE provider lookup (Settings > Privacy) sending api.redpill.ai anything but a
  session's id, sending it anything at all while the setting is off, or naming a provider for a
  record the reply's signed receipt doesn't name;
- a capture or an attached picture carrying its metadata to a model, or blacked-out pixels
  reaching one;
- a model's reply, an encyclopedia article or a dictionary entry that makes a window load
  remote resources, run script or go to another page, or a plain http link opening without
  being asked about;
- web search results reaching the model without being marked untrusted, or text in them aimed
  at the model going unflagged;
- an attached file, a dictionary or the encyclopedia's data doing more than being read and
  shown;
- a release's AppImage that doesn't rebuild byte for byte from its tag.

Weaknesses in upstream projects (NanoGPT, Tinfoil, RedPill, GTK, WebKitGTK and so on) should go
to those projects, though a heads-up is welcome if DA Assistant can mitigate them.
