# Revenue Audit and Objective-Layer Readiness

Status: the revenue audit and objective interface are implemented. Integration of the four requested costs into an operational solver is blocked because the production solver is absent from the repository. This report does not describe the generic coverage MIP as the production solver.

## 1. Solver inventory

The current repository contains experiment runners, pairing validation, and an archived file at ../patches/mip.py.orig. It does not contain active source for the production column-generation / aircraft-timeline solver described in the meeting and handoff materials. The generic MIP has binary pairing-selection variables x_p and uncovered-flight slack u_f:

\[
\sum_p A_{fp}x_p + u_f = 1,\qquad
\min (N_{active}+1)\sum_f u_f + \sum_p x_p.
\]

It prioritizes covering flights, then minimizes selected pairings. u_f is not a cancellation decision; selected pairings are not distinct crew sets. The generic model does not model aircraft-tail trackability or crew roster feasibility.

## 2. Revenue join audit

Inputs: 19,238 prepared schedule rows and 1,533 revenue rows. Joins use route market, equipment code, and weekday.

| Join convention | Matched schedule rows | Rate |
|---|---:|---:|
| Literal directed route only | 9,548 / 19,238 | 49.631% |
| Literal directed route + equipment + weekday | 3,742 / 19,238 | 19.451% |
| Hub-spoke market route only (inbound/outbound both normalized to SIN + spoke) | 19,106 / 19,238 | 99.314% |
| Market route + equipment | 7,489 / 19,238 | 38.928% |
| Market route + equipment + weekday | 7,489 / 19,238 | 38.928% |

Direction-normalizing to SIN + spoke is an assumption that revenue is market-level in either direction; data-owner confirmation is required. Exact equipment-code matching is used. No crosswalk is guessed.

There are 618 distinct schedule route/equipment/weekday keys; 302 match unambiguously (48.867%). Revenue has 1,533 distinct route/equipment/weekday keys and no duplicates. No further rows are lost due to weekday once route and equipment are matched.

| Schedule equipment | Rows | Exact matched | Match rate |
|---|---:|---:|---:|
| 320 | 3,807 | 3,547 | 93.170% |
| 32N | 4,014 | 3,942 | 98.206% |
| 32P | 1,533 | 0 | 0.000% |
| 32Q | 9,884 | 0 | 0.000% |

Revenue contains codes 320, 32N, 321, 78B, 788, 789 and 290, but not 32P or 32Q. Do not map 32P/32Q to 321 or another type without an authoritative crosswalk. The market SINKWE is absent from revenue (132 schedule rows); 220 more rows have a route but no matching route/equipment combination. Exact unmatched-key groups and example flight/date values are emitted to CSV.

### Revenue anomalies

- 25 PLF records are above 1.0 (range 0.323–1.194; median 0.880).
- The fare column has no missing/non-numeric or nonpositive values, but ranges from SGD 2,333.33 to SGD 157,750 (median SGD 31,083.33).
- 121 fare rows exceed a SGD 100,000 screening value and 63 exceed the Tukey 1.5×IQR screening bound (~SGD 109,563). These are screening flags, not accepted business rules.
- Fare meaning and scale remain unconfirmed.

Revenue-driven cancellation cost stays disabled until the owner confirms route convention, fare definition/units, treatment of PLF above 1.0, missing routes, and the equipment crosswalk.

## 3. Four-term objective interface

objective_spec.py exposes tested component functions, a configurable objective policy, cost-parameter loading, and validation. It does not add fake cost terms to the generic MIP.

1. Cancellation penalty: sums supplied owner-validated per-flight cancellation values. Raw fare/PLF rows are not converted into a per-flight penalty.
2. Retiming penalty: source tiers are 0 minutes = retime_base 0; 5 minutes = 0.001; 10 minutes = 0.003; 15 minutes = 0.006. Other offsets fail closed. The approved range and symmetric interpretation need confirmation.
3. Crew efficiency: source scalers are unutilized_hub_time_cost_scaling 20, unutilized_spoke_time_cost_scaling 40, and fdp_wasted_cost_scaling 1. The metric units and efficiency calculations need confirmation before using them.
4. Crew-set count: counts distinct crew sets from an actual roster/assignment layer. man_day_cost 785 is loaded but not assumed to equal cost per set.

The proposed priority order follows the meeting summary—cancellations, retiming, crew efficiency, crew-set count—but is not owner-confirmed; the meeting notes flag ambiguity between the last two. Lexicographic priority is configurable; weighted mode requires four explicit nonnegative weights. No weights are invented.

## 4. Comparison with the saved coverage-only baseline

The values below come from saved logs, not a new optimizer run. The objective decomposition is independently recomputed.

| Horizon | True span | Active flights | Covered flights | Coverage | Selected pairings | Uncovered flights | Uncovered-slack term | Pairing-count term | Recomputed objective |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| One week | 7 days | 811 | 728 | 89.766% | 325 | 83 | 67,396 | 325 | 67,721 |
| Full schedule | 155 days | 18,901 | 17,366 | 91.879% | 7,721 | 1,535 | 29,014,570 | 7,721 | 29,022,291 |

The factor M = active flights + 1 is 812 for the week and 18,902 for the full schedule. The recomputed objective matches the saved reported objective in both logs. The historical full-horizon log says 166 days, but 25 Oct 2026 to 28 Mar 2027 inclusive is 155 days.

| Requested operational component | Existing coverage-only baseline | Four-term operational objective |
|---|---|---|
| Cancellation / revenue | Not modeled | BLOCKED — NOT RUN |
| Retiming magnitude | Not modeled in MIP objective | BLOCKED — NOT RUN |
| Unused FDP / crew-string efficiency | Not modeled | BLOCKED — NOT RUN |
| Distinct crew-set count | Not modeled; pairing count is not crew-set count | BLOCKED — NOT RUN |

There is no defensible operational objective value or cost delta until the real solver and decision variables are supplied. Operational component costs are blank, not zero.

## Reproduction

\`\`\`bash
python revenue_audit.py
python objective_layer_readiness.py
python -m pytest -q tests
\`\`\`

Machine-readable outputs are under ../output/revenue_audit/ and ../output/objective_readiness/.
