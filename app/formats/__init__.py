"""Format adapters for the supported file types.

Translation adapters (parse + write-back): ``docx_fmt``, ``pptx_fmt``,
``xlsx_fmt``, ``pdf_fmt`` (selected by extension at runtime in
``common.get_format_handler``).

UI preview: ``xlsx_preview`` exposes a streaming HTML renderer used by
``/api/tasks/{task_id}/preview/xlsx`` to show an XLSX side-by-side
comparison in the browser. It is **not** a translation adapter — the
name overlap is a known wart; renaming it would touch 3 importers
(previews.py, test_xlsx_preview.py, and an entry in docs/architecture.md)
for cosmetic gain, so it is left in place. The module-level docstring
makes the role explicit.
"""
