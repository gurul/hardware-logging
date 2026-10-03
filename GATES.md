# Gates: README and code improvements

OWNS: src/hwlog/**, tests/**, docs/**, README.md, pyproject.toml, GATES.md

Scope: Improve evidenced code defects and onboarding documentation, verify the changes, and publish them to main.

- [x] G1: Query regressions preserve boot/lifecycle evidence, avoid input mutation, and recover replaced logs in CLI/MCP workflows.
  CHECK: uv run pytest -q tests/test_query.py tests/test_summary.py tests/test_discovery.py
  EXPECT: passed
  EVIDENCE: exit=0; EXPECT=matched; query/summary/discovery regression run passed. Full-suite JUnit independently records 22 query, 14 summary and 16 discovery tests with zero failures, errors or skips (52 total).

- [x] G2: README and affected documentation match the implemented installation, commands, and limitations.
  EVIDENCE: Manually reviewed README.md and docs/{cli,mcp,agent-workflow}.md against cli.py, mcp_server.py, records.py, query.py, summary.py and storage defaults. Isolated `uv tool install .` succeeded in /tmp/hwlog-readme-install.7EPQkQ; its `hwlog version` returned 0.1.0, `describe` returned schema version 1 with the documented limits, and `init` installed the packaged skill and printed the snippet. Local Markdown links resolve. Examples target both capture and flasher, explain session selection and wait exit/boundary behavior, and distinguish observed output from hardware health. No physical-board validation was performed.

- [x] G3: Repository lint, formatting, tests, and distribution build pass.
  CHECK: uv run ruff check . && uv run ruff format --check . && uv run pytest -q --junitxml=.pytest_cache/results.xml && uv build --no-sources --no-build-isolation
  EXPECT: Successfully built
  EVIDENCE: exit=0 for lint, formatting, full suite and build; JUnit measured 226 tests, 0 failures, 0 errors and 0 skips. Successfully built dist/hardware_logging-0.1.0.tar.gz and dist/hardware_logging-0.1.0-py3-none-any.whl; rebuilt after the final workflow-guide edit.

- [x] G4: Research notes cite verified primary papers and map findings to implemented behavior with explicit limitations.
  EVIDENCE: Independently reviewed the research leaf and primary full-text passages: Dapper sections 2.1/4.4, GTSO sections 2/4, Log20 sections 1/7, and Observing a Moving Target sections III/IV. Verified metadata, identity/sampling, clock-order, profile-coverage and transport/energy claims. docs/research-notes.md gives findings, engineering applications and limits for all four papers; README, CLI and MCP docs link it. Parent `--reverify --root . --cwd .` reran the research structure check successfully (3 research gates met). No claim that the papers validate hwlog performance or hardware correctness.

- [x] G5: Source and wheel distributions retain package code and agent assets while excluding local research downloads and verification state.
  CHECK: uv run python -c 'from pathlib import Path; import tarfile,zipfile; a=tarfile.open("dist/hardware_logging-0.1.0.tar.gz"); names=a.getnames(); prefix="hardware_logging-0.1.0/"; bad=[p for p in names if "/.firecrawl/" in p or "/.unlazy/" in p]; assert not bad,bad; files=["src/hwlog/query.py","src/hwlog/assets/SKILL.md","README.md","docs/research-notes.md","docs/agent-workflow.md","pyproject.toml"]; assert all(a.extractfile(prefix+p).read()==Path(p).read_bytes() for p in files); w=zipfile.ZipFile("dist/hardware_logging-0.1.0-py3-none-any.whl"); assert w.read("hwlog/query.py")==Path("src/hwlog/query.py").read_bytes(); assert w.read("hwlog/assets/SKILL.md")==Path("src/hwlog/assets/SKILL.md").read_bytes(); assert not any(p.startswith((".firecrawl/",".unlazy/")) for p in w.namelist()); print("distribution contents verified")'
  EXPECT: distribution contents verified
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/Users/gurucharan/Documents/personal/hardware-logging; path=892706188a42/24 entries; output=distribution contents verified
