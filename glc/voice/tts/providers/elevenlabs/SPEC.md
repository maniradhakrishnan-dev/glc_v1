# ElevenLabs TTS — Behavioural Test Spec

> **Team:** Group ElevenLabs · **Slot:** `elevenlabs`
> **Purpose:** Map each of the 7 tests in
> `tests/voice/tts/test_elevenlabs.py` to the exact adapter behaviour it
> requires. This is the contract every implementer (skeleton, HTTP,
> chunking, quota, errors) codes against.

## 1. Purpose & scope

This document is the team-internal behavioural specification for the
ElevenLabs Flash v2.5 TTS provider. It is derived directly from the
**read-only** test and mock source, not paraphrased from `tasks.md`:

- `tests/voice/tts/test_elevenlabs.py` — the 7 tests that must pass
- `tests/voice/tts/mocks/elevenlabs_mock.py` — the mock API fake
- `glc/voice/tts/base.py` — `SynthesizeResult`, `TTSError`, `TTSProvider`

**Do not modify** the tests or the mock. The graded artifact is
`adapter.py` (and `schemas.py`) made to satisfy this spec.

All 7 tests run through the **mock path** — every test constructs
`Provider(config={"mock": mock})` (verified: all seven use the `mock`
fixture and pass it via `config`). The real HTTP path is never exercised
by these tests; it is intended to be covered separately by a
`requires_live_api` live test (to be added by the HTTP owner). This means
the structural tests are satisfied by correctly **delegating to the
mock**; the real-API rules in §6 exist so the delegation pattern, quota
check, and error handling behave identically against the live API.

## 2. Canonical types (`glc/voice/tts/base.py`)

```python
@dataclass
class SynthesizeResult:
    audio_b64: str
    mime: str
    sample_rate: int
    provider: str
    cost_usd: float = 0.0

class TTSError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status      # ← tests read .status directly

class TTSProvider(ABC):
    name: str = ""
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
    @abstractmethod
    async def synthesize(self, text: str, voice_id: str | None = None) -> SynthesizeResult: ...
```

Key points implementers rely on:

- `TTSError` carries `.status` — both the error test and the quota test
  assert on `ei.value.status`, not on the message text alone.
- `TTSProvider.__init__` stores the injected config on `self.config`, so
  the mock is reachable via `self.config.get("mock")`.
- `synthesize` is `async` and takes `(text, voice_id=None)`.

## 3. Mock contract (`ElevenlabsMock`)

`ElevenlabsMock` is a `@dataclass`. Fields and defaults:

| Field | Default | Meaning |
|---|---|---|
| `canned_audio_b64` | `"QUFBQQ=="` (base64 of `"AAAA"`) | Audio string returned on success |
| `canned_mime` | `"audio/wav"` | MIME returned on success |
| `canned_sample_rate` | `24000` | Sample rate returned — **adapter must honour, not hardcode** |
| `received_calls` | `[]` | Appended on **every** call with `{"text_len": len(text), "voice_id": voice_id}` |
| `rate_limited` | `False` | If `True`, raises `TTSError("upstream rate-limited", status=429)` |
| `upstream_failure` | `None` | If `(code, msg)`, raises `TTSError(msg, status=code)` |
| `monthly_chars_used` | `0` | Chars consumed so far this month |
| `monthly_chars_limit` | `10_000` | Monthly cap |
| `last_body` | `None` | Set to `{"text", "voice_id"}` on success |

### Mock `synthesize()` execution order

```
1. received_calls.append({"text_len": len(text), "voice_id": voice_id})
2. if rate_limited:        raise TTSError("upstream rate-limited", status=429)
3. if upstream_failure:    raise TTSError(msg, status=code)
4. last_body = {"text": text, "voice_id": voice_id}
5. monthly_chars_used += len(text)        # increments, does NOT enforce
6. return SynthesizeResult(canned_audio_b64, canned_mime,
                           canned_sample_rate, provider="elevenlabs", cost_usd=0.0)
```

**Critical:** the mock **records the call and increments usage but never
enforces the quota cap.** Quota enforcement is the adapter's job and must
happen **before** delegating to `mock.synthesize()` (see test 7 and §5).

Notes on two fields not driven by the current 7 tests:

- `rate_limited` is supported by the mock (raises `TTSError(status=429)`)
  but **no current test sets it**. It is available for future tests and
  mirrors the real-API back-pressure shape.
- The mock also defines a `record_frame()` method; it is a vestigial
  copy from the streaming-provider mocks (it only acts if a `frames_sent`
  attribute exists, which this dataclass never sets), so it is a no-op for
  the ElevenLabs TTS path and is intentionally not part of this contract.

## 4. The 7 tests → required behaviour

| # | Test | Assertion (from source) | Required adapter behaviour |
|---|------|--------------------------|----------------------------|
| 1 | `test_provider_name_matches` | `adapter.name == "elevenlabs"` | Class attr `name = "elevenlabs"` |
| 2 | `test_synthesize_returns_synthesize_result` | `isinstance(r, SynthesizeResult)`; `r.provider == "elevenlabs"`; `r.audio_b64` truthy; `r.sample_rate > 0` | Return a `SynthesizeResult`; on the mock path simply return what the mock returns |
| 3 | `test_synthesize_passes_text_to_upstream` | `mock.received_calls[-1]["text_len"] == len("hello world")` | Pass the original text through unchanged; recorded length is **total** `len(text)`, never per-chunk |
| 4 | `test_synthesize_records_sample_rate` | with `mock.canned_sample_rate = 22050`, `r.sample_rate == 22050` | Use the sample rate the upstream/mock returns; never hardcode it on the mock path |
| 5 | `test_synthesize_propagates_upstream_error` | with `mock.upstream_failure = (502, "upstream broken")`, `TTSError` raised and `ei.value.status == 502` | Let the mock's `TTSError` propagate untouched; on the real path map HTTP status → `TTSError.status` |
| 6 | `test_synthesize_handles_empty_text` | for `text=""`, `isinstance(r, SynthesizeResult)`, no exception | Handle empty text gracefully — return a valid result, do not crash |
| 7 | `test_channel_specific_behaviour_free_tier_quota_tracking` | with `monthly_chars_used=9990`, `monthly_chars_limit=10000`, message `"this is a long enough message to bust the cap"`: `TTSError` raised, `ei.value.status == 429`, and `"quota" in str(ei.value).lower() or "limit" in str(ei.value).lower()` | Pre-flight quota check **before** delegating; raise `TTSError(status=429)` whose message contains "quota" or "limit" |

### Per-test notes

- **Test 1** is satisfied by the class attribute alone — no `synthesize`
  call is made.
- **Test 2** passes `voice_id="default"`; the value is recorded by the
  mock but not asserted here. The four field assertions are all satisfied
  by the mock's canned return values.
- **Test 3** is the "gateway owns no quirks of its own" check: the text
  must reach upstream byte-for-byte. The recorded `text_len` is the full
  input length even if the real path later chunks the text.
- **Test 4** deliberately overrides `canned_sample_rate` to `22050` to
  catch adapters that hardcode `44100`. On the mock path the rate comes
  straight from the returned `SynthesizeResult`.
- **Test 5** injects `upstream_failure`, so the mock raises inside
  `synthesize()`. The adapter must **not** catch-and-swallow or re-wrap
  with a different status — `502` must survive to the caller.
- **Test 6** uses a fresh mock (usage 0). An empty string is a valid
  call; returning the mock's canned result also passes, but see §5 for
  the recommended short-circuit.
- **Test 7** is the load-bearing behavioural test. The seeded usage is
  `9990` and the message is 45 chars, so `9990 + 45 = 10035 > 10000` — the
  adapter must fail-fast **before** the mock is called. The message check
  is case-insensitive and accepts either "quota" or "limit".

## 5. Behavioural edge cases & ordering notes

These are the subtle points implementers get wrong:

1. **Quota check runs before mock delegation.** Test 7 seeds
   `monthly_chars_used = 9990`; if the adapter delegated first, the mock
   would succeed and merely increment usage — the test would fail. The
   current skeleton (`adapter.py`) already calls `_check_quota(text, mock=mock)`
   *before* `await mock.synthesize(...)`. Keep that order.

2. **Empty-text vs quota ordering.** Test 6 uses a fresh mock (usage 0),
   so either order passes there. Recommended: short-circuit empty text
   **before** the quota check so a 0-char call never touches quota state.
   This matches the real-path empty-text guard already in the skeleton.

3. **Do not wrap or re-status errors on the mock path.** The mock raises
   `TTSError` with the correct status already; the adapter just lets it
   bubble. Re-wrapping (e.g. forcing `status=500`) breaks test 5.

4. **`text_len` is the total original length.** Even when the real path
   chunks text > 5000 chars across multiple upstream calls, the value
   recorded/checked is the full `len(text)` (test 3; `tasks.md` §6.4.7).
   Quota is likewise checked against total `len(text)` before chunking.

5. **Quota message wording.** Must contain "quota" or "limit"
   (case-insensitive). The reference message is
   `"monthly quota limit exceeded"` — it satisfies both substrings.

## 6. Real-API reference (for the non-mock path)

Cross-reference: `glc/voice/tts/providers/elevenlabs/README.md`. The 7
tests do not exercise this path, but the adapter must behave consistently
against the live API.

| Property | Value |
|---|---|
| Endpoint | `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` |
| Auth header | `xi-api-key: <KEY>` — **not** `Authorization: Bearer` |
| Request body | `{"text": "...", "model_id": "eleven_flash_v2_5"}` |
| Response body | Raw MP3 bytes → `base64.b64encode(resp.content).decode("ascii")` |
| `mime` | `"audio/mpeg"` |
| `sample_rate` | `44100` (default MP3 output, 44.1 kHz) |
| Default `voice_id` | `21m00Tcm4TlvDq8ikWAM` (Rachel) |
| Free-tier limit | 10,000 chars/month |
| Per-request limit | ~5,000 chars — chunk longer text on sentence boundaries before sending |
| HTTP client | `httpx` (already in `pyproject.toml`) |

Real-path error mapping (for parity with test 5):

- `httpx.HTTPStatusError` → `TTSError(str(e), status=e.response.status_code)`
- `httpx.RequestError` (network) → `TTSError(str(e), status=503)`

Real-path quota state is persisted to `~/.glc/elevenlabs_quota.json` keyed
by `YYYY-MM`; the counter is incremented after a successful upstream call.

## 7. Traceability

- These behaviours match the Implementation Contract in `tasks.md` §6.1
  and the architectural decisions in §6.4.
- This document is the deliverable for the **Test & Mock Analyst** role
  (briefing.md §5, Member 2; `tasks.md` "Nawaz — Phase A"): a written
  spec mapping each test to exact adapter behaviour, plus the mock's
  quota/usage attribute documentation.
