### ADR-0012 — Registration hashing of cloud-bound shards (condition 1)
**Status:** Accepted. **Decision.** Every cloud-bound shard is sha256-hashed **at registration** (seconds per bundle). Hash deferral is permitted only for local dense shards under ADR-0008. **Consequences.** The registration transaction is the durability boundary for cloud artifacts.
