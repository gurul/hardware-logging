---
title: Research notes
order: 7
---

# Research notes

These primary sources inform hwlog's evidence handling. The applications below
are engineering inferences; the papers do not validate hwlog's hardware
correctness, capture completeness, or performance.

## Papers and applications

### Dapper (2010)

[Dapper, a Large-Scale Distributed Systems Tracing Infrastructure](https://research.google.com/archive/papers/dapper-2010-1.pdf),
Benjamin H. Sigelman, Luiz André Barroso, Mike Burrows, Pat Stephenson, Manoj
Plakal, Donald Beaver, Saul Jaspan, and Chandan Shanbhag. Google technical report.

**Finding:** Section 2.1 gives traces and spans separate identities. Section 4.4
records sampling probability with each trace and explains that low-volume
workloads can lose important events under aggressive sampling.

**Application:** Keep identity when reducing output: repeat collapse applies only
to ordinary `log` records with the same boot, level, tag, message, source, and
payload. Preserve each non-log event and leave source records unchanged.

**Limit:** Boot-scoped serial logs have no distributed span tree. Collapse is a
display transformation; it does not sample the stored capture.

### GTSO (2017; 2018 journal issue)

[GTSO: Global Trace Synchronization and Ordering Mechanism for Wireless Sensor Network Monitoring Platforms](https://pmc.ncbi.nlm.nih.gov/articles/PMC5796343/),
Marlon Navia, José Carlos Campelo, Alberto Bonastre, and Rafael Ors. *Sensors*
18(1):28, published December 23, 2017; January 2018 issue.

**Finding:** Section 2 shows how correcting a clock backward can invert event
order. Section 4 uses uniquely numbered synchronization events to relate traces,
including when synchronization points are missed.

**Application:** Interpret `seq` as host capture order within a session, `ts` as
host wall-clock UTC, and `dev_ts` as optional device-reported milliseconds. Keep
boot boundaries visible when messages repeat.

**Limit:** hwlog does not implement GTSO, synchronize device clocks, or establish
cross-device causality. Boot indices reflect detected markers, so an unobserved
reset can remain uncounted.

### Log20 (2017)

[Log20: Fully Automated Optimal Placement of Log Printing Statements under Specified Overhead Threshold](https://www.eecg.toronto.edu/~yuan/papers/p125-Zhao.pdf),
Xu Zhao, Kirk Rodrigues, Yu Luo, Michael Stumm, Ding Yuan, and Yuanyuan Zhou.
SOSP 2017, DOI: [10.1145/3132747.3132778](https://doi.org/10.1145/3132747.3132778).

**Finding:** Section 1 balances log informativeness against a specified overhead
budget. Section 7 states that profiled trace coverage is not exhaustive and that
a changed workload can produce excessive logging overhead.

**Application:** Expose the bounds of every compact observation. Summary counts
cover its scanned window; scan truncation, skipped records, an incomplete tail,
and storage drops remain visible. Retain `raw.log` alongside parsed `log.jsonl`
to investigate parser interpretation within the captured window.

**Limit:** Log20 changes logging instrumentation using workload profiles. hwlog
consumes existing serial output; its 500-record query ceiling and 16 MiB/32 KiB
summary budgets bound host work, not firmware logging overhead or lost bytes.

### Observing a Moving Target (2021)

[Observing a Moving Target — Reliable Transmission of Debug Logs from Mobile Embedded Devices](https://arxiv.org/html/2110.01412v2),
Björn Daase, Leon Matthes, Lukas Pirl, and Lukas Wenzel. Author-deposited IEEE
manuscript, arXiv:2110.01412v2.

**Finding:** Sections III and IV compare continuous/aperiodic wired and wireless
log transport. The slot-car case study finds that transmission can alter device
power use and normal operation, and that transport suitability depends on the
deployment and development phase.

**Application:** Use one serial capture owner and file-based consumers to avoid
competing readers. Record disconnect/reconnect status. When a log file is
replaced, reset the follower's byte offset and pending partial line on a
device/inode identity change, including replacement by a larger file.

**Limit:** This ownership/follower policy is our inference, not the paper's
algorithm. UART output before connection, during flashing, or after a transport
failure may be absent; actual device timing and energy effects need measurement.

## Rerun inputs

Workflow: `firecrawl-research-papers` and `firecrawl-research-index`; target:
at least three primary papers; output: this Markdown note.

Start with semantic paper queries:

- `Low overhead event tracing and debugging embedded systems microcontrollers serial logs dropped records boot identity event ordering`
- `Structured system logging accurate event sequences diagnosis log integrity instrumentation overhead Dapper DeepLog Log20`

Expand `arxiv:2110.01412` and `pmcid:PMC5796343` with intent
`Embedded device debugging logs, stable event identity, trace ordering, distinguishing reboots, and logging overhead`.
Read candidate bodies for the specific findings above. The index returned no
GTSO body passages and missed Log20; use the linked primary full texts for those
cases. Verify Dapper against Google's report: a different TCP-diagnosis paper
shares its name. Local search output and extracted text are cached in
`.firecrawl/`, which is excluded from Git.
