# Gates: photo-informed agent discovery and evidence

OWNS: src/hwlog/**, tests/**, docs/**, README.md, GATES.md

Scope: Apply the supplied MHS presentation's discovery, explicit capability, and observation-loop ideas to hwlog's existing serial capture interface.

- [x] G1: CLI and MCP expose the same capability manifest, with truthful write policy and limits.
  CHECK: uv run pytest -q tests/test_discovery.py
  EXPECT: passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/gurucharan/Documents/personal/hardware-logging; path=34f446375605/21 entries; output=................                                                         [100%] | 16 passed in 0.30s

- [x] G2: Session summaries measure recorded evidence, retain recent faults, and expose incomplete scans/storage loss within bounded output.
  CHECK: uv run pytest -q tests/test_summary.py
  EXPECT: passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/gurucharan/Documents/personal/hardware-logging; path=34f446375605/21 entries; output=..............                                                           [100%] | 14 passed in 0.37s

- [x] G3: Existing behavior and repository CI checks pass.
  CHECK: uv run ruff check . && uv run ruff format --check . && uv run pytest -q && uv build --no-sources --no-build-isolation
  EXPECT: Successfully built
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/gurucharan/Documents/personal/hardware-logging; path=34f446375605/21 entries; output=Successfully built dist/hardware_logging-0.1.0.tar.gz | Successfully built dist/hardware_logging-0.1.0-py3-none-any.whl

- [x] G4: Documentation and installed agent guidance explain the implemented workflow and distinguish photo-derived ideas from protocol compatibility.
  EVIDENCE: Reviewed README.md, docs/{cli,mcp,architecture,agent-workflow,mhs-design-notes}.md and src/hwlog/assets/SKILL.md against implemented commands and tests. Source notes account for all 27 supplied photos. Docs state AND/exact filter semantics, explicit session selection, 16 MiB scan/32 KiB JSON limits, eight recent faults, twenty tracked tags, partial/corrupt/storage-loss coverage, raw-write opt-in, device safety boundaries and no MHS compatibility claim. No physical-device validation was performed; serial discovery and writes are mocked in integration tests.
