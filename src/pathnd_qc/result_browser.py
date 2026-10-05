"""Small offline contents pages; all links are relative so copied result folders still work.

These pages expose saved results and execution status, without interpreting QC measurements.
Navigation is best effort and must never replace or fail the scientific report.
"""
from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path
from urllib.parse import quote

from pathnd_qc import artifact_store as store
from pathnd_qc._logging import get_logger
from pathnd_qc.output_layout import write_output_guide


def _text(value) -> str:
    return escape(str(value if value is not None else "Unknown"))


def _link(path: Path, root: Path, label: str) -> str:
    href = quote(Path(os.path.relpath(path.resolve(), root.resolve())).as_posix(), safe="/")
    return f'<a href="{href}">{_text(label)}</a>'


def write_page(path, title: str, body: str) -> None:
    """Write an offline page atomically. Caller escapes any variable text in body."""
    with store.atomic_write(path) as tmp:
        tmp.write_text(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_text(title)}</title><style>'
            'body{font:17px/1.6 system-ui,sans-serif;max-width:1100px;margin:3rem auto;padding:0 1.5rem;'
            'color:#243047;background:#fafafa}h1,h2{line-height:1.3}a{color:#145ea8}'
            'table{border-collapse:collapse;width:100%}td,th{text-align:left;vertical-align:top;'
            'padding:.6rem;border-bottom:1px solid #ddd}code,td{overflow-wrap:anywhere}'
            'dt{font-weight:600}dd{margin:0 0 .8rem;overflow-wrap:anywhere}'
            '</style><body>'
            f'<h1>{_text(title)}</h1>{body}</body></html>\n', encoding="utf-8")


def write_run_index(report: dict, report_path, run_dir, out_dir) -> None:
    """List only available local outputs. Never copy or link externally supplied inputs."""
    if run_dir is None:
        return
    try:
        root = Path(run_dir)
        prov = report.get("provenance") or {}
        execution = prov.get("execution") or {}
        error = report.get("error")
        state = ("Failed" if error else "Requested processing complete"
                 if execution.get("complete") is True else "Processing incomplete or not established")
        body = f'<p><strong>{state}</strong>. Processing completion is separate from slide quality.</p>'
        details = {"Slide source": report.get("slide_path"), "Generated (UTC)": report.get("generated_at"),
                   "Stain": prov.get("stain_type"), "Pipeline version": prov.get("pipeline_version"),
                   "Git commit": prov.get("git_commit"), "Modified source": prov.get("git_dirty")}
        body += "<dl>" + "".join(f"<dt>{key}</dt><dd>{_text(value)}</dd>" for key, value in details.items()) + "</dl>"
        if error:
            body += f'<p>Run error: {_text(error.get("message"))}</p>'
        reasons = execution.get("reasons") or []
        if reasons:
            body += '<h2>Execution notes</h2><ul>' + ''.join(f'<li>{_text(r)}</li>' for r in reasons) + '</ul>'
        body += "<h2>Report</h2><ul>"
        if report_path and Path(report_path).is_file():
            body += f'<li>{_link(Path(report_path), root, "Full measurements and provenance (JSON)")}</li>'
        else:
            body += "<li>Report writing was disabled or no report was saved.</li>"
        for status in root.glob("*_status.json"):
            body += f'<li>{_link(status, root, "Run status (JSON)")}</li>'
        body += "</ul><p>QC threshold comparisons are in the report’s verdict section; "
        body += "an unconfigured threshold does not judge slide quality.</p>"
        outputs = prov.get("outputs") or {}
        for directory, heading in (("images", "Images and masks"), ("masks", "Segmentation masks (older layout)"),
                                   ("data", "Detailed tile data")):
            links = []
            for meta in outputs.values():
                path = Path(meta.get("path", ""))
                try:
                    relative = path.resolve().relative_to(root.resolve())
                except ValueError:
                    continue
                if relative.parts[0] == directory and path.is_file():
                    links.append(f'<li>{_link(path, root, path.name)}</li>')
            if links:
                body += f"<h2>{heading}</h2><ul>{''.join(links)}</ul>"
        body += "<p>Only saved outputs are listed. Requested components may be skipped or fail; "
        body += "see the report. Supplied inputs remain at their original paths.</p>"
        write_page(root / "index.html", f"Slide {report.get('slide_id', '')}", body)
        if out_dir is not None:
            write_output_guide(out_dir)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        get_logger(__name__).warning("Could not write result navigation: %s", exc)


def write_batch_index(record: dict, rows: list[dict] | None = None) -> None:
    """Link this batch's outcomes (including refused and resumed jobs) to their available results."""
    try:
        root = Path(record["batch_dir"])
        if rows is None:
            rows = json.loads((root / "results.json").read_text(encoding="utf-8"))
        body = '<p>One row per job in this batch. Job outcome describes execution, not slide quality.</p>'
        body += '<p>' + ' · '.join(_link(root / name, root, label) for name, label in
                                 (("results.csv", "Job outcomes (CSV)"), ("results.json", "Job outcomes (JSON)"),
                                  ("batch.log", "Batch log"), ("batch.json", "Batch settings"))
                                 if (root / name).is_file()) + '</p>'
        if (root / "summary.csv").is_file():
            body += '<p>' + _link(root / "summary.csv", root, "Measurements in this batch (CSV)") + '</p>'
        body += '<table><thead><tr><th>Slide / source</th><th>Job outcome</th><th>Results</th><th>Notes</th></tr></thead><tbody>'
        for row in rows:
            folder = Path(row["run_dir"]) if row.get("run_dir") else None
            links = []
            if folder:
                candidates = [folder / "index.html", *sorted(folder.glob("*_report.json")),
                              *sorted(folder.glob("*_status.json"))]
                links = [_link(path, root, "Open run" if path.name == "index.html" else path.name)
                         for path in candidates if path.is_file()]
            body += (f'<tr><td>{_text(row["key"])}<br>{_text(row["slide"])}</td>'
                     f'<td>{_text(row["outcome"])}</td><td>{"<br>".join(links) or "No saved run"}</td>'
                     f'<td>{_text(row.get("message") or "")}</td></tr>')
        body += '</tbody></table><p>summary.csv and summary.json contain measurements from saved '
        body += 'runs in this batch folder, including earlier attempts when this batch is resumed.</p>'
        write_page(root / "index.html", f'Batch {record["batch_id"]}', body)
        write_output_guide(record["out_dir"])
        write_output_guide(root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        get_logger(__name__).warning("Could not write batch navigation: %s", exc)
