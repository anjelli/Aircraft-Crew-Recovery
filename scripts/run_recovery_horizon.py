"""Horizon-wide recovery preflight and deliverable exporter.

The runner deliberately fails closed: it never labels the source schedule as
recovered when required operational inputs or an integrated crew solver are
missing. It exports one workbook plus CSV/JSON artifacts for 1W, 1M, 154D, and 155D.
No rolling windows are used, so no inter-window state is silently discarded.
"""
undefined