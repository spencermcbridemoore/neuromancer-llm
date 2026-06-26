"""vLLM adapter — HTTP client to the containerized OpenAI-compatible server.

Pin --revision/--tokenizer-revision at launch and capture the server identity row at start
(revision/dtype/quant are NOT on the wire), `logprob_token_ids` (<=128) fits MCQ letters. STAGE 2.

This drives the next-token logprob distribution: a `max_tokens=1`, `temperature=0` completion with
top-k logprobs, returned as an *id-keyed* map (the server is launched with
`--return-tokens-as-token-ids` so the OpenAI `top_logprobs` keys carry token ids, not ambiguous
decoded strings). That id-keyed vector is the object the E6 gate exact-matches across batch sizes and
engine restarts (capture/determinism.py).

vLLM is NOT a package dependency (it lives in the container). This adapter talks to the server over
HTTP using only the Python standard library (urllib + json), so
`import neuromancer_llm.capture.adapters.vllm` never needs vLLM — and never needs any optional client
either; the "lazy/optional import" guarantee is satisfied trivially by depending on nothing.

DEVIATION recorded (not drift): ADR-0033's nominal dense lane is 8-9B single-layer. This slice refines
to a *7B* model because the 4090 is the shared DISPLAY GPU (~4 GB wobbling baseline) leaving ~16 GB,
which rules out 8B bf16 with margin (`--gpu-memory-utilization 0.65`). The specific 7B pick
(Mistral-7B-v0.3 default / Qwen2.5-7B equally valid) does NOT affect E6's determinism verdict.
"""

from __future__ import annotations

import json
import struct
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# A token-id map fits the MCQ-letter workload exactly; the capture contract caps it at <=128 ids.
MAX_LOGPROB_TOKEN_IDS = 128
_TOKEN_ID_PREFIX = "token_id:"


class VLLMAdapterError(RuntimeError):
    """Loud, typed failure talking to the vLLM server (no silent gap, per the binding constraints)."""


class ServerNotReadyError(VLLMAdapterError):
    """The server did not become ready within the deadline (weights still loading, or not launched)."""


@dataclass(frozen=True)
class ServerIdentity:
    """The identity facts the substrate/semantic fingerprint needs POST-gate.

    `served_model` is read from the wire (/v1/models); `revision`/`dtype`/`quant`/`batch_invariant`
    are launch-time facts that are NOT on the wire (the capture contract §2 note) and so are passed in
    by the launcher. Wiring this into `fingerprints.semantic_config` is a post-gate step — recorded
    here only so the E6 report can quote the exact substrate it measured.
    """

    base_url: str
    served_model: str
    revision: str | None = None
    dtype: str | None = None
    quant: str | None = None
    batch_invariant: bool | None = None


@dataclass(frozen=True)
class LogprobSample:
    """One next-token logprob distribution, id-keyed — the unit E6 exact-matches.

    `top_logprobs` is a sorted (token_id, logprob) tuple so equality is order-independent. Bitwise
    equality is decided on the IEEE-754 bytes of each logprob (see `signature`), not float `==` — that
    is the "EXACT-match (bitwise)" the E6 spec demands (catches a -0.0/0.0 or denormal divergence that
    `==` would hide).
    """

    prompt: str
    generated_token_id: int
    top_logprobs: tuple[tuple[int, float], ...]
    batch_size: int = 1
    phase: str = "pre-restart"
    replicate_index: int = -1

    @property
    def token_ids(self) -> tuple[int, ...]:
        return tuple(tid for tid, _ in self.top_logprobs)

    @property
    def logprob_map(self) -> dict[int, float]:
        return {tid: lp for tid, lp in self.top_logprobs}

    def signature(self) -> tuple[tuple[int, bytes], ...]:
        """The bit-exact identity of this distribution: each logprob as its 8 raw IEEE-754 bytes."""
        return tuple((tid, struct.pack("<d", lp)) for tid, lp in self.top_logprobs)


@dataclass(frozen=True)
class CapturedLogprobs:
    """A next-token logprob pass WITH its verbatim wire payload (the true bytes, never reconstructed).

    `request_body` is the EXACT JSON bytes POSTed to /v1/completions; `response_body` is the EXACT bytes
    the server returned. `sample` is the parsed distribution DERIVED from `response_body` — a convenience
    over the bytes, never a substitute for them (the capture lane stores the bytes immutably and computes
    the parquet array from them; the predecessor sin was storing a reconstructed request + {"text"})."""

    sample: LogprobSample
    request_body: bytes
    response_body: bytes
    http_status: int
    content_type: str


def _parse_token_id(key: str) -> int:
    """Parse a vLLM `token_id:NNN` key. Raise loudly if the server was not launched id-keyed."""
    if not key.startswith(_TOKEN_ID_PREFIX):
        raise VLLMAdapterError(
            f"logprob key {key!r} is not id-keyed; launch the server with "
            "--return-tokens-as-token-ids so logprobs carry token ids (not ambiguous decoded strings)."
        )
    try:
        return int(key[len(_TOKEN_ID_PREFIX) :])
    except ValueError as exc:  # pragma: no cover - defensive
        raise VLLMAdapterError(f"malformed token-id key {key!r}") from exc


class VLLMClient:
    """Thin stdlib HTTP client for the containerized OpenAI-compatible vLLM server."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # --- transport ---------------------------------------------------------------------------------
    def _get(self, path: str, *, timeout: float | None = None) -> object:
        req = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise VLLMAdapterError(f"GET {path} failed: {exc}") from exc

    def _post_capture(
        self, path: str, payload: dict[str, object], *, timeout: float | None = None
    ) -> tuple[object, bytes, bytes, int, str]:
        """POST returning (parsed, VERBATIM request bytes, VERBATIM response bytes, http status, content-type).

        The request bytes are the EXACT JSON body sent; the response bytes are the EXACT bytes received.
        This is the true wire payload the capture lane stores immutably — never reconstructed (ADR-0003;
        the predecessor bypassed raw capture and stored a rebuilt request + a {"text"} response)."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:  # noqa: S310
                raw = resp.read()
                status = int(resp.status)
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:  # surface the server's error body, don't swallow it
            detail = exc.read().decode("utf-8", "replace")
            raise VLLMAdapterError(f"POST {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise VLLMAdapterError(f"POST {path} failed: {exc}") from exc
        return json.loads(raw.decode("utf-8")), body, raw, status, content_type

    def _post(self, path: str, payload: dict[str, object], *, timeout: float | None = None) -> object:
        parsed, _req, _resp, _status, _ctype = self._post_capture(path, payload, timeout=timeout)
        return parsed

    # --- readiness + identity ----------------------------------------------------------------------
    def is_ready(self) -> bool:
        """True once /health returns 200 (vLLM signals readiness only after weights are loaded)."""
        req = urllib.request.Request(self.base_url + "/health", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:  # noqa: S310
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def wait_until_ready(self, *, deadline_s: float, poll_s: float = 3.0, _sleep=None) -> None:
        """Poll /health until ready or raise ServerNotReadyError at the deadline (fail loud)."""
        import time

        sleep = _sleep or time.sleep
        start = time.monotonic()
        while time.monotonic() - start < deadline_s:
            if self.is_ready():
                return
            sleep(poll_s)
        raise ServerNotReadyError(
            f"vLLM server at {self.base_url} not ready after {deadline_s:.0f}s "
            "(weights still loading, OOM at init, or not launched)."
        )

    def served_model(self) -> str:
        """The model id the server actually serves (the OpenAI `model` field must match it)."""
        data = self._get("/v1/models")
        if not isinstance(data, dict) or not data.get("data"):
            raise VLLMAdapterError(f"/v1/models returned no models: {data!r}")
        return str(data["data"][0]["id"])

    def server_identity(
        self,
        *,
        revision: str | None = None,
        dtype: str | None = None,
        quant: str | None = None,
        batch_invariant: bool | None = None,
    ) -> ServerIdentity:
        return ServerIdentity(
            base_url=self.base_url,
            served_model=self.served_model(),
            revision=revision,
            dtype=dtype,
            quant=quant,
            batch_invariant=batch_invariant,
        )

    # --- the next-token logprob primitive ----------------------------------------------------------
    @staticmethod
    def _completion_payload(prompt: str, *, model: str, n_logprobs: int, seed: int) -> dict[str, object]:
        """The exact greedy/max_tokens=1 completion request body (one builder, so the captured wire
        payload and the E6 request are byte-for-byte the same shape)."""
        if not 1 <= n_logprobs <= MAX_LOGPROB_TOKEN_IDS:
            raise VLLMAdapterError(f"n_logprobs={n_logprobs} out of range [1, {MAX_LOGPROB_TOKEN_IDS}]")
        return {
            "model": model,
            "prompt": prompt,
            "max_tokens": 1,
            "temperature": 0,  # greedy: argmax, no sampling RNG (declared_mode = greedy)
            "seed": seed,
            "logprobs": n_logprobs,
            "echo": False,
        }

    def next_token_logprobs(
        self,
        prompt: str,
        *,
        model: str,
        n_logprobs: int,
        seed: int,
        batch_size: int = 1,
        phase: str = "pre-restart",
        replicate_index: int = -1,
    ) -> LogprobSample:
        """Greedy (temperature=0), max_tokens=1 completion returning the top-`n_logprobs` distribution.

        `--logprobs-mode raw_logprobs` (set at launch) makes these the raw log-softmax values, the
        right quantity to exact-match. The server must also be launched `--return-tokens-as-token-ids`
        and with `--max-logprobs >= n_logprobs`.
        """
        payload = self._completion_payload(prompt, model=model, n_logprobs=n_logprobs, seed=seed)
        data = self._post("/v1/completions", payload)
        return self._parse_completion(data, prompt, batch_size, phase, replicate_index)

    def next_token_logprobs_capture(
        self, prompt: str, *, model: str, n_logprobs: int, seed: int
    ) -> CapturedLogprobs:
        """The capture-lane primitive: one greedy next-token logprob pass returning the parsed
        distribution ALONGSIDE the verbatim request/response wire bytes (ADR-0003). The parsed `sample`
        is DERIVED from `response_body`; the bytes are the authority the capture row stores immutably."""
        payload = self._completion_payload(prompt, model=model, n_logprobs=n_logprobs, seed=seed)
        parsed, req_bytes, resp_bytes, status, ctype = self._post_capture("/v1/completions", payload)
        sample = self._parse_completion(parsed, prompt, 1, "capture", -1)
        return CapturedLogprobs(
            sample=sample,
            request_body=req_bytes,
            response_body=resp_bytes,
            http_status=status,
            content_type=ctype,
        )

    @staticmethod
    def _parse_completion(
        data: object, prompt: str, batch_size: int, phase: str, replicate_index: int
    ) -> LogprobSample:
        if not isinstance(data, dict) or not data.get("choices"):
            raise VLLMAdapterError(f"completion returned no choices: {data!r}")
        choice = data["choices"][0]
        lp = choice.get("logprobs")
        if not lp or not lp.get("top_logprobs") or not lp.get("tokens"):
            # Requested-but-missing logprobs/tokens are a recorded failure, never a silent gap (capture
            # contract §2). For E6 this is a hard error: the gate cannot be evaluated without them. An EMPTY
            # tokens/top_logprobs list is included here so it raises VLLMAdapterError rather than a bare
            # IndexError on lp["tokens"][0] (Section-C: a typed loud error, not an opaque IndexError).
            raise VLLMAdapterError(
                "server returned no logprobs/tokens; launch with --logprobs-mode raw_logprobs and pass a "
                "logprobs count (and --max-logprobs high enough)."
            )
        generated = _parse_token_id(lp["tokens"][0])
        top = lp["top_logprobs"][0]
        pairs = sorted((_parse_token_id(k), float(v)) for k, v in top.items())
        return LogprobSample(
            prompt=prompt,
            generated_token_id=generated,
            top_logprobs=tuple(pairs),
            batch_size=batch_size,
            phase=phase,
            replicate_index=replicate_index,
        )

    def batched_next_token_logprobs(
        self,
        target_prompt: str,
        *,
        model: str,
        n_logprobs: int,
        seed: int,
        filler_prompts: list[str] | None = None,
        phase: str = "pre-restart",
        replicate_index: int = -1,
    ) -> LogprobSample:
        """Fire the target alongside `filler_prompts` concurrently so they co-batch in one forward pass.

        The realized batch size is `1 + len(filler_prompts)` — this is the knob E6 varies. Under
        `VLLM_BATCH_INVARIANT=1` the target's logprobs must be identical regardless of who it shares
        the batch with; with the flag unset they drift (the control arm proves the protocol exercises
        exactly this). Fillers are fired purely to perturb the batch shape; their results are discarded.
        """
        fillers = list(filler_prompts or [])
        batch_size = 1 + len(fillers)
        if batch_size == 1:
            return self.next_token_logprobs(
                target_prompt,
                model=model,
                n_logprobs=n_logprobs,
                seed=seed,
                batch_size=1,
                phase=phase,
                replicate_index=replicate_index,
            )
        with ThreadPoolExecutor(max_workers=batch_size) as pool:
            target_future = pool.submit(
                self.next_token_logprobs,
                target_prompt,
                model=model,
                n_logprobs=n_logprobs,
                seed=seed,
                batch_size=batch_size,
                phase=phase,
                replicate_index=replicate_index,
            )
            # Fillers: same shape of request, different content, longer/shorter to vary the batch's
            # token dimension. Results discarded; they exist only to occupy the forward batch.
            filler_futures = [
                pool.submit(self.next_token_logprobs, fp, model=model, n_logprobs=n_logprobs, seed=seed)
                for fp in fillers
            ]
            target = target_future.result()
            for fut in filler_futures:
                fut.result()  # surface any filler error loudly rather than hiding a half-batch
        return target
