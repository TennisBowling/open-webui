Dev-only tools (not shipped):
- e2e_refactor.py — drives real generations through the full turn pipeline against a throwaway uvicorn :8083 (start it from backend/ with DATABASE_URL set, then run each scenario: s1 plain, s2 tool round, s3 stop). Asserts persisted-state invariants.
- capture_map.py — symtable-based exact closure-capture analysis for any Python file; used to verify middleware extractions capture nothing.
