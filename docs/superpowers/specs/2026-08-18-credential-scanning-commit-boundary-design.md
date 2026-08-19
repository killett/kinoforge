# Design — credential scanning at the commit boundary

- **Date:** 2026-08-18
- **Status:** validated (operator approved 2026-08-18)
- **Brief:** close the gap between `.gitignore` and the credential regexes
- **Upstream verification:** `docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md`
  — F7 (no secret scanning at the commit boundary, three disagreeing lists) and F8 (transcript hook
  is user-scoped, its guard fails open) are both CONFIRMED at that audit's HEAD and are the two
  findings this design closes.

## Problem

`.gitignore` matches **paths**. It is well built and it stops `.env`, `.gcp/`, `.aws/` from ever
being tracked. It does nothing about a credential **pasted into an already-tracked file**, which is
the realistic leak vector in an agent-assisted repo: `PROGRESS.md` is 438 KB of session notes and
`successful-generations.md` is 189 KB, both full of pasted terminal output.

Good credential regexes already exist — in three places, all wired to fixture-writing and tool
stderr, none of which runs at `git commit`:

| Location | Patterns | Wired to |
| --- | --- | --- |
| `tools/_redact.py` | 5 | `tools/` debug + error paths (`safe_print`) |
| `~/.claude/hooks/redact_secrets.py` | 13 | Claude Code PostToolUse transcript scrub |
| `tests/providers/conftest_runpod.py` | 7 | fixture-recording leak assertion |

The lists disagree, and the disagreement runs the wrong way: the weakest list is the one wired into
repo tooling, and the best-defended path is the test fixtures. `tools/_redact.py` catches **no AWS
key and no PEM private key**. The user-scope hook matches `AKIA` only, so STS temporary credentials
(`ASIA…`) — exactly what a SkyPilot instance-profile session produces — are invisible to it. Only
the hook ↔ `tools/_redact.py` pairing is parity-tested; `conftest_runpod.py`'s two stronger patterns
can be deleted without any test noticing.

Separately, the transcript hook lives **outside the repo**. A fresh clone gets no transcript
protection, a green test suite, and no signal that the backstop is absent, because
`tests/test_redact_hook_parity.py` calls `pytest.skip` when the hook file is missing unless
`KINOFORGE_REQUIRE_REDACT_HOOK=1` is set — and that variable appears nowhere else in the repo. That
is fail-open, the opposite of the fixture redactor's fail-closed posture. The repo currently ships
two credential mechanisms with opposite failure semantics.

A scan of all 1377 tracked files (pre-existing patterns plus a broader set) found 23 hits, all
synthetic test data. **The tree is clean today.** This design is about keeping it that way without
depending on anyone's vigilance.

## Goals

1. One credential pattern list in the repo, imported by every consumer that can import.
2. A pre-commit hook that scans **staged content** and blocks on a hit.
3. The transcript hooks committed inside the repo at `.claude/hooks/`, registered in a committed
   `.claude/settings.json`, so they travel with a clone.
4. The parity/presence test fails **closed** by default, with an explicit opt-out for environments
   where it genuinely does not apply.
5. A `CLAUDE.md` credential-safety section covering the rules a hook cannot enforce.

## Non-goals

- Rewriting git history. The tree is clean; there is nothing to excise.
- Entropy-based or ML secret detection. High false-positive rate on a repo full of model hashes,
  pod ids, and base64 provision payloads. Named-shape regexes plus credential-variable assignment
  context cover kinoforge's actual credential set.
- Scanning commit messages or branch names. Out of the brief's scope; the staged-content hook is the
  boundary being closed.
- Removing `.gitignore` coverage. This is additive defence, not a replacement.

## Unit 1 — `src/kinoforge/core/credential_patterns.py` (the one list)

A stdlib-only module with no kinoforge imports, so every consumer can take it without pulling the
package's dependency graph.

```python
class CredentialPattern(NamedTuple):
    name: str            # snake_case identifier, used in <REDACTED:{name}> markers
    regex: re.Pattern[str]

CREDENTIAL_PATTERNS: list[CredentialPattern]
PLACEHOLDER_MARKERS: frozenset[str]
ALLOW_PRAGMA: str = "kinoforge: allow-secret"

def redact_string(text: str) -> str: ...
def iter_findings(text: str, *, skip_placeholders: bool = True) -> Iterator[Finding]: ...
def looks_like_placeholder(match_text: str, line: str) -> bool: ...
```

`Finding` carries `pattern_name`, `line_no`, `col`, and `redacted_excerpt` — never the matched
value. Everything downstream (scanner output, test failure messages) prints the excerpt, so a
scanner that catches a leak does not itself write the credential to a terminal, a CI log, or this
conversation's transcript.

### Pattern set

Union of the three current lists, taking the **strongest** variant wherever they disagree, plus the
shapes kinoforge's own credential set implies (derived from `.env.example` variable names).

Ordering is significant and preserved from `tools/_redact.py`: `bearer_auth` is declared first so a
`Bearer rpa_…` header collapses to `<REDACTED:bearer_auth>` rather than leaking the word `Bearer`
around a redacted body.

| name | shape | source |
| --- | --- | --- |
| `bearer_auth` | `Bearer\s+[A-Za-z0-9._\-]{8,}` | all three lists |
| `rpa_token` | `\brpa_[A-Za-z0-9_\-]{8,}\b` | RunPod, all three |
| `hf_token` | `\bhf_[A-Za-z0-9_\-]{8,}\b` | HF, all three |
| `fal_key` | `\bfal_key_[A-Za-z0-9_\-]{8,}\b` | fal, all three |
| `sk_token` | `\bsk-[A-Za-z0-9_\-]{20,}\b` | all three |
| `aws_access_key` | `\b(?:AKIA\|ASIA)[0-9A-Z]{16}\b` | **conftest variant wins** — closes the STS gap (F7) |
| `pem_private_key` | full `BEGIN…END PRIVATE KEY` span | **conftest variant wins** — hook's marker-only form leaves the body |
| `github_token` | `\bghp_[A-Za-z0-9]{36,}\b` | hook |
| `github_app` | `\b(?:gho\|ghu\|ghs)_[A-Za-z0-9]{36,}\b` | hook |
| `replicate_token` | `\br8_[A-Za-z0-9]{30,}\b` | hook |
| `runway_key` | `\bkey[-_][A-Za-z0-9]{30,}\b` | hook, widened to Runway's real `key_<hex>` form |
| `slack_token` | `\bxox[bpars]-[A-Za-z0-9-]{10,}\b` | hook |
| `jwt` | `\beyJ[A-Za-z0-9._=-]{20,}\b` | hook |
| `luma_key` | `\bluma-[A-Za-z0-9-]{20,}\b` | new — `LUMAAI_API_KEY` |
| `modal_token` | `\b(?:ak\|as)-[A-Za-z0-9]{20,}\b` | new — `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` |
| `lambda_key` | `\bsecret_[A-Za-z0-9]+_[0-9a-f]{32,}\b` | new — `LAMBDA_API_KEY` |
| `gcp_access_token` | `\bya29\.[A-Za-z0-9._\-]{20,}\b` | new — `gcloud auth print-access-token` output |
| `credential_assignment` | `\b(?:AWS_SECRET_ACCESS_KEY\|RUNPOD_API_KEY\|…)\s*[=:]\s*["']?[^\s"'#]{8,}` | new — see below |

`credential_assignment` is the pattern that actually addresses this brief's stated leak vector. A
pasted `export AWS_SECRET_ACCESS_KEY=<40 chars>` line has no distinctive token prefix to key off, so
the **variable name** is the signal. The name list is built from `.env.example`'s credential
variables (`AWS_SECRET_ACCESS_KEY`, `AZURE_CLIENT_SECRET`, `B2_APPLICATION_KEY`, `CIVITAI_TOKEN`,
`DOCKERHUB_TOKEN`, `FAL_KEY`, `HF_TOKEN`, `KINOFORGE_R2_SECRET_ACCESS_KEY`, `LAMBDA_API_KEY`,
`LUMAAI_API_KEY`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `REPLICATE_API_TOKEN`, `RUNPOD_API_KEY`,
`RUNPOD_TERMINATE_KEY`, `RUNWAYML_API_SECRET`, `VAST_API_KEY`, `GH_TOKEN`, `AWS_ACCESS_KEY_ID`,
`KINOFORGE_R2_ACCESS_KEY_ID`). `GOOGLE_APPLICATION_CREDENTIALS` and `DOCKERHUB_USERNAME` are
excluded — the first holds a path, the second a username. The `{8,}` floor means `.env.example`'s
empty `AWS_SECRET_ACCESS_KEY=` lines do not match.

### Shapes deliberately excluded

`CIVITAI_TOKEN` (32 hex), `VAST_API_KEY` (64 hex), and `B2_APPLICATION_KEY` (31 alnum) have no
distinguishing prefix. Bare 32/64-hex patterns would match md5/sha256 digests, which this repo
carries in quantity (`pixi.lock`, model manifests, ComfyUI fixtures). They are covered by
`credential_assignment` when they appear next to their variable name — which is how a pasted
terminal line actually looks — and are otherwise left alone. **This is a deliberate coverage/noise
trade, recorded here so a future reader does not mistake it for an oversight.**

### Consumers

- `tools/_redact.py` — keeps `redact_string` / `safe_print` as its public surface, re-exports
  `_CREDENTIAL_PATTERNS` from the shared module. No caller changes.
- `tests/providers/conftest_runpod.py` — imports the shared list. Its two strong patterns are now
  in the shared list, so nothing is lost and drift becomes structurally impossible.
- `tools/scan_secrets.py` — Unit 2.
- `.claude/hooks/*.py` — **cannot** import (Unit 4); duplicates the list, guarded by Unit 5.

## Unit 2 — `tools/scan_secrets.py` (staged-content scanner)

### Why staged content, not the working tree

`git add -p` stages a hunk, not a file. A working-tree scanner would either block a commit over an
unstaged line (false block, teaches `--no-verify`) or miss a staged line whose file was subsequently
edited. The scanner therefore reads what is actually being committed:

```
git diff --cached --unified=0 --no-color -- <paths>
```

and scans **added lines only** (`+`, excluding the `+++` header), tracking file and line number from
the `@@ -a,b +c,d @@` headers. Deleted lines are irrelevant — removing a credential is not a leak.

**Binary files are handled structurally, not by extension guessing.** Git emits
`Binary files a/x and b/x differ` for them and no `+` lines at all, so a binary blob produces zero
scannable content and cannot crash the scanner on a decode error. Text-decode is still defensive:
diff output is read as UTF-8 with `errors="replace"`.

### Modes

| Invocation | Behaviour |
| --- | --- |
| `scan_secrets.py <paths…>` | scan staged added-lines for those paths (pre-commit passes filenames) |
| `scan_secrets.py --all-tracked` | scan the working-tree content of every file listed by `git ls-files -z`, skipping any file whose bytes are not valid UTF-8 (binary); used by the standing-guard test |
| `scan_secrets.py --stdin` | scan text on stdin; used by unit tests and ad-hoc checks |

Exit 0 = clean, 1 = findings, 2 = usage/git error. A git error is **not** silently treated as clean.

### Output

```
tools/scan_secrets.py: 1 credential-shaped match in staged content

  PROGRESS.md:12841  aws_access_key  …export AWS_SECRET_ACCESS_KEY=<REDACTED:aws_access_key>…

Commit blocked. If this is a placeholder, mark it (example / xxxx / <PLACEHOLDER> / ${VAR})
or add the pragma comment `kinoforge: allow-secret` on that line.
If it is real: rotate the credential first, then remove it from the staged content.
```

The excerpt is redacted before printing. The "rotate first" instruction is deliberate — by the time
a human is reading this the value has already been in a shell buffer and possibly a transcript.

### Wiring

A local `.pre-commit-config.yaml` hook alongside the existing ones, `entry: pixi run python
tools/scan_secrets.py`, `language: system`, `types: [file]`, `pass_filenames: true`. Consistent with
every other hook in that file (all `pixi run python -m …`), so it inherits the same env story.

## Unit 3 — false-positive suppression

> A scanner people routinely bypass with `--no-verify` has stopped working.

Two suppression mechanisms, both cheap to read at the point of a block:

1. **Placeholder markers.** A match is skipped when the matched text *or its line* contains a
   case-insensitive marker: `example`, `xxxx`, `placeholder`, `<placeholder>`, `${`, `your-`,
   `your_`, `dummy`, `fake`, `changeme`, `redacted`, `<redacted`, `deadbeef`, `notreal`, `sample`.
   This keeps `.env.example`, the design docs, and the fixture files clean — several tracked files
   carry realistic-looking synthetic tokens and must not trip the scanner.
2. **Line pragma.** `kinoforge: allow-secret` anywhere on the line suppresses that line entirely.
   The escape hatch for a synthetic value that genuinely has no marker in it — e.g. a fixture
   asserting the redactor's behaviour on a bare `AKIA…` string. Preferred over a path allowlist:
   it is local, visible in review, and cannot silently widen to a whole directory.

Tuning is **empirical**: the acceptance bar for this unit is `--all-tracked` returning zero findings
over all 1377 tracked files. Any file that still trips gets a marker or a pragma, and if a *pattern*
produces broad noise it is narrowed rather than allowlisted file-by-file.

## Unit 4 — `.claude/` travels with the clone

Three committed files:

- `.claude/hooks/redact_secrets.py` — PostToolUse scrub, ported from the user-scope hook.
- `.claude/hooks/block_secret_reads.py` — PreToolUse **deny**. New.
- `.claude/settings.json` — registers both.

Both hooks are **stdlib-only and self-contained**: they run without the pixi env, without the
project on `sys.path`, and without a working `kinoforge` install, because Claude Code invokes them
as bare `python3` on an arbitrary cwd. That constraint is why the pattern list is duplicated into
them rather than imported — and why Unit 5's drift test exists.

### Block vs scrub — the argument

The brief asks whether the transcript hook should block rather than scrub. **Ship both.** They fail
in opposite directions and neither subsumes the other.

*Blocking is strictly better where it applies.* Post-hoc scrubbing cannot catch a novel credential
format, a value split across lines, or a value that a regex simply misses — and by the time it runs,
the bytes already exist in the tool result. `PreToolUse` denial of `echo $AWS_SECRET_ACCESS_KEY`,
bare `env`, `cat .env`, `gcloud auth print-access-token`, and `aws configure get` means the value is
never produced, so there is nothing to miss. The denial is also *teachable* in a way a scrub is not:
the refusal names the safe alternative, `[ -n "${VAR:-}" ] && echo set`, which is the shape the
project's own memory already mandates ("print only length-and-shape").

*Blocking alone is insufficient*, which is why the scrubber stays. A credential can arrive in tool
output the hook never had a chance to veto: an API error body echoing an `Authorization` header, a
provider log tail, a `curl` response, a `bootstrap.log` fetch. Those are not command patterns and
cannot be enumerated in advance. The scrubber is the layer that covers arrivals rather than
requests.

The failure semantics also differ, and both are correct for their layer. The PreToolUse blocker
fails **closed** on a pattern it recognises (deny, exit 2) and open on anything else — it can only
deny what it can name. The PostToolUse scrubber stays **fail-open** on exception: a crashing
scrubber must not break the ability to debug, and it is a defence-in-depth layer, not the primary
control. The primary control remains "the credential is not in the repo and not in the file."

### PreToolUse deny surface

Matched against `tool_input.command` for `Bash` only. Denied:

| Shape | Example |
| --- | --- |
| Bare env dump | `env`, `printenv` with no argument |
| Credential var echo | `echo $X` / `echo ${X}` / `printenv X` where `X` matches `(KEY\|TOKEN\|SECRET\|PASSWORD\|PASSWD\|CREDENTIAL)` |
| Dotenv read | `cat`/`less`/`head`/`tail`/`bat`/`rg`/`grep` targeting `.env` (not `.env.example`) |
| Cloud token print | `gcloud auth print-access-token`, `gcloud auth print-identity-token`, `aws configure get *secret*`, `modal token …`, `runpodctl config` |

Deliberately **not** denied: `[ -n "${VAR:-}" ] && echo set`, `env | wc -l`-style shape probes that
name no credential variable, and `rg` over `.env.example`. The narrow surface is the point — a
blocker that fires on ordinary work gets disabled, and a disabled blocker protects nothing.

Denial returns the documented `PreToolUse` deny payload
(`hookSpecificOutput.permissionDecision = "deny"`) with a reason string naming the safe alternative.

## Unit 5 — fail-closed parity + standing guard

`tests/test_redact_hook_parity.py` is rewritten around two assertions:

1. **Repo hooks ⊇ shared list — no skip path.** `.claude/hooks/redact_secrets.py` is committed, so
   its absence is a genuine failure, not an environment difference. If the file is missing, the test
   **fails**. This is the inversion the brief asks for.
2. **User-scope hook — fails closed with an explicit opt-out.** `~/.claude/hooks/redact_secrets.py`
   is still the hook that runs on the operator's machine today. The presence check now fails by
   default; `KINOFORGE_SKIP_USER_REDACT_HOOK=1` is the documented opt-out for environments where it
   genuinely does not apply (CI runners, containers without Claude Code). The variable name is
   asserted to appear in `CLAUDE.md`, so the opt-out cannot become folklore.

Plus the standing guard: a test invoking `scan_secrets.py --all-tracked` and asserting zero
findings. This is what makes the work a guard rather than a one-time cleanup — a credential pasted
into `PROGRESS.md` fails the suite even if the committer used `--no-verify`.

### Test list (all offline, no live spend)

| Test | Concrete bug it catches |
| --- | --- |
| staged real-shaped key blocked | scanner reads the working tree, or the pattern does not fire → exit 0 on a leak |
| same value + placeholder marker not blocked | suppression missing → `.env.example` blocks every commit → `--no-verify` habit |
| partial staging: only the staged hunk scanned | scanner reads the file instead of the diff → blocks on an unstaged line |
| binary file staged | scanner decodes blob bytes → `UnicodeDecodeError` crash → exit 2 on an innocent commit |
| pragma suppresses a bare synthetic token | no escape hatch → a legitimate fixture cannot be committed |
| git failure exits 2, not 0 | error treated as clean → silent no-op scanner |
| repo hook ⊇ shared list | a pattern added to the shared list never reaches the transcript hook |
| repo hook missing → **fail** | the F8 fail-open reappears |
| user hook missing → fail unless opt-out set | ditto, at user scope |
| `--all-tracked` returns zero | a credential lands in a tracked file and nothing notices |

Scanner tests build throwaway git repos under `tmp_path` (`git init`, `git add`, real `git diff
--cached`) rather than mocking git — the entire point of the unit is its interaction with git's
staging area, so mocking that boundary would test nothing.

## Unit 6 — `CLAUDE.md` credential-safety section

Rules a hook cannot enforce, stated for humans and agents both:

- Never echo a credential variable. Print length and shape only:
  `[ -n "${HF_TOKEN:-}" ] && echo "HF_TOKEN set len=${#HF_TOKEN}"`.
- Never paste a credential into a config, fixture, test, commit message, or design doc — including
  "just to check the shape." Use the synthetic conventions already in the repo
  (`kinoforge-prod-deadbeef` and friends).
- Prefer identity probes over key inspection: `aws sts get-caller-identity`,
  `gcloud config list account`, not `aws configure get aws_secret_access_key`.
- Credentials live in `.gitignore`d workspace dirs (`.env`, `.gcp/`, `.aws/`). Claude never
  Writes/Edits a secret-bearing file; the operator creates it.
- If a credential does reach a file, a terminal, or a transcript: **rotate first**, clean second.

And the residual risk, stated honestly: **a regex filter reduces exposure, it does not eliminate
it.** The scanner catches named shapes and credential-variable assignments. It does not catch a
novel provider's format, a value split across lines, a base64-wrapped blob, or a credential
paraphrased into prose. `.gitignore` + the scanner + the hooks are three imperfect layers; the
control that actually works is not putting the credential there.

`AGENTS.md` gets a pointer to this section rather than a duplicate, so the two cannot drift.

## Rollout order

1. Unit 1 (shared list) + rewire `tools/_redact.py` and `conftest_runpod.py` — pure refactor, suite
   stays green.
2. Unit 2 + 3 (scanner + suppression), TDD, then tune against `--all-tracked` until zero.
3. Wire the pre-commit hook (after the tree is proven clean — otherwise the first commit of this
   work blocks itself).
4. Unit 4 (`.claude/` hooks + settings).
5. Unit 5 (fail-closed parity + standing guard).
6. Unit 6 (docs).

## Risks

| Risk | Mitigation |
| --- | --- |
| Scanner blocks legitimate commits → `--no-verify` habit | Unit 3 suppression, tuned to zero findings on the current tree before the hook is wired |
| Shared list drifts from the `.claude` duplicates | Unit 5 parity test, no skip path |
| PreToolUse blocker fires on ordinary work | Narrow, enumerated deny surface; explicit non-denied list |
| Suppression markers weaken real detection | Markers are placeholder words unlikely to co-occur with a live credential; pragma is line-scoped and visible in review |
| `pixi run` unavailable in a bare-git environment (hook entry fails) | pre-commit hook failure is loud, not silent; the standing-guard test covers the same ground inside the suite |
