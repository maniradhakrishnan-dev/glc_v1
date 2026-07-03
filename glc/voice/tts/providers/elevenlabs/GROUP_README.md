# Group ElevenLabs — Implementation README

**Slot:** `elevenlabs` · **Route:** `POST /v1/speak` with `prefer=quality`  
**Team:** Hari Prasath (Batflash5), Nawaz Ali (nzbeta24
), Anshul Agarwal (eranshulbly), Abhinav Gupta (kill007az)

---

## 1. Architecture

### How the provider fits into GLC v1

```
HTTP Client
    │
    ▼
POST /v1/speak  {"text": "...", "prefer": "quality"}
    │
    ▼
glc/routes/speak.py          — validates request, calls synthesize()
    │
    ▼
glc/voice/tts/router.py      — maps prefer="quality" → "elevenlabs"
    │                           dynamically imports adapter.py
    ▼
glc/voice/tts/providers/elevenlabs/adapter.py   ← THIS FILE
    │
    ├── _check_quota()        — pre-flight monthly char limit check
    ├── _chunk_text()         — splits long text on sentence boundaries
    ├── _call_upstream()      — POST to api.elevenlabs.io, returns raw MP3 bytes
    └── _persist_real_quota() — increments ~/.glc/elevenlabs_quota.json after success
    │
    ▼
ElevenLabs Flash v2.5 API   →   raw MP3 bytes
    │
    ▼
base64-encode → SynthesizeResult(audio_b64, mime, sample_rate, provider)
    │
    ▼
HTTP response  {"audio_b64": "...", "mime": "audio/mpeg", "sample_rate": 44100, ...}
```

### Files owned by this group

```
glc/voice/tts/providers/elevenlabs/
├── adapter.py          — Provider class: synthesize(), _check_quota(),
│                         _call_upstream(), _chunk_text(), _persist_real_quota()
├── schemas.py          — Pydantic types: ElevenLabsRequest, ElevenLabsVoiceSettings
├── __init__.py         — package marker
├── README.md           — maintainer spec (do not modify)
├── GROUP_README.md     — this file
└── tests/
    └── test_elevenlabs_live.py  — live API test (skipped in CI)
```

### Key types (from `glc/voice/tts/base.py`)

```python
@dataclass
class SynthesizeResult:
    audio_b64: str    # base64-encoded MP3
    mime: str         # "audio/mpeg"
    sample_rate: int  # 44100
    provider: str     # "elevenlabs"
    cost_usd: float   # 0.0 (free tier)

class TTSError(Exception):
    status: int | None  # HTTP status code to return to the caller
```

---

## 2. Channel Quirks

These are ElevenLabs-specific behaviours that differ from what a generic HTTP API adapter would expect.

### Quirk 1 — Non-standard authentication header

Most APIs use `Authorization: Bearer <token>`. ElevenLabs uses a **custom header**:

```
xi-api-key: <ELEVENLABS_API_KEY>
```

Using the standard Bearer scheme returns a `401`. The adapter sets this explicitly in `_call_upstream()`:

```python
headers = {"xi-api-key": self._api_key}
```

### Quirk 2 — Response is raw MP3 bytes, not JSON

The ElevenLabs TTS endpoint returns **raw binary MP3 content** directly in the response body. There is no JSON wrapper, no base64 field — just bytes. The adapter reads `response.content` and base64-encodes it to fit the `SynthesizeResult` contract:

```python
audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
```

### Quirk 3 — Free-tier silent truncation at 5,000 chars per request

The free tier silently truncates input above ~5,000 characters per request without returning an error. The adapter splits text on sentence boundaries (`.`, `?`, `!`) before sending, then concatenates the raw MP3 bytes from all chunks before the single base64 encode:

```python
chunks = self._chunk_text(text)          # ≤ 5000 chars each
audio_bytes = b""
for chunk in chunks:
    audio_bytes += await self._call_upstream(chunk, voice_id)
# single encode on combined bytes
audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
```

A single sentence longer than 5,000 chars is sent unsplit (no mid-word cutting).

### Quirk 4 — Monthly character quota with fail-fast enforcement

The free tier allows 10,000 characters per calendar month. Rather than waiting for the upstream to return a 401-style rejection (which wastes a network round-trip), the adapter enforces this **pre-flight** via `_check_quota()`:

- **Mock path:** reads `mock.monthly_chars_used` and `mock.monthly_chars_limit` directly
- **Real path:** reads `~/.glc/elevenlabs_quota.json` keyed by `YYYY-MM`; writes the updated count after a successful call via `_persist_real_quota()`

```python
if used + len(text) > limit:
    raise TTSError("monthly quota limit exceeded", status=429)
```

The check uses total `len(text)` — not per-chunk length — so chunking cannot be used to game the counter.

### Quirk 5 — Empty text must not reach the API

Sending an empty string to the ElevenLabs API returns an error. The adapter short-circuits on the real path before any quota check or HTTP call:

```python
if not text:
    return SynthesizeResult(audio_b64="", mime="audio/mpeg",
                            sample_rate=44100, provider="elevenlabs")
```

On the mock path, the mock's `synthesize("")` is tolerant and returns a valid result, so no guard is needed there.

---

## 3. How the Tests Exercise the Trust-Level Boundary

In GLC v1, every inbound message passes through `glc/security/trust_level.py` which classifies the caller as `owner_paired`, `user_paired`, or `untrusted`. The TTS layer sits downstream of this gate — a request only reaches `adapter.py` if it has already passed the channel-level trust check.

Within the adapter itself, the quota pre-flight check acts as a **second trust boundary** specific to the ElevenLabs provider. The 7 tests collectively exercise both the structural contract and this boundary in the following way:

### Structural boundary tests (tests 1–5)

| Test | Boundary exercised |
|---|---|
| `test_provider_name_matches` | Identity check — confirms the router will bind to the correct class |
| `test_synthesize_returns_synthesize_result` | Output contract — the adapter must return a fully-populated `SynthesizeResult`; raw upstream data must never leak to the caller |
| `test_synthesize_passes_text_to_upstream` | Input fidelity — the total `text_len` recorded must equal the original input, not a per-chunk value, preventing quota manipulation via chunking |
| `test_synthesize_records_sample_rate` | Upstream value propagation — the adapter must not hard-code the sample rate; it must honour what the upstream (or mock) provides |
| `test_synthesize_propagates_upstream_error` | Error boundary — a 502 from upstream must not surface as a raw `httpx` exception; it must be translated to a structured `TTSError(status=502)`. This isolates callers from internal HTTP implementation details. |

### Trust-level boundary test (test 6)

| Test | Boundary exercised |
|---|---|
| `test_synthesize_handles_empty_text` | Input validation boundary — the adapter must accept zero-length input without crashing and return a valid (empty-audio) result instead of propagating an API error |

### Quota gate test (test 7 — the channel-specific behaviour test)

```python
mock.monthly_chars_used = 9_990
mock.monthly_chars_limit = 10_000
await adapter.synthesize("this is a long enough message to bust the cap")
# ↑ 46 chars; 9990 + 46 = 10036 > 10000 → must raise TTSError(status=429)
```

This test mirrors how the trust classifier blocks `untrusted` callers before any channel resource is consumed. Here, `_check_quota()` blocks the request **before** it touches the upstream API:

```
Request arrives
    │
    ▼
_check_quota()  ← quota gate (analogous to trust-level gate in the channel layer)
    │
    ├── used + len(text) > limit  →  TTSError(status=429)  ← blocked here
    │
    └── within quota              →  proceed to _call_upstream()
```

The test asserts:
- `TTSError` is raised (not a 200 with partial audio)
- `status == 429` (not a generic 500 or raw upstream error)
- The message contains `"quota"` or `"limit"` (structured, human-readable rejection)
- The mock's `received_calls` is **not** appended — confirming the upstream was never contacted

---

## 4. Running the Tests

```bash
# All 7 CI tests (no credentials needed — mock only)
uv run pytest tests/voice/tts/test_elevenlabs.py -v

# Live API test (requires ELEVENLABS_API_KEY)
uv run pytest glc/voice/tts/providers/elevenlabs/tests/ -m requires_live_api -v

# Lint + types
uv run ruff check glc/voice/tts/providers/elevenlabs/
uv run mypy glc/voice/tts/providers/elevenlabs/
```

## 5. Environment Variables

| Variable | Required | Default |
|---|---|---|
| `ELEVENLABS_API_KEY` | For real API only | — (mock tests need none) |
| `ELEVENLABS_VOICE_ID` | No | `21m00Tcm4TlvDq8ikWAM` (Rachel) |

Create `.env` at one level above `glc_v1/` — the server loads it automatically on startup.
