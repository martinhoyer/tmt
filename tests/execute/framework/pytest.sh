#!/bin/bash
. ../../utils.sh

# Plan name can be overriden by the first argument
PLAN=${1:-smoke}

rlPhaseStartTest $PLAN
    rlRun "tmt run -av provision -h container execute -h tmt discover -h fmf --filter tag:pytest-smoke --force plans --name $PLAN"
    # Add specific checks for pass, fail, info results based on the test cases
    # This requires knowing the exact output format or how tmt stores results.
    # For now, we'll rely on tmt's exit code or summary.
    # Example checks (these will need refinement):
    # Check for passing test
    rlAssertGrep "1 test passed" $rlRun_LOG
    # Check for failing test
    rlAssertGrep "1 test failed" $rlRun_LOG # Or however tmt reports this for pytest
    # Check for no tests found (info)
    rlAssertGrep "1 test info" $rlRun_LOG # Or however tmt reports this

    # A more robust way would be to inspect the results.yaml or use tmt report
rlPhaseEnd
