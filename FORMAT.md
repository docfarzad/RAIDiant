# RAIDiant formats 1–3 / rs-gf256-v1

All offsets and coding parameters are versioned. This document records the current
implementation, not an interoperability promise with any other RAID product.

## Member layout

| Offset | Content |
| --- | --- |
| 0 | 4096-byte superblock A |
| 4096 | 4096-byte superblock B |
| 8192 | Metadata bank 0 |
| 8192 + bank_size | Metadata bank 1 |
| 8192 + 2 × bank_size | Fixed-size shard records |

Superblocks contain magic `RDIANT01`, a little-endian 32-bit JSON byte length,
SHA-256 of the UTF-8 JSON, and zero padding. Geometry includes the array UUID,
member index, total count, parity count, chunk size, maximum member size, metadata
bank size, and stripe count. The dynamic root identifies the active bank, epoch,
checkpoint byte length, sequence, and final hash.

The newest membership generation and valid checkpoint epoch are selected. Disagreement between checkpoints at the same
epoch is unsafe and rejected. Every selected member's root must point at the
selected checkpoint before normal writes resume, even when its alternate bank
already contains that checkpoint.

Format 1/2 members retain their exact, fully reserved length. Format 3 adds
`storage=dynamic`; `member_size` remains the maximum, and EOF initially covers
only the two superblocks and metadata banks. Bank sizing is unchanged: each is
1/32 of member maximum, aligned to 4096 bytes, bounded by 64 KiB and 512 MiB.
The data region grows in bounded batches. Old applications reject format 3.

Session-local expected EOFs detect external size changes. On reopening, a member
must contain the reserved prefix and not exceed its maximum. The recovered live
catalog then determines whether its data is truncated or an unused tail remains.
Either condition blocks normal writes until recovery or replacement succeeds.

## Coding and shard identity

`k = n − m`. A systematic Reed–Solomon matrix is constructed from a Vandermonde
matrix over GF(256), polynomial `0x11d`, and normalized to identity in its first
k rows. Column scaling and compensating data-row scaling make the first parity
row all ones. This preserves the MDS property: any k distinct valid shards
reconstruct all n shards. Known vectors are pinned in `tests/test_codec.py`.

Physical member for logical role r in stripe s is `(r + s) % n`. The first k
roles contain data and the remaining m contain parity. There are no dedicated
parity members.

Each shard record contains a 128-byte header followed by exactly chunk_size
bytes. Header fields are `<8s16sQI32s`: magic `RDSHR001`, file UUID bytes, stripe
number, logical role, and SHA-256 of the fields before the digest plus the data.
Padding fills the remainder of the header. A shard is accepted only when its
identity matches the expected live file allocation and its checksum verifies.
Free stripes are not readable file data and need no initial parity materialization.

## Metadata and commits

Each variable-length log record starts with `<8sQI32s32s`: `RDLOG001`, sequence,
JSON payload length, previous-record hash, and current-record hash. The latter is
SHA-256 of little-endian sequence + previous hash + payload. Records are at most
65536 payload bytes. A zero header terminates the valid log.

Events create entries, add extents to staging files, finish files with size and
SHA-256, rename entries, abandon imports, or delete subtrees. Object UUIDs and
parent UUIDs define hierarchy independently of names. File extents hold physical
start, count, and logical ordinal. They support fragmented allocation.

Data writes precede a flush of every active member. Only then are extent records
written and flushed to every member. The final file record publishes the entry.
If interrupted before final publication, its staging extents are reclaimed.
Full-stripe copy-on-write allocation avoids in-place data/parity write holes.
Existing committed extents are never overwritten until their deletion commits.

For dynamic members, an `allocate` event (file UUID, start, count, ordinal) is
replicated and flushed before physical extension. Extent publication clears its
intent. Checkpoints preserve pending intents and staging entries. Abandonment
and recovery durably remove incomplete allocations before truncating members
to the highest committed stripe. A crash during truncation is detected and
recovered on reopening. Interior free ranges are reused without hole punching.
Deletion may reclaim an unused tail by the same durable-metadata-first rule.

Allocation, write or flush failures disable mutations. If a finish record may
already have reached disk, abandonment first reloads the valid metadata chain;
it never removes a file whose finish survived. Cleanup failure preserves recovery
state for a later retry. Native allocation reserves only the added range; a
bounded zero-fill fallback touches the new tail, never committed prefix data.

Open validates checkpoints, then merges only identical records or strict prefixes.
A longer valid chain can represent a commit interrupted during replication; its
data was already flushed to every member before that record could be written.
Shorter or damaged replicas require synchronization before any mutation. Two
different valid records at the same chain position are refused, never majority-voted.

The local SQLite catalog is rebuilt from this log and is not part of the array.
It has an 8 MiB page cache; records and checkpoints are streamed through local
temporary files. Losing the local cache does not lose array metadata.

## Compaction

A complete catalog snapshot is first constructed and sized on temporary disk.
It is copied into the inactive metadata bank and flushed on every present member.
Only then is a new root written to the alternate superblock slot and flushed.
The previous bank/root remains a recovery option until the new root is published.
After partial publication, recovery synchronizes roots before further writes.
A log reserve (one eighth of the bank, at least 8 KiB) allows delete/abandon
records when ordinary metadata capacity is exhausted and leaves room for the
slightly larger snapshot representation of directory records.

## Rebuilds

Replacements initially contain a `state=rebuilding` header, source metadata hash
and epoch, and `rebuild_next` physical stripe position. They never count toward
the active member threshold. New output is flushed before progress headers are
updated. Resume validates identity and checksum of checkpointed output; a changed
source catalog restarts reconstruction. Source data remains unchanged; format and
membership headers on surviving members are updated during publication.

On completion, data is flushed, the canonical metadata is copied and flushed,
then ready superblocks are published and flushed. Only then is the replacement
admitted. A crash during final publication can leave some completed replacements
and some incomplete replacements: select completed ones when opening and resume
the others through Rebuild missing.

A ready target may be selected again in Rebuild when final roster publication
was interrupted. Its slot, geometry, positive membership generation, compatible
roster, checkpoint and complete actual metadata chain must match the survivors.
New targets retain source epoch/hash tags; older ready targets without those tags
are validated against their actual chain. Validation is repeated after acquiring
the target lock. Verified shard output is reused and publication resumes. An
unrelated ready original or divergent/newer history is preserved and refused.
Opening the ready replacement with the survivors also permits exclusive Repair
to synchronize a partially installed generation.

Format 2 adds `membership_epoch`, `member_roster` and `member_id`. Legacy member
IDs are deterministically derived from the array UUID and slot. A replacement
receives a new ID; every current member records the same roster. Retired originals
are isolated when a newer roster is available. Equal-generation conflicting rosters
are refused. Resuming an unfinished generation retains its pending IDs.

Before retirement, existing member headers are upgraded to version 2 so legacy
applications reject them. Both header copies are upgraded. Targets are complete
and whole-file verification has passed before ready headers are published, then
survivors receive the new roster. A partial publication requires recovery; a
completed target can provide the new roster. Each first new header is flushed
before replacing a fallback header. A completely isolated historical member set
has no external authority telling it that a newer generation exists.

Exports during reconstruction hold read leases on immutable catalog/source data.
Publication closes the lease gate and drains existing readers before replacing
handles or rebuilding the local index. Mutations remain exclusive.

## Local interruption records and verification

Before creation allocates large members, a locked local manifest records absolute
paths, random ownership tokens and filesystem identities. Member prefixes initially
contain `RDICREAT` ownership markers. After every allocation and initial catalog is
durable, a durable publishing phase precedes ready headers containing creation
tokens. Recovery can resume allocation/publication; safe deletion is allowed only
before publication. Cleanup has its own resumable phase. Changed files are refused.

Local disposable indexes have marked, locked scratch directories. Startup reclaims
only verifiably owned inactive directories. Export staging is similarly tracked;
only a flushed, checksum-verified file receives a final name, through an atomic
non-overwriting link or native exclusive rename. No partial-copy publication is used.

Full verification rereads headers/catalog and streams complete files through shard,
parity and whole-file SHA-256 checks. Unrecoverable stripes do not end the scan;
the disk-backed JSONL report includes every affected file and a bounded UI sample.
Free stripes have no defined data checksum. Verification does not test unused media
or prove physical hardware health. Repair cannot clear a whole-file checksum failure
merely by making parity agree.

## Scope of failure guarantees

The protocol relies on durable flushes, independent members, and exclusive
cooperating access. It does not solve arbitrary host filesystem damage,
firmware that lies about persistence, adversarial changes, or a collection of
stale copies that contains no evidence of newer commits. SHA-256 is an integrity
check here, not authentication. Detected ambiguous/unrecoverable damage is
preserved and reported instead of overwritten with guessed content.
