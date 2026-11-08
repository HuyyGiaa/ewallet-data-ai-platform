# EDAI-2 testing evidence

| Run | Code | Rows | Content |
|---|---|---|---|
| [`b1_281db72_20261106/`](b1_281db72_20261106/README.md) | `281db72` (B1 branch) | 28–31 | The runs shown in the testing screenshots |
| `g2_mutation_20261107T101557Z/`, `g2_mutation_20261107T102329Z/` | `cceb274` (integration, G2) | 30 | Mutation on the integrated code: 1,107 mutants, actionable 90.52%, then 91.44% after equivalent review |
| `head_coverage_20261108T082044Z/` | `d388bd9` (integration HEAD) | 28 | Coverage of the current code: 256 passed, 91.90% (1,643 statements) |
| `head_eq_boundary_20261108T082124Z/` | `d388bd9` | 29 | The same four boundary modules: 70 passed, 77 subtests |
| `head_property_20261108T082124Z/` | `d388bd9` | 31 | Hypothesis `b1-ci`: 566 passed, 9 xfailed |

Every run folder has a `provenance.txt` with the commit, branch, time and `uv.lock` hash. Generated `.coverage` files and HTML reports are git-ignored.
