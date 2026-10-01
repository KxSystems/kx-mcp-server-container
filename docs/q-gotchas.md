# q dictionary gotchas

These traps affect identity storage and promotion in `kx.auth`.

- **Conforming dictionary values form a keyed table.** Joining a record with fewer keys can merge it into the old record, retaining stale identity fields such as `tenant`; different key sets can raise `'mismatch`. Assignment with `@[d;k;:;v]` or `d[k]:v` also merges. Store each principal as a one-row table (`enlist enlist p`) and read it with `first`, keeping the outer values general. Guard the invariant with `0h = type value store`.
- **Uniform typed dictionary values reject vector assignment.** For example, ``p:`sub`iss!(`a;`b)`` has symbol-typed values, so ``p[`groups]:`x`y`` fails with `'type`. Use a dictionary join, ``p,(enlist `groups)!enlist v``, to widen the values before assignment. Adding and removing padding is insufficient: q re-types the uniform remainder.

Regressions: [principal rebinding](../tests/deterministic/integration/kx_auth_rebind.q) and [identity promotion](../tests/deterministic/integration/kx_auth_assertion_gate.q).
