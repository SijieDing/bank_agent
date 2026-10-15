# Validation history

## Original workflow change (2026-10-14, 8061903)

The original in-process mock passed 20 new workflow tests and 54 focused tests.
Full discovery had the same 23 failures and 1 error as unchanged main.
Those results apply only to the original commit.

## REST backend refactor (2026-10-15)

The current change replaces in-process banking storage with a REST service and
separate bank SQLite database. Workflow and gateway tests now use real loopback
HTTP servers and temporary databases. API tests cover server-side ownership,
session revocation, concurrent debit, persistent records, seed idempotence,
database separation, and the Pi bridge.

The local execution environment was unavailable. Previous passing counts are
not validation of this refactor. A GitHub Actions job compiles Python and runs:

    python -m unittest test_bank_api test_transfer_workflow test_gateway test_authentication test_fast_path -v

See branch Actions results for execution status. Live model behavior has not
been validated. Full discovery retains the pre-existing model-dependent/obsolete
router and Pi expectations described above.

### Verified REST result

GitHub Actions run [38747669971](https://github.com/SijieDing/bank_agent/actions/runs/38747669971)
passed for code commit `5db63451b4f07885aec1caa6f4c9b83fe5eecae2`:
Python compilation succeeded and all **63 focused tests passed**. This includes
legacy-data migration and unknown submission outcomes. Full legacy discovery was
not re-run for this refactor; the historical baseline above is not a current result.
