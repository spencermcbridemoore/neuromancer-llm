"""vLLM adapter — pin --revision/--tokenizer-revision, capture the server identity row at start
(revision/dtype/quant are NOT on the wire), logprob_token_ids (<=128) fits MCQ letters. STAGE 2.
"""

from __future__ import annotations
