# LLM Streaming Guardrail Gateway

A FastAPI gateway that forwards a text-generation request to an LLM provider, streams the response back, and redacts emails, SSNs and credit card numbers as `[REDACTED]` in real time.

## Requirements

Python >= 3.10 (the shared repo venv needs 3.11 because of Task 4)

## Setup

Follow [Setup](../README.md#setup) in the root README, then `cd task3_stream_guardrail`.

## Project layout

```text
.
├── gateway.py          # Endpoint, SSE parsing, error handling
├── redactor.py         # Streaming PII redaction
├── mock_provider.py    # Mock LLM provider with PII split across chunks
├── README.md
└── tests/
```

## Run

Use two terminals, both in this folder with the venv active:

```bash
uvicorn mock_provider:app --port 9100
uvicorn gateway:app --port 8000
```

`PROVIDER_URL` defaults to `http://127.0.0.1:9100/v1/completions`. Set the environment variable to override it.

## Try it

```bash
curl -N -X POST localhost:8000/v1/generate -H 'Content-Type: application/json' -d '{"prompt":"hi"}'
```

Sample output (shortened):

```text
data: {"text": "Contact "}

data: {"text": "[REDACTED] for support. "}

data: {"text": "SSN on file: "}

data: {"text": "[REDACTED]. "}

data: [DONE]
```

## Output format

- Success: `data: {"text": ...}` events, then `data: [DONE]`.
- Mid-stream failure: held text is discarded, and the stream ends with `event: error` and a generic message. There is no `[DONE]`.
- Failure before streaming starts: a JSON error with 502 (provider unavailable or invalid response) or 504 (timeout).
- Invalid request body: 422.

## How streaming redaction works

- Only the trailing word or digit run that could still become PII is held back. Everything else is released immediately.
- Hold-back is capped at 330 characters for emails and 23 for digit patterns, so memory stays bounded.
- Cards must pass a Luhn check.
- Values that look cut off at the end of the stream are redacted.
- Provider events are capped at 64 KB.

## Detected formats

- Emails.
- SSNs: 3-2-4 digits with matching `-` or space separators.
- Cards: 13–19 digits that pass Luhn, either unseparated or grouped with `-` or spaces as 4-4-4-(1–7), 4-4-4-4-3 or 4-6-(4–5).

## Known limitations

- Unformatted 9-digit SSNs are not redacted, to avoid false positives on order numbers and IDs.
- Card separators other than space or hyphen (for example dots or non-breaking spaces) and non-ASCII digits are not detected.
- Only the completions format (`choices[0].text`) is parsed, not chat format (`delta.content`).
- No provider API key is forwarded, since the provider is a mock.
- Card numbers must pass the Luhn check, so common placeholder numbers such as `1234 5678 9012 3456` are intentionally not redacted, while real-format test cards such as `4111 1111 1111 1111` are.
- Emails with non-ASCII characters (for example `jöhn@exämple.com`) are not detected.

## Tests

```bash
pytest -v tests/
```
