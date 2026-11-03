# Utilities Suite — Applications & Tooling Specifications

This folder contains exhaustive technical specifications, architectural breakdowns, feature inventories, configuration references, and end-to-end execution pseudo-scripts for each utility application in the workspace.

---

## Applications Index

| App Name | Source File / Directory | Technology Stack | Core Purpose | Documentation |
|---|---|---|---|---|
| **CVsorter** | `cvsorter/` | Python 3, Tkinter, Tesseract OCR, PyMuPDF, python-docx, python-pptx, watchdog, RapidFuzz | Automated folder watcher, multi-format CV ingestion, OCR, applicant name/metadata extraction, candidate duplicate registry, interactive review dialog, and dynamic file renaming/routing. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/cvsorter.md) |
| **Docpacker** | `docpacker.py` | Python 3, Tkinter, PyMuPDF (fitz), Tesseract OCR, OpenCV, NumPy | Document compliance verification (passports, photos, medical reports, bank letters), automated facial & MRZ checksum checks, rotation/skew correction, and unified PDF generation with cover page & bookmarks. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/docpacker.md) |
| **File Picker** | `file_picker.py` | Python 3 (Standard Library only), Tkinter | High-speed batch search, organization, dry-run preview, and copy/move tool matching names against multi-extension files with regex support and conflict resolution. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/file_picker.md) |
| **Multi File Merger** | `Mulit File Merger.html` | HTML5, CSS3, JavaScript, `pdf-lib`, `mammoth.js`, `html2canvas` | Offline-first browser tool to merge PDFs, photos (with A4 centering or custom canvas scaling), and Word `.docx` documents (with RTL Arabic support) into a single PDF. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/multi_file_merger.md) |
| **Name Matcher** | `name-matcher.html` | HTML5, CSS3, JavaScript, SheetJS (`xlsx.js`) | Offline fuzzy name matcher reconciling lists between two Excel/CSV spreadsheets; features specialized Arabic orthographic normalization, token-sort algorithms, non-blocking time-sliced execution, and 3-sheet export. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/name_matcher.md) |
| **Screenshot Extract** | `screenshot_extract.py` | Python 3, Tkinter, PIL, Tesseract OCR, pyperclip | Real-time clipboard listener detecting screenshot captures, performing OCR and targeted extraction (phones, emails, names, job titles, IBANs, qualifications), with side-by-side editing popup and tab-separated spreadsheet export. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/screenshot_extract.md) |
| **Sheet Sync Standalone** | `sheet-sync-standalone.html` | HTML5, CSS3, Pure Vanilla JavaScript (Zero Dependencies) | Synchronizes an existing master Excel sheet from a newer one; matches on unique keys, preserves existing values on blank cells, detects modified cells, and exports an updated workbook with a cell-level change log. | [Read Specification](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/sheet_sync_standalone.md) |

---

## Detailed Specifications Overview

1. [**CVSorter Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/cvsorter.md)
   - Real-time directory watching with stability heuristics.
   - Intelligent hybrid text/OCR extraction for PDF, Office (`.docx`, `.pptx`), legacy formats (`.doc`, `.ppt` via LibreOffice), and multi-frame images.
   - Extraction heuristics for Name, Email, Phone, Job Title, Company (with canonical snapping), and user-defined Regex/Keyword fields.
   - External candidate name registry with live duplicate detection and auto-growth.
   - Side-by-side verification dialog with original visual page preview and live dynamic filename preview.
   - Customizable clipboard templates and CRM/SharePoint URL builders.

2. [**Docpacker Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/docpacker.md)
   - Compliance rules engine for Passports, Photos, Bank letters, and Medical reports.
   - Automated MRZ ICAO 9303 checksum validation, passport expiry verification vs joining date.
   - OpenCV Haar cascade face detection and EXIF smartphone camera exclusion.
   - Automatic scan orientation correction (Tesseract OSD) and Hough transform deskewing.
   - Multi-page PDF segmentation and page picker.
   - Automated cover page compilation with checklist icons, metadata grid, and PDF bookmarks.
   - 14 modular feature toggles including batch processing and Arabic RTL font shaping.

3. [**File Picker Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/file_picker.md)
   - Multi-delimiter text parser with case-insensitive deduplication and order retention.
   - Four matching modes: Exact (multi-extension aware), Starts With, Contains, and Regular Expressions.
   - Recursive vs flat folder scanning, relative subfolder tree preservation, and extension whitelisting.
   - Non-destructive dry-run Preview mode, color-coded live audit log, cancellation support, and missing names export.
   - Zero third-party dependencies with automatic JSON settings persistence.

4. [**Multi File Merger (Merge Desk) Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/multi_file_merger.md)
   - 100% in-browser, client-side PDF compilation from mixed PDFs, images, and `.docx` files.
   - Drag-and-drop workspace with visual image thumbnails, page counts, and interactive reordering.
   - Image layout controls: proportional centering on A4 pages vs photo-dimension matching.
   - `.docx` pagination and HTML-to-canvas rasterization engine with bidirectional RTL language support.
   - Progressive compilation loop, PDF metadata stamping, and Claude Artifacts / browser download bridge.

5. [**Sheet Name Matcher Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/name_matcher.md)
   - Specialized bilingual normalization engine handling Arabic-Indic digits, harakat diacritics, and orthographic variants (Alef, Yaa, Heh, Hamza).
   - Multi-metric scoring combining Levenshtein distance, token sort ratio (swapped word orders), and length-weighted token coverage.
   - Non-blocking 40ms time-sliced matrix calculation with live progress bar.
   - Reconciliation ledger with Strong, Probable, and Weak filter pills, and live text search.
   - Comprehensive 3-sheet Excel export (`Matches`, `All candidates`, `Unmatched in B`).

6. [**Screenshot Extract Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/screenshot_extract.md)
   - Background clipboard listener with MD5 image deduplication.
   - Lanczos 2x upscaling, contrast boost, and Tesseract OCR with email whitespace repair.
   - Eight modular extractor types: phone, email, name, keywords, regex, after_label, today, manual.
   - Pinned control panel with field checkboxes and "Re-scan last screenshot" utility.
   - Side-by-side verification popup with single-keystroke `Enter`/`Esc` export to tab-separated spreadsheet format.

7. [**Sheet Sync Standalone Specification**](file:///c:/Users/admin/Desktop/Mustafa/Code/utils/docs/sheet_sync_standalone.md)
   - Custom in-memory OpenXML parser and writer implemented in pure JavaScript (zero libraries).
   - Native ZIP decompression via browser `DecompressionStream`.
   - Unique key matching with whitespace and case folding.
   - Conditional synchronization rules: add new rows, preserve old values on blank cells, add new columns, and inject sync status tags.
   - Interactive diff table showing cell modifications and dual download modes (full updated workbook or standalone change log).

