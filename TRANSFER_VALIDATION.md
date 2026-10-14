# Transfer workflow validation

Validated on 2026-10-14 against base commit
`a593117324d3f876ee46cb6607f9dab1239b38b6`.

## Results

- 20 new transfer tests pass, without model/provider calls.
- `python -m unittest test_transfer_workflow test_fast_path test_gateway test_authentication -q`:
  **54 tests pass**.
- `python -m unittest discover -q`: 101 tests, **23 failures and 1 error**.
- The same discovery command in an isolated, untouched main checkout: 81 tests,
  **23 failures and 1 error**.
- Compared the failing test/subtest identifiers: they are identical. There are
  no additional failing cases introduced by this branch.
- `git diff --check` passes.

The legacy failures include tests expecting old router/tool-selection behavior
without mocking the model, and Pi tests expecting a `--thinking medium` flag and
printed thinking content absent from main. These unrelated production behaviors
and legacy tests were left unchanged.

## Covered behavior

Complete and incomplete requests; amount and recipient corrections; explicit
field clearing; confirmation invalidation; pause/query/resume; cancellation;
ambiguous and malformed model proposals; amount/email/currency checks; unknown
recipients; unauthorized accounts; insufficient funds; duplicate and concurrent
submissions; balance changes after confirmation; crash recovery before submission;
lost responses after successful debit; unknown outcomes; persistent balances;
owner-bound restoration; fresh sign-in; reset; second-task rejection; initial
workflow routing; and application integration.

## Limits of verification

The transfer parser was tested with structured mock responses. No live LM Studio
or Pi model was used, so real-language interpretation quality and end-to-end
model latency remain to be checked with the user's configured model.
All transfers are local mock operations, not real banking integrations. The
SQLite/file-lock implementation targets a single macOS/Linux host.
